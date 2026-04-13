"""Flow extraction + classification.

Given a tx receipt and a game config, produce:
  - decoded game events (from the configured ABI)
  - a list of `Flow` records: every ERC-20 Transfer touching the game contract,
    classified by direction, token, counterparty, and category.

Native-value flows are handled by `trace.py` (optional, RPC-dependent); this
module focuses on log-based extraction which works on any RPC.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from eth_abi import decode as abi_decode
from eth_utils import event_abi_to_log_topic, to_checksum_address
from web3 import Web3


# keccak("Transfer(address,address,uint256)")
ERC20_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)


@dataclass
class DecodedEvent:
    tx_hash: str
    log_index: int
    block_number: int
    address: str
    event_name: str
    args: Dict[str, object]


@dataclass
class Flow:
    tx_hash: str
    log_index: int
    block_number: int
    direction: str   # 'in' | 'out'
    token: str       # 'native' or lowercase ERC-20 address
    amount_raw: int
    counterparty: str  # lowercase
    category: str
    label_name: Optional[str]


def _hex_topic(t) -> str:
    if isinstance(t, bytes):
        return "0x" + t.hex()
    return str(t).lower()


def _addr_from_topic(topic: str) -> str:
    # topics are 32 bytes; the address is the right-most 20.
    h = topic[2:] if topic.startswith("0x") else topic
    return "0x" + h[-40:].lower()


def build_topic_index(abi: List[dict]) -> Dict[str, dict]:
    """Map 0x-prefixed lowercase topic0 → ABI event fragment."""
    out: Dict[str, dict] = {}
    for frag in abi:
        if frag.get("type") != "event" or frag.get("anonymous"):
            continue
        topic0 = "0x" + event_abi_to_log_topic(frag).hex()
        out[topic0.lower()] = frag
    return out


def decode_game_events(
    receipt_logs: List[dict],
    game_address: str,
    topic_index: Dict[str, dict],
) -> List[DecodedEvent]:
    """Decode logs emitted *by the game contract* using the configured ABI."""
    out: List[DecodedEvent] = []
    game_lower = game_address.lower()
    for log in receipt_logs:
        if log["address"].lower() != game_lower:
            continue
        topics = [_hex_topic(t) for t in log["topics"]]
        if not topics:
            continue
        frag = topic_index.get(topics[0])
        if frag is None:
            continue
        args = _decode_event_args(frag, topics, log["data"])
        out.append(
            DecodedEvent(
                tx_hash=_hex(log["transactionHash"]),
                log_index=int(log["logIndex"]),
                block_number=int(log["blockNumber"]),
                address=game_address,
                event_name=frag["name"],
                args=args,
            )
        )
    return out


def _hex(v) -> str:
    if isinstance(v, bytes):
        return "0x" + v.hex()
    return str(v)


def _decode_event_args(frag: dict, topics: List[str], data) -> Dict[str, object]:
    indexed = [i for i in frag["inputs"] if i.get("indexed")]
    non_indexed = [i for i in frag["inputs"] if not i.get("indexed")]
    args: Dict[str, object] = {}
    # indexed args come from topics[1:]
    for i, inp in enumerate(indexed):
        raw = topics[1 + i]
        if inp["type"] == "address":
            args[inp["name"]] = _addr_from_topic(raw)
        else:
            # decode a single-value 32-byte topic
            args[inp["name"]] = int(raw, 16) if inp["type"].startswith(("uint", "int")) else raw
    # non-indexed args live in data blob
    if non_indexed:
        data_bytes = data if isinstance(data, (bytes, bytearray)) else bytes.fromhex(data[2:])
        types = [i["type"] for i in non_indexed]
        values = abi_decode(types, data_bytes)
        for inp, val in zip(non_indexed, values):
            if inp["type"] == "address":
                args[inp["name"]] = str(val).lower()
            elif isinstance(val, bytes):
                args[inp["name"]] = "0x" + val.hex()
            else:
                args[inp["name"]] = val
    return args


def extract_erc20_flows(
    receipt_logs: List[dict],
    game_address: str,
    known_tokens: Iterable[str],
) -> List[Tuple[int, int, str, str, int, str]]:
    """Return tuples for every ERC-20 Transfer log where the game contract is
    sender or receiver. Token filter: only tokens in `known_tokens` (lowercase).

    Each tuple: (log_index, block_number, direction, token, amount_raw, counterparty)
    """
    out: List[Tuple[int, int, str, str, int, str]] = []
    game_lower = game_address.lower()
    known = {t.lower() for t in known_tokens}
    for log in receipt_logs:
        topics = [_hex_topic(t) for t in log["topics"]]
        if not topics or topics[0].lower() != ERC20_TRANSFER_TOPIC:
            continue
        token_addr = log["address"].lower()
        if known and token_addr not in known:
            continue
        if len(topics) < 3:
            continue
        sender = _addr_from_topic(topics[1])
        recipient = _addr_from_topic(topics[2])
        data = log["data"]
        data_bytes = (
            data if isinstance(data, (bytes, bytearray)) else bytes.fromhex(data[2:])
        )
        (amount,) = abi_decode(["uint256"], data_bytes)
        if sender == game_lower:
            out.append(
                (
                    int(log["logIndex"]),
                    int(log["blockNumber"]),
                    "out",
                    token_addr,
                    int(amount),
                    recipient,
                )
            )
        elif recipient == game_lower:
            out.append(
                (
                    int(log["logIndex"]),
                    int(log["blockNumber"]),
                    "in",
                    token_addr,
                    int(amount),
                    sender,
                )
            )
    return out


def classify_flows(
    raw_flows: List[Tuple[int, int, str, str, int, str]],
    decoded_events: List[DecodedEvent],
    tx_hash: str,
    ts: int,
    label_map: Dict[str, dict],
    event_names: Dict[str, Optional[str]],
) -> List[Flow]:
    """Classify each raw flow into a Flow with a category label.

    Strategy:
      - outbound flow whose counterparty address is in `label_map` → use that
        category + label name.
      - outbound flow that matches a `payout`-named game event by (player,
        amount) → category 'payout'.
      - inbound flow that matches a `wager`-named game event by (player,
        amount) → category 'wager'.
      - everything else → 'UNLABELED' (out) or 'UNLABELED_IN' (in).
    """
    wager_event = event_names.get("wager")
    payout_event = event_names.get("payout")

    # Pre-index game events by (event_name, amount) for fast matching. Amount
    # is looked up under common field names.
    def _amount(args: Dict[str, object]) -> Optional[int]:
        for k in ("amount", "value", "wager", "payout", "stake", "win", "amountIn", "amountOut"):
            v = args.get(k)
            if isinstance(v, int):
                return v
        return None

    def _player(args: Dict[str, object]) -> Optional[str]:
        for k in ("player", "user", "account", "from", "to", "winner"):
            v = args.get(k)
            if isinstance(v, str) and v.startswith("0x"):
                return v.lower()
        return None

    wager_matches: Dict[Tuple[str, int], DecodedEvent] = {}
    payout_matches: Dict[Tuple[str, int], DecodedEvent] = {}
    for ev in decoded_events:
        amt = _amount(ev.args)
        player = _player(ev.args)
        if amt is None or player is None:
            continue
        if wager_event and ev.event_name == wager_event:
            wager_matches[(player, amt)] = ev
        if payout_event and ev.event_name == payout_event:
            payout_matches[(player, amt)] = ev

    out: List[Flow] = []
    for log_index, block_number, direction, token, amount, counterparty in raw_flows:
        cp = counterparty.lower()
        category = "UNLABELED" if direction == "out" else "UNLABELED_IN"
        label_name: Optional[str] = None

        if direction == "in" and (cp, amount) in wager_matches:
            category = "wager"
        elif direction == "out" and (cp, amount) in payout_matches:
            category = "payout"
        else:
            lbl = label_map.get(cp)
            if lbl:
                category = lbl["category"]
                label_name = lbl["name"]

        out.append(
            Flow(
                tx_hash=tx_hash,
                log_index=log_index,
                block_number=block_number,
                direction=direction,
                token=token,
                amount_raw=amount,
                counterparty=cp,
                category=category,
                label_name=label_name,
            )
        )

    return out
