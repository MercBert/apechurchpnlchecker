"""Flow extraction + classification.

Two modes:
  - **Log-based** (`extract_erc20_flows`, `classify_flows`): for games that
    move value via ERC-20 Transfer events. Works on any RPC.
  - **Native** (`classify_native_flows`): for games that use `msg.value` and
    distribute fees/payouts via internal transactions. Requires the caller
    to supply a list of internal transactions (from apescan or trace RPC).

Both converge on the same `Flow` dataclass and the same classification rules
(counterparty labels + payout-back-to-sender heuristic).
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


# ---------- native-APE flow classification ----------------------------------


def classify_native_flows(
    tx_hash: str,
    block_number: int,
    tx_from: str,
    tx_to: str,
    tx_value_wei: int,
    internal_txs: List[Tuple[int, str, str, int]],
    game_address: str,
    label_map: Dict[str, dict],
) -> List[Flow]:
    """Build Flow rows for a native-APE tx.

    Parameters
    ----------
    tx_hash, block_number : tx identity
    tx_from, tx_to        : tx `from`/`to` (the caller and the direct callee)
    tx_value_wei          : `msg.value` on the outer call (wei)
    internal_txs          : list of (trace_id, from, to, value_wei) for every
                            internal call where game is sender or receiver.
                            `trace_id` is used to build a deterministic
                            composite log_index so rows are idempotent.
    game_address          : the game contract (checksum or any case)
    label_map             : {lowercase_address: {"category":..., "name":...}}

    Classification:
      - inbound with counterparty == tx_from → category 'wager'
        (the player sent APE as msg.value)
      - outbound with counterparty == tx_from → category 'payout'
        (the contract is sending APE back to the player in the same tx)
      - outbound with counterparty in label_map → use the label's category
      - everything else → 'UNLABELED' / 'UNLABELED_IN'
    """
    game_lower = game_address.lower()
    tx_from_lower = (tx_from or "").lower()
    out: List[Flow] = []

    # 1. Outer `msg.value` — an implicit inbound flow from tx.from to tx.to
    #    (the game contract). We give it a stable, reserved log_index of -1.
    if tx_value_wei > 0 and tx_to.lower() == game_lower:
        category = "wager"  # player → game
        label_name: Optional[str] = None
        # If the sender is actually a labeled address, respect the label.
        lbl = label_map.get(tx_from_lower)
        if lbl:
            category = lbl["category"]
            label_name = lbl["name"]
        out.append(
            Flow(
                tx_hash=tx_hash,
                log_index=-1,  # sentinel for outer msg.value
                block_number=block_number,
                direction="in",
                token="native",
                amount_raw=int(tx_value_wei),
                counterparty=tx_from_lower,
                category=category,
                label_name=label_name,
            )
        )

    # 2. Internal txs where the game contract is from or to.
    for trace_id_raw, ifrom, ito, ivalue in internal_txs:
        if ivalue <= 0:
            continue
        if ifrom.lower() == game_lower:
            direction = "out"
            counterparty = ito.lower()
        elif ito.lower() == game_lower:
            direction = "in"
            counterparty = ifrom.lower()
        else:
            continue

        category = "UNLABELED" if direction == "out" else "UNLABELED_IN"
        label_name = None
        lbl = label_map.get(counterparty)
        if lbl:
            category = lbl["category"]
            label_name = lbl["name"]
        elif direction == "out" and counterparty == tx_from_lower:
            category = "payout"
        elif direction == "in" and counterparty == tx_from_lower:
            category = "wager"

        # trace_id may look like "0", "0_1", "call_3", etc. We encode it
        # into an integer log_index by hashing. Collisions within a single
        # tx are extremely unlikely given typical trace depths, but we use
        # a small offset from -2 downward to keep it distinct from the
        # reserved -1 for msg.value.
        try:
            composite = -(abs(hash(trace_id_raw)) % 1_000_000_000 + 2)
        except Exception:
            composite = -2

        out.append(
            Flow(
                tx_hash=tx_hash,
                log_index=composite,
                block_number=block_number,
                direction=direction,
                token="native",
                amount_raw=int(ivalue),
                counterparty=counterparty,
                category=category,
                label_name=label_name,
            )
        )

    # Deduplicate by (direction, counterparty, amount_raw) — some apescan
    # responses include the same internal tx twice with different trace_ids.
    # Keep the first occurrence and renumber log_index densely so SQLite PK
    # collisions don't happen on re-runs.
    seen = set()
    deduped: List[Flow] = []
    for f in out:
        key = (f.direction, f.counterparty, f.amount_raw)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)

    # Re-assign stable negative log_index by position so PKs are deterministic.
    for i, f in enumerate(deduped):
        f.log_index = -1 - i  # -1, -2, -3, …

    return deduped
