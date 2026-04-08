#!/usr/bin/env python3
"""
Solana Wallet Discovery Tool
────────────────────────────
Commands:
  clusters               Show known clusters from your wallet list (no API)
  scan-block <sig>       Scan block for same-fee matches to a known tx
  scan-cluster <name>    Co-occurrence scan for a named cluster
  trace <address>        Trace funding chain for an address
  candidates             Show saved match candidates from DB
  refresh <name>         Re-fetch transaction data for a named cluster
  export <name> [addrs]  Export cluster as importable JSON with UPPER/lower naming
"""
import asyncio
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
from rich.text import Text

from src.config import HELIUS_API_KEY, DB_PATH
from src.db import init_db, get_candidates
from src.wallets import WalletRegistry
from src.analysis import summary, cluster_by_group
from src.helius import HeliusClient
from src.matcher import (
    same_block_fee_scan,
    co_occurrence_scan,
    fetch_and_cache_wallet_sigs,
    funding_trace,
)
from src.output import export_cluster, write_export, merge_into_wallets_json
from src.twitter import search_url as twitter_search_url
from src.gmgn import wallet_page_url as gmgn_page_url
from src.kolscan import wallet_page_url as kolscan_page_url

console = Console()


def check_setup():
    if not HELIUS_API_KEY:
        console.print("[red]Error:[/red] HELIUS_API_KEY not set. Copy .env.example to .env and add your key.")
        sys.exit(1)
    if not Path("wallets.json").exists():
        console.print("[red]Error:[/red] wallets.json not found.")
        sys.exit(1)


def cmd_clusters(registry: WalletRegistry):
    """Show known clusters from wallet list."""
    stats = summary(registry)

    console.print(Panel(
        f"[bold]Total wallets:[/bold] {stats['total_wallets']}  "
        f"[bold]Unique names:[/bold] {stats['unique_names']}  "
        f"[bold]Known clusters:[/bold] {stats['known_clusters']}  "
        f"[bold]Uncertain (?):[/bold] {stats['uncertain']}",
        title="Wallet Registry Summary",
        border_style="cyan",
    ))

    # Groups table
    groups = stats["groups"]
    gt = Table(title="Groups", box=box.SIMPLE)
    gt.add_column("Group", style="cyan")
    gt.add_column("Wallets", justify="right")
    for g, count in sorted(groups.items(), key=lambda x: -x[1]):
        gt.add_row(g, str(count))
    console.print(gt)

    # Top clusters
    ct = Table(title="Top Known Clusters (same name = likely same person)", box=box.SIMPLE)
    ct.add_column("Name", style="green")
    ct.add_column("Count", justify="right", style="yellow")
    ct.add_column("Addresses (first 3)")
    for cluster in stats["top_clusters"]:
        addrs = cluster["addresses"]
        addr_preview = ", ".join(a[:8] + "…" for a in addrs[:3])
        if len(addrs) > 3:
            addr_preview += f" +{len(addrs)-3} more"
        ct.add_row(cluster["name"], str(cluster["count"]), addr_preview)
    console.print(ct)


async def cmd_scan_block(tx_sig: str, registry: WalletRegistry):
    """Scan block of a given tx for same-fee matches."""
    client = HeliusClient()
    try:
        console.print(f"\n[cyan]Scanning block for transaction:[/cyan] {tx_sig}")
        console.print("[dim]Fetching transaction and block data...[/dim]")

        results = await same_block_fee_scan(tx_sig, registry, client)

        if not results:
            console.print("[yellow]No same-fee matches found in this block.[/yellow]")
            return

        t = Table(title=f"Same-Block Fee Matches ({len(results)} found)", box=box.ROUNDED)
        t.add_column("Address", style="cyan")
        t.add_column("Fee (lamports)", justify="right")
        t.add_column("Confidence", justify="right")
        t.add_column("Already Tracked")
        t.add_column("Known As")

        for r in sorted(results, key=lambda x: -x.confidence):
            conf_color = "green" if r.confidence >= 0.8 else "yellow" if r.confidence >= 0.5 else "red"
            already = "[green]Yes[/green]" if r.known_label else "[dim]No[/dim]"
            t.add_row(
                r.address,
                str(r.evidence.get("fee", "?")),
                f"[{conf_color}]{r.confidence:.0%}[/{conf_color}]",
                already,
                r.known_label or "-",
            )
        console.print(t)

        untracked = [r for r in results if not r.known_label]
        console.print(f"\n[bold yellow]{len(untracked)} untracked wallets[/bold yellow] found with same fee.")
        console.print(f"[dim]Credits used: {client.credits_used}[/dim]")
    finally:
        await client.close()


async def cmd_scan_cluster(name: str, registry: WalletRegistry):
    """Co-occurrence scan for all wallets with a given name."""
    wallets = registry.by_name(name)
    if not wallets:
        console.print(f"[red]No wallets found with name:[/red] {name}")
        # Suggest similar
        similar = registry.search(name)
        if similar:
            console.print("Did you mean:")
            for w in similar[:5]:
                console.print(f"  {w.display}")
        return

    console.print(f"\n[cyan]Scanning cluster:[/cyan] '{name}' ({len(wallets)} wallets)")
    for w in wallets:
        console.print(f"  {w.display}")

    client = HeliusClient()
    try:
        addresses = [w.address for w in wallets]

        console.print(f"\n[dim]Fetching recent transactions for {len(wallets)} wallets...[/dim]")
        results = await co_occurrence_scan(addresses, registry, client)

        if not results:
            console.print("[yellow]No significant co-occurrences found. Try refreshing tx data first.[/yellow]")
            console.print("Tip: Run [bold]python discover.py refresh " + name + "[/bold] to fetch fresh data.")
            return

        t = Table(title=f"Co-occurrence Matches for '{name}'", box=box.ROUNDED)
        t.add_column("Address", style="cyan")
        t.add_column("Co-occurring slots", justify="right", style="yellow")
        t.add_column("Confidence", justify="right")
        t.add_column("Known As")

        for r in sorted(results, key=lambda x: -x.confidence)[:30]:
            conf_color = "green" if r.confidence >= 0.7 else "yellow"
            t.add_row(
                r.address,
                str(r.evidence.get("co_occurring_slots", "?")),
                f"[{conf_color}]{r.confidence:.0%}[/{conf_color}]",
                r.known_label or "[dim]untracked[/dim]",
            )
        console.print(t)
        console.print(f"[dim]Credits used: {client.credits_used}[/dim]")
    finally:
        await client.close()


async def cmd_trace(address: str, registry: WalletRegistry):
    """Trace funding chain for an address."""
    w = registry.get(address)
    if w:
        console.print(f"\n[cyan]Tracing funding for:[/cyan] {w.display}")
    else:
        console.print(f"\n[cyan]Tracing funding for:[/cyan] {address}")

    client = HeliusClient()
    try:
        results = await funding_trace(address, registry, client)

        if not results:
            console.print("[yellow]No tracked funding ancestors found.[/yellow]")
            return

        t = Table(title="Funding Chain Matches", box=box.ROUNDED)
        t.add_column("Known Wallet", style="cyan")
        t.add_column("Hops", justify="right")
        t.add_column("Confidence", justify="right")
        t.add_column("Path")

        for r in sorted(results, key=lambda x: -x.confidence):
            conf_color = "green" if r.confidence >= 0.7 else "yellow"
            path = " → ".join(p[:8] + "…" for p in r.evidence.get("funding_path", []))
            t.add_row(
                r.known_label or r.address[:12] + "…",
                str(r.evidence.get("hops", "?")),
                f"[{conf_color}]{r.confidence:.0%}[/{conf_color}]",
                path,
            )
        console.print(t)
        console.print(f"[dim]Credits used: {client.credits_used}[/dim]")
    finally:
        await client.close()


async def cmd_refresh(name: str, registry: WalletRegistry):
    """Fetch fresh transaction data for a cluster into local cache."""
    wallets = registry.by_name(name)
    if not wallets:
        console.print(f"[red]No wallets found:[/red] {name}")
        return

    client = HeliusClient()
    try:
        console.print(f"[cyan]Refreshing tx data for '{name}' ({len(wallets)} wallets)...[/cyan]")
        for w in wallets:
            console.print(f"  Fetching {w.display}...", end="")
            slots = await fetch_and_cache_wallet_sigs(w.address, client, limit=100)
            console.print(f" {len(slots)} txs cached")
        console.print(f"\n[green]Done.[/green] Credits used: {client.credits_used}")
    finally:
        await client.close()


async def cmd_export(
    cluster_name: str,
    registry: WalletRegistry,
    extra_addresses: list[str],
    no_twitter: bool = False,
    output_file: str = "",
    merge: bool = False,
):
    """
    Export a cluster as importable JSON with UPPERCASE main / lowercase alts.

    Discovery pipeline:
      1. KolScan leaderboard  → main wallet
      2. GMGN realized PnL   → main wallet (fallback)
      3. Twitter/X search    → identity hints
    """
    wallets = registry.by_name(cluster_name)
    if not wallets and not extra_addresses:
        console.print(f"[red]No wallets found for cluster:[/red] {cluster_name}")
        similar = registry.search(cluster_name)
        if similar:
            console.print("Did you mean:")
            for w in similar[:5]:
                console.print(f"  {w.display}")
        return

    n_known = len(wallets)
    n_new = len(extra_addresses)
    console.print(
        f"\n[cyan]Exporting cluster:[/cyan] '{cluster_name}' "
        f"({n_known} tracked + {n_new} new)"
    )

    with console.status("[dim]Checking KolScan & GMGN…[/dim]"):
        result, entries = await export_cluster(
            cluster_name,
            registry,
            new_addresses=extra_addresses if extra_addresses else None,
            run_twitter=not no_twitter,
        )

    # ── Summary panel ────────────────────────────────────────────────────────
    main_addr = result.main_address or "unknown"
    main_w = registry.get(main_addr)
    pnl_str = ""
    if main_addr in result.pnl and result.pnl[main_addr] is not None:
        pnl_str = f"  [dim]PnL: ${result.pnl[main_addr]:,.0f}[/dim]"

    console.print(Panel(
        f"[bold green]Main wallet (UPPERCASE):[/bold green] {main_addr}\n"
        f"[dim]Source: {result.main_source}{pnl_str}[/dim]\n"
        f"[bold]Alt wallets:[/bold] {len(result.alt_addresses)}\n"
        f"[bold]New (discovered):[/bold] {len(result.new_addresses)}",
        title=f"Cluster: {cluster_name.upper()}",
        border_style="green",
    ))

    # ── Wallet table ─────────────────────────────────────────────────────────
    t = Table(title="Export Preview", box=box.SIMPLE)
    t.add_column("Address", style="cyan")
    t.add_column("Name", style="bold")
    t.add_column("Role")
    t.add_column("KolScan")
    t.add_column("GMGN PnL", justify="right")
    t.add_column("Twitter")

    all_addrs = result.all_addresses
    for entry in entries:
        addr = entry["trackedWalletAddress"]
        name = entry["name"]
        is_main = addr == result.main_address
        role = "[green]MAIN[/green]" if is_main else "[dim]alt[/dim]"
        if addr in result.new_addresses:
            role = "[yellow]NEW[/yellow]"

        ks_url = kolscan_page_url(addr)
        gm_url = gmgn_page_url(addr)
        pnl = result.pnl.get(addr)
        pnl_disp = f"${pnl:,.0f}" if pnl is not None else "[dim]—[/dim]"

        tw = result.twitter_hits.get(addr)
        tw_disp = (
            f"[link={tw.query_url}]{tw.suggested_name or str(tw.mentions) + ' hits'}[/link]"
            if tw else "[dim]—[/dim]"
        )

        t.add_row(addr[:16] + "…", name, role, f"[link={ks_url}]view[/link]", pnl_disp, tw_disp)

    console.print(t)

    # ── Write output ─────────────────────────────────────────────────────────
    if merge:
        updated, added = merge_into_wallets_json(entries)
        console.print(
            f"\n[green]✓ Merged into wallets.json[/green] — "
            f"{updated} updated, {added} added"
        )
    elif output_file:
        write_export(entries, output_file)
        console.print(f"\n[green]✓ Saved[/green] → {output_file}")
        console.print(f"[dim]Import this file into your terminal tracker.[/dim]")
    else:
        # Print JSON to stdout so user can pipe / copy
        console.print("\n[bold]Import JSON:[/bold]")
        console.print_json(json.dumps(entries, indent=2))

    # ── Manual verification links ────────────────────────────────────────────
    console.print("\n[dim]Manual verification:[/dim]")
    for addr in all_addrs[:5]:
        tw = result.twitter_hits.get(addr)
        tw_url = tw.query_url if tw else twitter_search_url(addr)
        console.print(
            f"  [cyan]{addr[:12]}…[/cyan]  "
            f"[link={kolscan_page_url(addr)}]KolScan[/link]  "
            f"[link={gmgn_page_url(addr)}]GMGN[/link]  "
            f"[link={tw_url}]Twitter[/link]"
        )
    if len(all_addrs) > 5:
        console.print(f"  … and {len(all_addrs) - 5} more")


def cmd_candidates(registry: WalletRegistry, min_conf: float = 0.5):
    """Show all saved match candidates from DB."""
    candidates = get_candidates(min_confidence=min_conf)
    if not candidates:
        console.print("[yellow]No candidates found. Run a scan first.[/yellow]")
        return

    t = Table(title=f"Match Candidates (confidence ≥ {min_conf:.0%})", box=box.ROUNDED)
    t.add_column("Address", style="cyan")
    t.add_column("Matched To")
    t.add_column("Type")
    t.add_column("Confidence", justify="right")
    t.add_column("Known As")

    for c in candidates:
        conf = c["confidence"]
        conf_color = "green" if conf >= 0.8 else "yellow" if conf >= 0.5 else "red"
        matched_w = registry.get(c["matched_wallet"])
        candidate_w = registry.get(c["address"])
        t.add_row(
            c["address"][:12] + "…",
            (matched_w.label if matched_w else c["matched_wallet"][:12] + "…"),
            c["match_type"],
            f"[{conf_color}]{conf:.0%}[/{conf_color}]",
            candidate_w.label if candidate_w else "[dim]untracked[/dim]",
        )
    console.print(t)
    console.print(f"\n[dim]{len(candidates)} candidates. "
                  f"Untracked = potential new wallet to add.[/dim]")


async def main():
    args = sys.argv[1:]
    if not args:
        console.print(__doc__)
        return

    cmd = args[0]

    if cmd == "clusters":
        check_setup()
        registry = WalletRegistry()
        cmd_clusters(registry)

    elif cmd == "scan-block":
        if len(args) < 2:
            console.print("[red]Usage:[/red] python discover.py scan-block <tx_signature>")
            return
        check_setup()
        registry = WalletRegistry()
        init_db()
        await cmd_scan_block(args[1], registry)

    elif cmd == "scan-cluster":
        if len(args) < 2:
            console.print("[red]Usage:[/red] python discover.py scan-cluster <name>")
            return
        check_setup()
        registry = WalletRegistry()
        init_db()
        await cmd_scan_cluster(args[1], registry)

    elif cmd == "trace":
        if len(args) < 2:
            console.print("[red]Usage:[/red] python discover.py trace <address>")
            return
        check_setup()
        registry = WalletRegistry()
        init_db()
        await cmd_trace(args[1], registry)

    elif cmd == "refresh":
        if len(args) < 2:
            console.print("[red]Usage:[/red] python discover.py refresh <name>")
            return
        check_setup()
        registry = WalletRegistry()
        init_db()
        await cmd_refresh(args[1], registry)

    elif cmd == "candidates":
        check_setup()
        registry = WalletRegistry()
        init_db()
        min_conf = float(args[1]) if len(args) > 1 else 0.5
        cmd_candidates(registry, min_conf)

    elif cmd == "export":
        if len(args) < 2:
            console.print(
                "[red]Usage:[/red] python discover.py export <name> [addr1 addr2 …] "
                "[--no-twitter] [--out file.json] [--merge]"
            )
            return
        check_setup()
        registry = WalletRegistry()

        cluster_name = args[1]
        extra_addrs = []
        no_twitter = False
        out_file = ""
        merge = False
        i = 2
        while i < len(args):
            a = args[i]
            if a == "--no-twitter":
                no_twitter = True
            elif a == "--merge":
                merge = True
            elif a == "--out" and i + 1 < len(args):
                out_file = args[i + 1]
                i += 1
            elif not a.startswith("--"):
                extra_addrs.append(a)
            i += 1

        await cmd_export(
            cluster_name,
            registry,
            extra_addresses=extra_addrs,
            no_twitter=no_twitter,
            output_file=out_file,
            merge=merge,
        )

    else:
        console.print(f"[red]Unknown command:[/red] {cmd}")
        console.print(__doc__)


if __name__ == "__main__":
    asyncio.run(main())
