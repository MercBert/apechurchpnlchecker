"""Indexer: backfill + live-tail loop.

Usage:
    python -m ape_church_tracker.indexer                 # all games
    python -m ape_church_tracker.indexer --game blizzard_blitz
    python -m ape_church_tracker.indexer --relabel       # no RPC, relabel only
    python -m ape_church_tracker.indexer --once          # backfill to head and exit
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict
from typing import Dict, Iterable, List, Optional, Tuple

from eth_utils import to_checksum_address
from rich.logging import RichHandler
from sqlalchemy.engine import Engine
from web3 import Web3

from .apescan import Apescan
from .config import GameConfig, Settings, load_all_game_configs, load_game_config, load_settings
from .db import (
    block_reconciliation as recon_table,
    flows as flows_table,
    game_events as game_events_table,
    get_engine,
    get_last_block,
    insert_many_ignore,
    raw_events as raw_events_table,
    relabel_flows,
    set_last_block,
    upsert_labels,
)
from .decoder import (
    ERC20_TRANSFER_TOPIC,
    DecodedEvent,
    Flow,
    build_topic_index,
    classify_flows,
    classify_native_flows,
    decode_game_events,
    extract_erc20_flows,
)
from .rpc import make_web3, with_retry


log = logging.getLogger("indexer")


# ---------- log fetching ----------


def _pad_address_topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower().replace("0x", "")


def fetch_candidate_txs(
    w3: Web3,
    cfg: GameConfig,
    from_block: int,
    to_block: int,
) -> List[str]:
    """Return the list of unique tx hashes in [from_block, to_block] that touch
    the game contract either via its own events or via ERC-20 Transfers where
    the contract is sender or receiver.
    """
    tx_hashes: set[str] = set()

    # 1) Logs emitted by the game contract.
    game_logs = with_retry(
        lambda: w3.eth.get_logs(
            {
                "address": to_checksum_address(cfg.address),
                "fromBlock": from_block,
                "toBlock": to_block,
            }
        )
    )
    for lg in game_logs:
        tx_hashes.add(lg["transactionHash"].hex() if hasattr(lg["transactionHash"], "hex") else str(lg["transactionHash"]))

    # 2) ERC-20 Transfer logs where the game contract is from or to, per token.
    padded = _pad_address_topic(cfg.address)
    for token in cfg.erc20_tokens():
        for topics in (
            [ERC20_TRANSFER_TOPIC, padded, None],
            [ERC20_TRANSFER_TOPIC, None, padded],
        ):
            logs_ = with_retry(
                lambda: w3.eth.get_logs(
                    {
                        "address": to_checksum_address(token),
                        "fromBlock": from_block,
                        "toBlock": to_block,
                        "topics": topics,
                    }
                )
            )
            for lg in logs_:
                h = lg["transactionHash"]
                tx_hashes.add(h.hex() if hasattr(h, "hex") else str(h))

    return sorted(tx_hashes)


# ---------- per-tx processing ----------


def _log_topic_hex(t) -> str:
    if isinstance(t, (bytes, bytearray)):
        return "0x" + t.hex()
    return str(t)


def _log_data_hex(d) -> str:
    if isinstance(d, (bytes, bytearray)):
        return "0x" + d.hex()
    return str(d)


def process_tx(
    w3: Web3,
    cfg: GameConfig,
    tx_hash: str,
    topic_index: Dict[str, dict],
    label_map: Dict[str, dict],
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Fetch the receipt, build the flow/event/raw rows.

    Block-level reconciliation is done separately in `reconcile_blocks` after
    all txs in a window have been processed.

    Returns (raw_rows, event_rows, flow_rows).
    """
    receipt = with_retry(lambda: w3.eth.get_transaction_receipt(tx_hash))
    logs = receipt["logs"]
    block_number = int(receipt["blockNumber"])

    block = with_retry(lambda: w3.eth.get_block(block_number))
    ts = int(block["timestamp"])

    decoded = decode_game_events(logs, cfg.address, topic_index)
    raw_flow_tuples = extract_erc20_flows(
        logs,
        cfg.address,
        known_tokens=cfg.erc20_tokens(),
    )
    flows_ = classify_flows(
        raw_flow_tuples,
        decoded,
        tx_hash=tx_hash,
        ts=ts,
        label_map=label_map,
        event_names={
            "wager": cfg.events.wager,
            "payout": cfg.events.payout,
        },
    )

    # --- raw_events rows (only logs on the game contract itself) ---
    raw_rows: List[dict] = []
    for lg in logs:
        if lg["address"].lower() != cfg.address.lower():
            continue
        raw_rows.append(
            {
                "game": cfg.name,
                "tx_hash": tx_hash,
                "log_index": int(lg["logIndex"]),
                "block_number": block_number,
                "ts": ts,
                "address": lg["address"].lower(),
                "topics_json": json.dumps([_log_topic_hex(t) for t in lg["topics"]]),
                "data": _log_data_hex(lg["data"]),
            }
        )

    # --- game_events rows ---
    event_rows = [
        {
            "game": cfg.name,
            "tx_hash": ev.tx_hash,
            "log_index": ev.log_index,
            "block_number": ev.block_number,
            "ts": ts,
            "event_name": ev.event_name,
            "args_json": json.dumps(_stringify(ev.args)),
        }
        for ev in decoded
    ]

    # --- flow rows ---
    flow_rows = [
        {
            "game": cfg.name,
            "tx_hash": f.tx_hash,
            "log_index": f.log_index,
            "block_number": f.block_number,
            "ts": ts,
            "direction": f.direction,
            "token": f.token,
            "amount_raw": str(f.amount_raw),
            "counterparty": f.counterparty,
            "category": f.category,
            "label_name": f.label_name,
        }
        for f in flows_
    ]

    return raw_rows, event_rows, flow_rows


def reconcile_blocks(
    w3: Web3,
    cfg: GameConfig,
    flow_rows: List[dict],
) -> List[dict]:
    """Aggregate flows by (block, token), compare against on-chain balance delta.

    Native flows are recorded as SKIPPED because log-based extraction can't see
    native moves; trace support is left for a future iteration.
    """
    # group sums per (block, token) -> (s_in, s_out)
    agg: Dict[Tuple[int, str], List[int]] = {}
    for row in flow_rows:
        key = (int(row["block_number"]), row["token"])
        s_in, s_out = agg.get(key, [0, 0])
        amt = int(row["amount_raw"])
        if row["direction"] == "in":
            s_in += amt
        else:
            s_out += amt
        agg[key] = [s_in, s_out]

    out: List[dict] = []
    for (block_number, token), (s_in, s_out) in agg.items():
        if token == "native":
            out.append(
                {
                    "game": cfg.name,
                    "block_number": block_number,
                    "token": token,
                    "sum_in_raw": str(s_in),
                    "sum_out_raw": str(s_out),
                    "balance_delta_raw": "SKIPPED",
                    "ok": 1,
                }
            )
            continue
        delta = _erc20_balance_delta(w3, token, cfg.address, block_number)
        ok = 1 if (s_in - s_out) == delta else 0
        out.append(
            {
                "game": cfg.name,
                "block_number": block_number,
                "token": token,
                "sum_in_raw": str(s_in),
                "sum_out_raw": str(s_out),
                "balance_delta_raw": str(delta),
                "ok": ok,
            }
        )
    return out


def _stringify(args: Dict[str, object]) -> Dict[str, object]:
    out: Dict[str, object] = {}
    for k, v in args.items():
        if isinstance(v, int):
            out[k] = str(v)  # keep int precision across JSON
        elif isinstance(v, (bytes, bytearray)):
            out[k] = "0x" + v.hex()
        else:
            out[k] = v
    return out


# ERC-20 `balanceOf(address)` call data prefix
_BALANCE_OF_SELECTOR = bytes.fromhex("70a08231")


def _erc20_balance_delta(w3: Web3, token: str, holder: str, block_number: int) -> int:
    holder_padded = bytes.fromhex("0" * 24 + holder.lower().replace("0x", ""))
    data = _BALANCE_OF_SELECTOR + holder_padded
    token_cs = to_checksum_address(token)

    def _call(blk: int) -> int:
        result = w3.eth.call(
            {"to": token_cs, "data": "0x" + data.hex()},
            block_identifier=blk,
        )
        raw = bytes(result)
        if not raw:
            return 0
        return int.from_bytes(raw[-32:], "big")

    before = with_retry(lambda: _call(block_number - 1))
    after = with_retry(lambda: _call(block_number))
    return after - before


# ---------- main loop ----------


def backfill_game(
    w3: Web3,
    engine: Engine,
    cfg: GameConfig,
    settings: Settings,
    once: bool = False,
) -> None:
    if cfg.is_placeholder:
        log.warning("game %s still has placeholder address, skipping", cfg.name)
        return

    upsert_labels(engine, cfg.name, {k: v.model_dump() for k, v in cfg.labels.items() if not k.startswith("0xFILL_ME")})
    label_map = {k: v for k, v in {addr.lower(): meta.model_dump() for addr, meta in cfg.labels.items() if not addr.startswith("0xFILL_ME")}.items()}
    topic_index = build_topic_index(cfg.abi)

    last = get_last_block(engine, cfg.name)
    from_block = (last + 1) if last is not None else cfg.start_block
    window = settings.indexer_window

    while True:
        head = with_retry(lambda: w3.eth.block_number)
        if from_block > head:
            if once:
                return
            time.sleep(settings.tail_interval)
            continue

        to_block = min(from_block + window - 1, head)
        log.info("[%s] scanning blocks %d..%d (head=%d)", cfg.name, from_block, to_block, head)
        try:
            tx_hashes = fetch_candidate_txs(w3, cfg, from_block, to_block)
        except Exception as exc:
            log.error("get_logs failed: %s; halving window", exc)
            window = max(50, window // 2)
            continue

        raws: List[dict] = []
        evs: List[dict] = []
        fls: List[dict] = []
        for tx_hash in tx_hashes:
            try:
                r, e, f = process_tx(w3, cfg, tx_hash, topic_index, label_map)
            except Exception as exc:
                log.exception("failed to process tx %s: %s", tx_hash, exc)
                continue
            raws.extend(r)
            evs.extend(e)
            fls.extend(f)

        try:
            recs = reconcile_blocks(w3, cfg, fls)
        except Exception as exc:
            log.exception("reconcile_blocks failed: %s", exc)
            recs = []

        with engine.begin() as conn:
            insert_many_ignore(conn, raw_events_table, raws)
            insert_many_ignore(conn, game_events_table, evs)
            insert_many_ignore(conn, flows_table, fls)
            insert_many_ignore(conn, recon_table, recs)
            set_last_block(conn, cfg.name, to_block)

        unlabeled = sum(1 for f in fls if f["category"].startswith("UNLABELED"))
        if fls or evs:
            log.info(
                "[%s] %d txs, %d flows (%d UNLABELED), %d events, %d recon rows",
                cfg.name, len(tx_hashes), len(fls), unlabeled, len(evs), len(recs),
            )

        from_block = to_block + 1
        window = min(settings.indexer_window, int(window * 1.5) + 1)


# ---------- native-APE indexing (apescan-backed) ----------------------------


def _native_balance_delta(w3: Web3, holder: str, block_number: int) -> int:
    holder_cs = to_checksum_address(holder)
    before = with_retry(lambda: w3.eth.get_balance(holder_cs, block_identifier=block_number - 1))
    after = with_retry(lambda: w3.eth.get_balance(holder_cs, block_identifier=block_number))
    return int(after) - int(before)


def backfill_native_game(
    w3: Web3,
    engine: Engine,
    cfg: GameConfig,
    settings: Settings,
    once: bool = False,
) -> None:
    """Index a native-APE game via Apescan's txlist + txlistinternal.

    Workflow per range:
      1. `txlist(game)` → every external tx where game is to/from. The `value`
         field is the player's wager (`msg.value`).
      2. `txlistinternal(game)` → every internal call where game is to/from.
         These are the fee and payout outflows.
      3. Join on `tx_hash`, build Flow rows via `classify_native_flows`.
      4. Block-level reconciliation via `eth_getBalance`.
    """
    if cfg.is_placeholder:
        log.warning("game %s still has placeholder address, skipping", cfg.name)
        return

    label_map = {
        addr.lower(): meta.model_dump()
        for addr, meta in cfg.labels.items()
        if not addr.startswith("0xFILL_ME")
    }
    upsert_labels(engine, cfg.name, label_map)

    apescan = Apescan(
        base_url=settings.apescan_base_url,
        api_key=settings.apescan_api_key,
    )

    last = get_last_block(engine, cfg.name)
    if last is not None:
        from_block = last + 1
    elif cfg.start_block > 0:
        from_block = cfg.start_block
    else:
        discovered = apescan.contract_creation_block(cfg.address)
        if discovered is None:
            log.error("[%s] could not discover deploy block", cfg.name)
            return
        log.info("[%s] discovered deploy block: %d", cfg.name, discovered)
        from_block = discovered

    window = settings.indexer_window

    while True:
        head = with_retry(lambda: w3.eth.block_number)
        if from_block > head:
            if once:
                return
            time.sleep(settings.tail_interval)
            continue

        to_block = min(from_block + window - 1, head)
        log.info("[%s] scanning blocks %d..%d (head=%d)", cfg.name, from_block, to_block, head)

        try:
            ext_txs = apescan.txlist(cfg.address, from_block, to_block)
            int_txs = apescan.txlistinternal(
                address=cfg.address, start_block=from_block, end_block=to_block
            )
        except Exception as exc:
            log.error("apescan fetch failed: %s; halving window", exc)
            window = max(50, window // 2)
            continue

        # Index txs by hash: for each tx we need value/from/to + list of internals.
        per_tx: Dict[str, Dict[str, object]] = {}
        for t in ext_txs:
            if t.is_error:
                continue
            per_tx[t.tx_hash] = {
                "block": t.block_number,
                "ts": t.timestamp,
                "tx_from": t.from_addr,
                "tx_to": t.to_addr,
                "value": t.value_wei,
                "internals": [],
            }
        for it in int_txs:
            if it.is_error:
                continue
            rec = per_tx.setdefault(
                it.parent_tx_hash,
                {
                    "block": it.block_number,
                    "ts": it.timestamp,
                    "tx_from": "",
                    "tx_to": "",
                    "value": 0,
                    "internals": [],
                },
            )
            rec["internals"].append((it.trace_id, it.from_addr, it.to_addr, it.value_wei))

        fls: List[dict] = []
        for tx_hash, rec in per_tx.items():
            flows_native = classify_native_flows(
                tx_hash=tx_hash,
                block_number=int(rec["block"]),
                tx_from=str(rec["tx_from"]),
                tx_to=str(rec["tx_to"]),
                tx_value_wei=int(rec["value"]),
                internal_txs=list(rec["internals"]),  # type: ignore[arg-type]
                game_address=cfg.address,
                label_map=label_map,
            )
            for f in flows_native:
                fls.append(
                    {
                        "game": cfg.name,
                        "tx_hash": f.tx_hash,
                        "log_index": f.log_index,
                        "block_number": f.block_number,
                        "ts": int(rec["ts"]),
                        "direction": f.direction,
                        "token": f.token,
                        "amount_raw": str(f.amount_raw),
                        "counterparty": f.counterparty,
                        "category": f.category,
                        "label_name": f.label_name,
                    }
                )

        # Block-level reconciliation for native: per block, sum(in) - sum(out)
        # must equal the contract's native balance delta across that block.
        recs: List[dict] = []
        per_block: Dict[int, List[int]] = {}
        for r in fls:
            blk = int(r["block_number"])
            s_in, s_out = per_block.get(blk, [0, 0])
            amt = int(r["amount_raw"])
            if r["direction"] == "in":
                s_in += amt
            else:
                s_out += amt
            per_block[blk] = [s_in, s_out]
        for blk, (s_in, s_out) in per_block.items():
            try:
                delta = _native_balance_delta(w3, cfg.address, blk)
            except Exception as exc:
                log.warning("balance delta failed for block %d: %s", blk, exc)
                continue
            ok = 1 if (s_in - s_out) == delta else 0
            recs.append(
                {
                    "game": cfg.name,
                    "block_number": blk,
                    "token": "native",
                    "sum_in_raw": str(s_in),
                    "sum_out_raw": str(s_out),
                    "balance_delta_raw": str(delta),
                    "ok": ok,
                }
            )

        with engine.begin() as conn:
            insert_many_ignore(conn, flows_table, fls)
            insert_many_ignore(conn, recon_table, recs)
            set_last_block(conn, cfg.name, to_block)

        unlabeled = sum(1 for f in fls if f["category"].startswith("UNLABELED"))
        if fls:
            log.info(
                "[%s] %d txs, %d flows (%d UNLABELED), %d recon rows",
                cfg.name, len(per_tx), len(fls), unlabeled, len(recs),
            )

        from_block = to_block + 1
        window = min(settings.indexer_window, int(window * 1.5) + 1)


def run_relabel(settings: Settings, game_name: Optional[str]) -> None:
    engine = get_engine(settings.db_path)
    configs = [load_game_config(game_name)] if game_name else load_all_game_configs()
    for cfg in configs:
        label_map = {
            addr.lower(): meta.model_dump()
            for addr, meta in cfg.labels.items()
            if not addr.startswith("0xFILL_ME")
        }
        upsert_labels(engine, cfg.name, {addr: meta for addr, meta in label_map.items()})
        updated = relabel_flows(engine, cfg.name, label_map)
        log.info("[%s] relabeled %d flows", cfg.name, updated)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(rich_tracebacks=True, show_time=True, show_path=False)],
    )

    ap = argparse.ArgumentParser()
    ap.add_argument("--game", help="run only this game (by config name)")
    ap.add_argument("--once", action="store_true", help="catch up to head and exit")
    ap.add_argument("--relabel", action="store_true", help="rewrite flow categories from config and exit")
    args = ap.parse_args()

    settings = load_settings()

    if args.relabel:
        run_relabel(settings, args.game)
        return

    engine = get_engine(settings.db_path)
    w3 = make_web3(settings.rpc_url)
    if not w3.is_connected():
        log.error("could not connect to RPC %s", settings.rpc_url)
        return

    configs = [load_game_config(args.game)] if args.game else load_all_game_configs()
    if not configs:
        log.error("no game configs found in %s", settings.config_dir)
        return

    for cfg in configs:
        if cfg.native_only:
            backfill_native_game(w3, engine, cfg, settings, once=args.once)
        else:
            backfill_game(w3, engine, cfg, settings, once=args.once)


if __name__ == "__main__":
    main()
