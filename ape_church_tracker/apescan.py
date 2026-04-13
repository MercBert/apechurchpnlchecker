"""Thin client for the Etherscan v2 Multichain API (which serves Apescan).

Etherscan consolidated all its chain-family APIs — including Apescan — into
a single multichain v2 endpoint. You request a specific chain by passing
`chainid` in every call; Ape Chain is 33139. The API key you generate at
https://apescan.io/myapikey is an Etherscan key that works for every chain
in the family (Ethereum, Apescan, Basescan, etc.).

Endpoints we call:
  - `account.txlist`            — external txs for an address
  - `account.txlistinternal`    — internal txs for an address OR a single txhash
  - `contract.getcontractcreation`

All return JSON of shape:
    {"status": "1"|"0", "message": ..., "result": [...]}

`status == "0"` with `message` like "No transactions found" is *not* an error —
it just means the address/range is empty. We translate that to `[]`.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


log = logging.getLogger(__name__)


APECHAIN_CHAIN_ID = "33139"

# Cloudflare in front of etherscan blocks requests with a default urllib UA.
_USER_AGENT = (
    "ape-church-tracker/0.1 (+https://github.com/MercBert/apechurchpnlchecker)"
)


@dataclass
class ApescanTx:
    block_number: int
    timestamp: int
    tx_hash: str
    from_addr: str
    to_addr: str
    value_wei: int
    is_error: bool
    input_data: str


@dataclass
class ApescanInternalTx:
    block_number: int
    timestamp: int
    parent_tx_hash: str
    from_addr: str
    to_addr: str
    value_wei: int
    trace_id: str
    is_error: bool


class ApescanError(RuntimeError):
    pass


class Apescan:
    def __init__(
        self,
        base_url: str = "https://api.etherscan.io/v2/api",
        api_key: str = "",
        chain_id: str = APECHAIN_CHAIN_ID,
        rate_sleep: float = 0.25,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("?&")
        self.api_key = api_key or ""
        self.chain_id = chain_id
        self.rate_sleep = rate_sleep
        self.timeout = timeout
        self._last_call: float = 0.0

    # ---- plumbing --------------------------------------------------------

    def _request(self, params: Dict[str, str]) -> Any:
        # Always include chainid for the v2 multichain endpoint.
        merged = {"chainid": self.chain_id, **params}
        if self.api_key:
            merged["apikey"] = self.api_key
        query = urllib.parse.urlencode(merged)
        url = f"{self.base_url}?{query}"

        # simple client-side rate limit
        delta = time.monotonic() - self._last_call
        if delta < self.rate_sleep:
            time.sleep(self.rate_sleep - delta)

        last_exc: Optional[Exception] = None
        for attempt in range(5):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = resp.read()
                self._last_call = time.monotonic()
                break
            except Exception as exc:  # network blip / 403 / 429 / timeout
                last_exc = exc
                wait = 2 ** attempt
                log.warning("apescan request failed (%s); retrying in %.1fs", exc, wait)
                time.sleep(wait)
        else:
            raise ApescanError(
                f"apescan request failed after retries: {last_exc} url={url}"
            )

        import json  # local import keeps top-level import list short

        body = json.loads(data)
        if body.get("status") == "1":
            return body.get("result", [])
        # empty-range is reported as status=0 message="No transactions found"
        msg = str(body.get("message", "")).lower()
        if msg in {"no transactions found", "no records found"}:
            return []
        raise ApescanError(
            f"apescan error: status={body.get('status')} message={body.get('message')} "
            f"result={body.get('result')} url={url}"
        )

    # ---- endpoints -------------------------------------------------------

    def txlist(self, address: str, start_block: int, end_block: int) -> List[ApescanTx]:
        """External transactions where `address` is from or to."""
        result = self._request(
            {
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": str(start_block),
                "endblock": str(end_block),
                "sort": "asc",
            }
        )
        return [
            ApescanTx(
                block_number=int(r["blockNumber"]),
                timestamp=int(r["timeStamp"]),
                tx_hash=r["hash"].lower(),
                from_addr=(r.get("from") or "").lower(),
                to_addr=(r.get("to") or "").lower(),
                value_wei=int(r.get("value", "0") or "0"),
                is_error=str(r.get("isError", "0")) != "0",
                input_data=r.get("input", ""),
            )
            for r in result
        ]

    def txlistinternal(
        self,
        address: Optional[str] = None,
        tx_hash: Optional[str] = None,
        start_block: int = 0,
        end_block: int = 99999999,
    ) -> List[ApescanInternalTx]:
        """Internal transactions — either for a single tx hash, or all internal
        txs where `address` is from/to within [start_block, end_block]."""
        params: Dict[str, str] = {
            "module": "account",
            "action": "txlistinternal",
        }
        if tx_hash is not None:
            params["txhash"] = tx_hash
        else:
            if address is None:
                raise ValueError("must supply address or tx_hash")
            params["address"] = address
            params["startblock"] = str(start_block)
            params["endblock"] = str(end_block)
            params["sort"] = "asc"

        result = self._request(params)
        return [
            ApescanInternalTx(
                block_number=int(r["blockNumber"]),
                timestamp=int(r["timeStamp"]),
                parent_tx_hash=(r.get("hash") or tx_hash or "").lower(),
                from_addr=(r.get("from") or "").lower(),
                to_addr=(r.get("to") or "").lower(),
                value_wei=int(r.get("value", "0") or "0"),
                trace_id=str(r.get("traceId", "")),
                is_error=str(r.get("isError", "0")) != "0",
            )
            for r in result
        ]

    def contract_creation_block(self, address: str) -> Optional[int]:
        """Look up the block a contract was deployed at.

        Uses `contract.getcontractcreation` when available, otherwise falls
        back to the first tx in `txlist`.
        """
        try:
            result = self._request(
                {
                    "module": "contract",
                    "action": "getcontractcreation",
                    "contractaddresses": address,
                }
            )
            if result:
                tx = result[0].get("txHash") or result[0].get("transactionHash")
                if tx:
                    first = self._request(
                        {
                            "module": "proxy",
                            "action": "eth_getTransactionByHash",
                            "txhash": tx,
                        }
                    )
                    if isinstance(first, dict) and "blockNumber" in first:
                        return int(first["blockNumber"], 16)
        except ApescanError as exc:
            log.warning("getcontractcreation failed: %s", exc)

        # Fallback: first external tx involving the address.
        txs = self.txlist(address, 0, 99999999)
        return txs[0].block_number if txs else None
