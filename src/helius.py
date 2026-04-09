"""
Helius API client - credit-aware, rate-limited.

Dual-RPC mode (default):
  getSignaturesForAddress → FREE_RPC_URL  (0 Helius credits)
  getTransaction          → FREE_RPC_URL  (0 Helius credits)
  getBlock                → HELIUS_RPC_URL (~10 credits, cached forever)
  getAccountInfo          → FREE_RPC_URL  (0 Helius credits)

This means Helius credits are only consumed when a non-default fee tx is
detected and we need the full block data — rare, high-value, cached.

Set FREE_RPC_URL in .env to override the free endpoint.
Alternatives: https://rpc.ankr.com/solana
              https://solana-mainnet.g.alchemy.com/v2/demo
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Any

import httpx

from src.config import HELIUS_RPC_URL, HELIUS_API_KEY, FREE_RPC_URL, MAX_CREDITS_PER_RUN


@dataclass
class TxSummary:
    signature: str
    slot: int
    block_time: Optional[int]
    err: Optional[Any]
    fee: Optional[int] = None          # lamports, populated when full tx fetched
    fee_payer: Optional[str] = None


@dataclass
class BlockTx:
    signature: str
    fee: int                            # lamports
    fee_payer: str
    slot: int
    accounts: list[str]


class HeliusClient:
    def __init__(self, use_free_rpc: bool = True):
        """
        Args:
            use_free_rpc: Route signatures/transactions through the free public
                          Solana RPC instead of Helius. Helius credits are then
                          only spent on getBlock calls. Default: True.
        """
        self._client = httpx.AsyncClient(timeout=30.0)
        self._credits_used = 0
        self._use_free_rpc = use_free_rpc

        # Helius rate limit: ~10 req/s on free, 50 req/s on paid
        self._helius_last = 0.0
        self._helius_interval = 0.12   # ~8 req/s — safe for free tier

        # Public RPC: be gentle (~4 req/s to avoid 429s)
        self._free_last = 0.0
        self._free_interval = 0.25

    @property
    def credits_used(self) -> int:
        return self._credits_used

    def _check_budget(self, cost: int = 1):
        if self._credits_used + cost > MAX_CREDITS_PER_RUN:
            raise RuntimeError(
                f"Credit budget exhausted ({self._credits_used}/{MAX_CREDITS_PER_RUN})"
            )

    async def _throttle_helius(self):
        elapsed = time.monotonic() - self._helius_last
        if elapsed < self._helius_interval:
            await asyncio.sleep(self._helius_interval - elapsed)
        self._helius_last = time.monotonic()

    async def _throttle_free(self):
        elapsed = time.monotonic() - self._free_last
        if elapsed < self._free_interval:
            await asyncio.sleep(self._free_interval - elapsed)
        self._free_last = time.monotonic()

    async def _rpc(self, method: str, params: list, cost: int = 1) -> Any:
        """Paid Helius RPC call — counts against credit budget."""
        self._check_budget(cost)
        await self._throttle_helius()
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        resp = await self._client.post(HELIUS_RPC_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
        self._credits_used += cost
        return data.get("result")

    async def _free_rpc(self, method: str, params: list) -> Any:
        """
        Free public RPC call — zero Helius credits.
        Falls back to Helius if the free RPC returns an error or is unreachable.
        """
        await self._throttle_free()
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            resp = await self._client.post(FREE_RPC_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if "error" not in data:
                return data.get("result")
        except Exception:
            pass
        # Fallback to Helius (costs 1 credit)
        return await self._rpc(method, params, cost=1)

    async def get_signatures(
        self,
        address: str,
        limit: int = 100,
        before: Optional[str] = None,
    ) -> list[TxSummary]:
        """
        Get recent transaction signatures for a wallet.
        Uses free RPC by default (0 Helius credits); falls back to Helius.
        """
        params: list[Any] = [address, {"limit": min(limit, 1000)}]
        if before:
            params[1]["before"] = before
        call = self._free_rpc if self._use_free_rpc else lambda m, p: self._rpc(m, p, 1)
        result = await call("getSignaturesForAddress", params)
        if not result:
            return []
        return [
            TxSummary(
                signature=r["signature"],
                slot=r["slot"],
                block_time=r.get("blockTime"),
                err=r.get("err"),
            )
            for r in result
        ]

    async def get_transaction(self, signature: str) -> Optional[dict]:
        """
        Get full transaction details including fee.
        Uses free RPC by default (0 Helius credits); falls back to Helius.
        """
        params = [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
        call = self._free_rpc if self._use_free_rpc else lambda m, p: self._rpc(m, p, 1)
        return await call("getTransaction", params)

    async def get_transaction_fee(self, signature: str) -> Optional[tuple[int, str]]:
        """
        Returns (fee_lamports, fee_payer) for a transaction. 1 credit.
        """
        tx = await self.get_transaction(signature)
        if not tx:
            return None
        fee = tx.get("meta", {}).get("fee")
        accounts = (
            tx.get("transaction", {})
            .get("message", {})
            .get("accountKeys", [])
        )
        fee_payer = None
        for acc in accounts:
            if isinstance(acc, dict):
                if acc.get("signer") and acc.get("writable"):
                    fee_payer = acc.get("pubkey")
                    break
            elif isinstance(acc, str) and fee_payer is None:
                fee_payer = acc
        return (fee, fee_payer) if fee is not None else None

    async def get_block_transactions(self, slot: int) -> list[BlockTx]:
        """
        Get all transactions in a block. Expensive (~10-50 credits).
        Use sparingly - only for confirmed candidate slots.
        """
        self._check_budget(10)
        await self._throttle()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBlock",
            "params": [
                slot,
                {
                    "encoding": "jsonParsed",
                    "transactionDetails": "full",
                    "rewards": False,
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        }
        resp = await self._client.post(HELIUS_RPC_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            return []
        self._credits_used += 10
        block = data.get("result", {})
        txs = block.get("transactions", [])
        results = []
        for tx_data in txs:
            meta = tx_data.get("meta", {})
            fee = meta.get("fee", 0)
            tx = tx_data.get("transaction", {})
            msg = tx.get("message", {})
            accounts = msg.get("accountKeys", [])
            fee_payer = None
            all_accounts = []
            for acc in accounts:
                if isinstance(acc, dict):
                    pubkey = acc.get("pubkey", "")
                    all_accounts.append(pubkey)
                    if acc.get("signer") and acc.get("writable") and fee_payer is None:
                        fee_payer = pubkey
                elif isinstance(acc, str):
                    all_accounts.append(acc)
                    if fee_payer is None:
                        fee_payer = acc
            sigs = tx.get("signatures", [])
            sig = sigs[0] if sigs else ""
            if fee_payer and sig:
                results.append(BlockTx(
                    signature=sig,
                    fee=fee,
                    fee_payer=fee_payer,
                    slot=slot,
                    accounts=all_accounts,
                ))
        return results

    async def get_account_info(self, address: str) -> Optional[dict]:
        """Get account info (SOL balance etc). Uses free RPC by default."""
        params = [address, {"encoding": "base58"}]
        call = self._free_rpc if self._use_free_rpc else lambda m, p: self._rpc(m, p, 1)
        return await call("getAccountInfo", params)

    async def close(self):
        await self._client.aclose()


# ── Helpers (no client needed) ────────────────────────────────────────────────

# Well-known non-token mints to exclude (wrapped SOL is fine to include)
_EXCLUDED_MINTS: frozenset[str] = frozenset({
    "11111111111111111111111111111111",               # System Program
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # Token Program
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bRS", # ATA Program
    "ComputeBudget111111111111111111111111111111",    # Compute Budget
})


def extract_token_mints(tx_data: dict) -> list[str]:
    """
    Extract token mint addresses from a parsed getTransaction response.

    Uses `meta.preTokenBalances` / `meta.postTokenBalances` — the most
    reliable source; falls back to scanning inner instructions for
    `mintTo` / `transfer` parsed data.

    Returns a deduplicated list of mint addresses (excluding known programs).
    """
    if not tx_data:
        return []
    meta = tx_data.get("meta") or {}
    mints: set[str] = set()

    # Primary: token balance change entries contain the mint directly
    for balance_list in (
        meta.get("preTokenBalances") or [],
        meta.get("postTokenBalances") or [],
    ):
        for entry in balance_list:
            mint = entry.get("mint")
            if mint and mint not in _EXCLUDED_MINTS:
                mints.add(mint)

    # Secondary: parsed inner instructions (e.g. Jupiter routes)
    inner = meta.get("innerInstructions") or []
    for block in inner:
        for ix in block.get("instructions") or []:
            parsed = ix.get("parsed") or {}
            info = parsed.get("info") or {}
            mint = info.get("mint")
            if mint and mint not in _EXCLUDED_MINTS:
                mints.add(mint)

    return list(mints)


def extract_token_actions(tx_data: dict, fee_payer: str) -> dict[str, str]:
    """
    Determine whether the fee_payer bought or sold each token in a transaction.

    Uses `meta.preTokenBalances` / `meta.postTokenBalances` with the `owner`
    field to find the fee_payer's token balance changes:
      post > pre  → 'buy'   (received tokens)
      post < pre  → 'sell'  (sent tokens)
      post == 0, not in pre → 'sell' (closed ATA — sold everything)

    Returns {mint_address: 'buy' | 'sell'}
    Only includes mints where a meaningful balance change occurred.
    """
    if not tx_data or not fee_payer:
        return {}

    meta = tx_data.get("meta") or {}
    pre_balances: dict[str, int] = {}
    post_balances: dict[str, int] = {}

    for entry in meta.get("preTokenBalances") or []:
        if entry.get("owner") == fee_payer:
            mint = entry.get("mint")
            amt = entry.get("uiTokenAmount", {}).get("amount", "0")
            if mint and mint not in _EXCLUDED_MINTS:
                try:
                    pre_balances[mint] = int(amt)
                except (ValueError, TypeError):
                    pass

    for entry in meta.get("postTokenBalances") or []:
        if entry.get("owner") == fee_payer:
            mint = entry.get("mint")
            amt = entry.get("uiTokenAmount", {}).get("amount", "0")
            if mint and mint not in _EXCLUDED_MINTS:
                try:
                    post_balances[mint] = int(amt)
                except (ValueError, TypeError):
                    pass

    actions: dict[str, str] = {}
    all_mints = set(pre_balances) | set(post_balances)

    for mint in all_mints:
        pre = pre_balances.get(mint, 0)
        post = post_balances.get(mint, 0)
        if post > pre:
            actions[mint] = "buy"
        elif post < pre:
            actions[mint] = "sell"
        # No change → skip (e.g. token account involved but balance unchanged)

    return actions
