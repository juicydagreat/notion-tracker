#!/usr/bin/env python3
"""
Solana Wallet Discovery Daemon
──────────────────────────────
Continuously monitors tracked wallets for new transactions and automatically
runs same-block fee matching when unique-fee transactions are detected.

Usage:
  python daemon.py                          # monitor all wallets
  python daemon.py --cluster brad           # only monitor cluster "brad"
  python daemon.py --max-credits 300        # stop after 300 Helius credits
  python daemon.py --interval 120           # active poll interval (seconds)
  python daemon.py --no-token-boost         # disable shared-account confidence boost
  python daemon.py --min-confidence 0.7     # only show candidates above threshold

How it works:
  1. Polls each wallet periodically for new transactions
  2. When a non-default fee is detected, fetches the block (~10 credits)
  3. Finds other wallets in that block with the same fee
  4. If they also share a token mint / DEX pool account → confidence boosted
  5. All candidates saved to discovery.db (view with: python discover.py candidates)

Credit usage (approximate):
  - 1 credit per wallet poll (getSignaturesForAddress)
  - ~10 credits per block scan, only for non-default fees, skipped if cached
  - With 700 wallets on idle intervals: ~70 credits/cycle
"""
import asyncio
import sys
import time
from datetime import datetime

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from src.config import HELIUS_API_KEY, DB_PATH, MAX_CREDITS_PER_RUN
from src.db import init_db, get_candidates
from src.helius import HeliusClient
from src.wallets import WalletRegistry
from src.monitor import WalletMonitor, MonitorConfig
from src.matcher import MatchResult

console = Console()

# Live feed of recent discoveries
_recent_events: list[dict] = []
_MAX_FEED = 20


def _check_setup():
    if not HELIUS_API_KEY:
        console.print("[red]Error:[/red] HELIUS_API_KEY not set in .env")
        sys.exit(1)


def _parse_args() -> dict:
    args = sys.argv[1:]
    cfg = {
        "cluster": None,
        "max_credits": MAX_CREDITS_PER_RUN,
        "interval": 60,
        "idle_interval": 600,
        "token_boost": True,
        "min_confidence": 0.5,
        "addresses": [],
    }
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            console.print(__doc__)
            sys.exit(0)
        elif a == "--cluster" and i + 1 < len(args):
            cfg["cluster"] = args[i + 1]; i += 1
        elif a == "--max-credits" and i + 1 < len(args):
            cfg["max_credits"] = int(args[i + 1]); i += 1
        elif a == "--interval" and i + 1 < len(args):
            cfg["interval"] = int(args[i + 1]); i += 1
        elif a == "--idle-interval" and i + 1 < len(args):
            cfg["idle_interval"] = int(args[i + 1]); i += 1
        elif a == "--no-token-boost":
            cfg["token_boost"] = False
        elif a == "--min-confidence" and i + 1 < len(args):
            cfg["min_confidence"] = float(args[i + 1]); i += 1
        elif not a.startswith("--"):
            cfg["addresses"].append(a)
        i += 1
    return cfg


def _build_dashboard(
    stats: dict,
    monitor: WalletMonitor,
    cfg: dict,
    start_ts: float,
) -> Panel:
    """Build the Rich renderable for the live dashboard."""
    elapsed = time.monotonic() - start_ts
    h, rem = divmod(int(elapsed), 3600)
    m, s = divmod(rem, 60)
    uptime = f"{h:02d}:{m:02d}:{s:02d}"

    credits_used = stats.get("credits_used", 0)
    max_cr = cfg["max_credits"]
    credit_pct = credits_used / max_cr if max_cr else 0
    credit_bar = _bar(credit_pct, width=20)
    credit_color = "green" if credit_pct < 0.6 else "yellow" if credit_pct < 0.85 else "red"

    # Stats row
    stats_table = Table.grid(padding=(0, 2))
    stats_table.add_column()
    stats_table.add_column()
    stats_table.add_row(
        f"[bold]Uptime[/bold]  {uptime}",
        f"[bold]Credits[/bold]  [{credit_color}]{credits_used}/{max_cr}[/{credit_color}] {credit_bar}",
    )
    stats_table.add_row(
        f"[bold]Polls[/bold]   {stats.get('polls', 0)}",
        f"[bold]New TXs[/bold] {stats.get('new_txs', 0)}",
    )
    stats_table.add_row(
        f"[bold]Block scans[/bold] {stats.get('block_scans', 0)}",
        f"[bold]Candidates[/bold] [{'green' if stats.get('candidates') else 'dim'}]{stats.get('candidates', 0)}[/{'green' if stats.get('candidates') else 'dim'}]",
    )

    # Recent events feed
    feed = Table(box=None, show_header=False, padding=(0, 1))
    feed.add_column("Time", style="dim", width=8)
    feed.add_column("Event")
    for evt in reversed(_recent_events[-_MAX_FEED:]):
        feed.add_row(evt["time"], evt["msg"])

    if not _recent_events:
        feed.add_row("", "[dim]Waiting for new transactions…[/dim]")

    layout = Table.grid()
    layout.add_column()
    layout.add_row(stats_table)
    layout.add_row("")
    layout.add_row("[bold dim]Recent Activity[/bold dim]")
    layout.add_row(feed)

    return Panel(layout, title="[bold cyan]Wallet Discovery Daemon[/bold cyan]",
                 border_style="cyan")


def _bar(pct: float, width: int = 20) -> str:
    filled = int(pct * width)
    return f"[{'green' if pct < 0.6 else 'yellow' if pct < 0.85 else 'red'}]{'█' * filled}{'░' * (width - filled)}[/]"


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    _recent_events.append({"time": ts, "msg": msg})


async def _on_candidate(wallet_addr: str, candidates: list[MatchResult]):
    """Called whenever the monitor finds new match candidates."""
    registry = None  # We don't need registry here, just log it

    for c in candidates:
        conf_color = "green" if c.confidence >= 0.8 else "yellow" if c.confidence >= 0.6 else "dim"
        label = f" [dim]({c.known_label})[/dim]" if c.known_label else ""
        token_tag = " [cyan]+token[/cyan]" if c.evidence.get("token_match") else ""
        _log(
            f"[{conf_color}]MATCH {c.confidence:.0%}[/{conf_color}] "
            f"[cyan]{c.address[:12]}…[/cyan]{label}{token_tag} "
            f"← [dim]{wallet_addr[:12]}…[/dim]"
        )


async def run(cfg: dict):
    _check_setup()
    init_db()
    registry = WalletRegistry()

    # Determine which addresses to monitor
    if cfg["cluster"]:
        wallets = registry.by_name(cfg["cluster"])
        if not wallets:
            console.print(f"[red]Cluster not found:[/red] {cfg['cluster']}")
            sys.exit(1)
        addresses = [w.address for w in wallets]
        _log(f"Monitoring cluster '{cfg['cluster']}' ({len(addresses)} wallets)")
    elif cfg["addresses"]:
        addresses = cfg["addresses"]
        _log(f"Monitoring {len(addresses)} specified wallets")
    else:
        addresses = list(registry.all_addresses())
        _log(f"Monitoring all {len(addresses)} tracked wallets")

    monitor_cfg = MonitorConfig(
        poll_interval_active=cfg["interval"],
        poll_interval_idle=cfg["idle_interval"],
        enable_token_boost=cfg["token_boost"],
    )

    client = HeliusClient()
    monitor = WalletMonitor(
        registry=registry,
        client=client,
        config=monitor_cfg,
        on_candidate=_on_candidate,
    )

    _log(
        f"[dim]Intervals: active={cfg['interval']}s idle={cfg['idle_interval']}s "
        f"token-boost={'on' if cfg['token_boost'] else 'off'}[/dim]"
    )

    start_ts = time.monotonic()
    last_stats: dict = {}

    def on_tick(stats: dict):
        nonlocal last_stats
        last_stats = stats

    try:
        with Live(
            _build_dashboard(last_stats, monitor, cfg, start_ts),
            console=console,
            refresh_per_second=2,
            screen=True,
        ) as live:
            async def update_live():
                while not monitor._stop:
                    live.update(_build_dashboard(last_stats, monitor, cfg, start_ts))
                    await asyncio.sleep(0.5)

            await asyncio.gather(
                monitor.run_forever(addresses=addresses, on_tick=on_tick),
                update_live(),
            )
    except KeyboardInterrupt:
        monitor.stop()
        console.print("\n[yellow]Stopped.[/yellow]")
    finally:
        await client.close()

    # Summary
    total = get_candidates(min_confidence=cfg["min_confidence"])
    console.print(
        f"\n[bold]Session complete.[/bold] "
        f"Credits used: {client.credits_used}  |  "
        f"Candidates in DB: {len(total)}"
    )
    console.print(
        "[dim]View candidates:[/dim] python discover.py candidates"
    )


def main():
    cfg = _parse_args()
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
