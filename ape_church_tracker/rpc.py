"""Web3 client factory with simple retry + backoff."""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

from web3 import Web3
from web3.exceptions import Web3Exception


log = logging.getLogger(__name__)

T = TypeVar("T")


def make_web3(rpc_url: str) -> Web3:
    return Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))


def with_retry(fn: Callable[[], T], attempts: int = 5, base_sleep: float = 2.0) -> T:
    """Run `fn` with exponential backoff on transient RPC errors.

    Raises the last exception after exhausting attempts.
    """
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except (Web3Exception, ConnectionError, TimeoutError) as exc:
            last_exc = exc
            sleep = base_sleep * (2 ** i)
            log.warning("rpc error (%s); retry in %.1fs", exc, sleep)
            time.sleep(sleep)
    assert last_exc is not None
    raise last_exc
