"""
Helius API client - credit-aware, rate-limited.

Credit costs (approximate):
  getSignaturesForAddress: 1 credit per call (up to 1000 sigs)
  getTransaction:          1 credit per tx
  getBlock:                ~100 credits (avoid where possible)
  getAccountInfo:          1 credit
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Any

import httpx

from src.config import HELIUS_RPC_URL, HELIUS_API_KEY, MAX_CREDITS_PER_RUN


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
    def __init__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        self._credits_used = 0
        self._last_request = 0.0
        # Helius rate limit: ~10 req/s on free, 50 req/s on paid
        self._min_interval = 0.1

    @property
    def credits_used(self) -> int:
        return self._credits_used

    def _check_budget(self, cost: int = 1):
        if self._credits_used + cost > MAX_CREDITS_PER_RUN:
            raise RuntimeError(
                f"Credit budget exhausted ({self._credits_used}/{MAX_CREDITS_PER_RUN})"
            )

    async def _throttle(self):
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()

    async def _rpc(self, method: str, params: list, cost: int = 1) -> Any:
        self._check_budget(cost)
        await self._throttle()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        resp = await self._client.post(HELIUS_RPC_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
        self._credits_used += cost
        return data.get("result")

    async def get_signatures(
        self,
        address: str,
        limit: int = 100,
        before: Optional[str] = None,
    ) -> list[TxSummary]:
        """Get recent transaction signatures for a wallet. 1 credit."""
        params: list[Any] = [address, {"limit": min(limit, 1000)}]
        if before:
            params[1]["before"] = before
        result = await self._rpc("getSignaturesForAddress", params, cost=1)
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
        """Get full transaction details including fee. 1 credit."""
        result = await self._rpc(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            cost=1,
        )
        return result

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
        """Get account info (SOL balance etc). 1 credit."""
        result = await self._rpc(
            "getAccountInfo",
            [address, {"encoding": "base58"}],
            cost=1,
        )
        return result

    async def close(self):
        await self._client.aclose()
