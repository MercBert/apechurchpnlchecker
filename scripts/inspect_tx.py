"""Pretty-print every flow for a single tx.

First line of defense: run this against your reference tx *before* the full
backfill. It tells you which counterparty addresses are still UNLABELED so
you can add them to config/games/<game>.json.

Works for both ERC-20 and native-APE games:
  - If `config.native_only`, fetches internal txs via apescan.
  - Otherwise falls back to log-based ERC-20 decoding.

Usage:
    python scripts/inspect_tx.py <game_name> <tx_hash>
"""

from __future__ import annotations

import json
import sys

from rich.console import Console
from rich.table import Table

from ape_church_tracker.apescan import Apescan
from ape_church_tracker.config import load_game_config, load_settings
from ape_church_tracker.decoder import (
    build_topic_index,
    classify_flows,
    classify_native_flows,
    decode_game_events,
    extract_erc20_flows,
)
from ape_church_tracker.rpc import make_web3, with_retry


def _fmt_ape(wei: int, decimals: int = 18) -> str:
    return f"{wei / (10 ** decimals):,.6f}"


def _short(addr: str) -> str:
    return addr[:10] + "…" + addr[-6:] if len(addr) > 16 else addr


def inspect_native(console: Console, cfg, settings, tx_hash: str) -> int:
    w3 = make_web3(settings.rpc_url)
    apescan = Apescan(
        base_url=settings.apescan_base_url,
        api_key=settings.apescan_api_key,
    )

    if not w3.is_connected():
        console.print(f"[red]Could not connect to RPC {settings.rpc_url}[/red]")
        return 1

    tx = with_retry(lambda: w3.eth.get_transaction(tx_hash))
    receipt = with_retry(lambda: w3.eth.get_transaction_receipt(tx_hash))
    block = with_retry(lambda: w3.eth.get_block(int(receipt["blockNumber"])))

    tx_from = tx["from"].lower()
    tx_to = (tx.get("to") or "").lower()
    tx_value = int(tx["value"])

    console.print(
        f"[bold]tx:[/bold] {tx_hash}\n"
        f"[bold]block:[/bold] {int(receipt['blockNumber'])}    "
        f"[bold]timestamp:[/bold] {int(block['timestamp'])}    "
        f"[bold]status:[/bold] {'success' if int(receipt['status']) else 'FAIL'}"
    )
    console.print(
        f"[bold]game contract:[/bold] {cfg.address}\n"
        f"[bold]tx.from (player):[/bold] {tx_from}\n"
        f"[bold]tx.to:[/bold] {tx_to}\n"
        f"[bold]tx.value:[/bold] {_fmt_ape(tx_value)} APE ([yellow]this is the wager[/yellow])"
    )

    # Fetch internal txs via apescan
    console.print("\n[bold]Fetching internal transactions from apescan…[/bold]")
    try:
        internals = apescan.txlistinternal(tx_hash=tx_hash)
    except Exception as exc:
        console.print(f"[red]apescan error: {exc}[/red]")
        if not settings.apescan_api_key:
            console.print(
                "[yellow]No APESCAN_API_KEY set. Get a free key at "
                "https://apescan.io/myapikey and put it in .env.[/yellow]"
            )
        return 1

    console.print(f"Got {len(internals)} internal transactions.")

    itx_table = Table(title="Internal transactions (native APE)")
    itx_table.add_column("#", justify="right")
    itx_table.add_column("direction")
    itx_table.add_column("from")
    itx_table.add_column("to")
    itx_table.add_column("value (APE)", justify="right")
    game_lower = cfg.address.lower()
    for i, it in enumerate(internals):
        direction = ""
        if it.from_addr.lower() == game_lower:
            direction = "OUT"
        elif it.to_addr.lower() == game_lower:
            direction = "IN"
        itx_table.add_row(
            str(i),
            direction,
            _short(it.from_addr),
            _short(it.to_addr),
            _fmt_ape(it.value_wei),
        )
    console.print(itx_table)

    # Build and classify flows
    label_map = {
        addr.lower(): meta.model_dump()
        for addr, meta in cfg.labels.items()
        if not addr.startswith("0xFILL_ME")
    }
    flows = classify_native_flows(
        tx_hash=tx_hash,
        block_number=int(receipt["blockNumber"]),
        tx_from=tx_from,
        tx_to=tx_to,
        tx_value_wei=tx_value,
        internal_txs=[(it.trace_id, it.from_addr, it.to_addr, it.value_wei) for it in internals],
        game_address=cfg.address,
        label_map=label_map,
    )

    fl_table = Table(title=f"Classified flows ({len(flows)})")
    fl_table.add_column("#", justify="right")
    fl_table.add_column("dir")
    fl_table.add_column("amount (APE)", justify="right")
    fl_table.add_column("counterparty")
    fl_table.add_column("category")
    fl_table.add_column("label")
    total_in = 0
    total_out = 0
    unlabeled = 0
    for i, f in enumerate(flows):
        fl_table.add_row(
            str(i),
            f.direction,
            _fmt_ape(f.amount_raw),
            f.counterparty,
            f.category,
            f.label_name or "",
        )
        if f.direction == "in":
            total_in += f.amount_raw
        else:
            total_out += f.amount_raw
        if f.category.startswith("UNLABELED"):
            unlabeled += 1
    console.print(fl_table)

    # Reconciliation
    console.print(
        f"\n[bold]Sum in:[/bold]  {_fmt_ape(total_in)} APE"
    )
    console.print(
        f"[bold]Sum out:[/bold] {_fmt_ape(total_out)} APE"
    )
    console.print(
        f"[bold]Net (stayed in contract):[/bold] {_fmt_ape(total_in - total_out)} APE"
    )
    console.print(
        f"[dim](A losing spin shows positive net — the wager stayed in the house treasury.)[/dim]"
    )

    # Block-level native balance delta check
    try:
        blk = int(receipt["blockNumber"])
        bal_before = with_retry(
            lambda: w3.eth.get_balance(cfg.address, block_identifier=blk - 1)
        )
        bal_after = with_retry(
            lambda: w3.eth.get_balance(cfg.address, block_identifier=blk)
        )
        delta = int(bal_after) - int(bal_before)
        console.print(
            f"[bold]Contract balance delta in block {blk}:[/bold] {_fmt_ape(delta)} APE"
        )
        if delta == (total_in - total_out):
            console.print("[green]Reconciles — every APE is accounted for.[/green]")
        else:
            console.print(
                f"[yellow]Mismatch of {_fmt_ape(delta - (total_in - total_out))} APE.[/yellow] "
                "(Block may contain other txs touching this contract.)"
            )
    except Exception as exc:
        console.print(f"[yellow]balance delta check failed: {exc}[/yellow]")

    # Label guidance
    if unlabeled:
        console.print(
            f"\n[yellow bold]{unlabeled} flow(s) are UNLABELED.[/yellow bold]"
        )
        console.print(
            "Add entries like this to `config/games/" + cfg.name + ".json` under `labels`:\n"
        )
        seen = set()
        for f in flows:
            if not f.category.startswith("UNLABELED"):
                continue
            if f.counterparty in seen:
                continue
            seen.add(f.counterparty)
            console.print(
                f'  "{f.counterparty}": {{"category": "protocol_fee", "name": "describe this recipient"}},'
            )
        console.print(
            "\nUse category `protocol_fee` for ape.church's cut, `partner_fee` for partners, "
            "`house` for house treasury, `jackpot_pool` for progressive pools, etc.\n"
            f"After editing: python -m ape_church_tracker.indexer --relabel --game {cfg.name}"
        )
    else:
        console.print("\n[green]All flows classified. Ready to run the full backfill.[/green]")
    return 0


def inspect_erc20(console: Console, cfg, settings, tx_hash: str) -> int:
    w3 = make_web3(settings.rpc_url)
    if not w3.is_connected():
        console.print(f"[red]Could not connect to RPC {settings.rpc_url}[/red]")
        return 1

    receipt = with_retry(lambda: w3.eth.get_transaction_receipt(tx_hash))
    block = with_retry(lambda: w3.eth.get_block(int(receipt["blockNumber"])))

    console.print(
        f"[bold]tx:[/bold] {tx_hash}  block {int(receipt['blockNumber'])}  "
        f"ts {int(block['timestamp'])}"
    )
    topic_index = build_topic_index(cfg.abi)
    decoded = decode_game_events(receipt["logs"], cfg.address, topic_index)

    ev_table = Table(title=f"Decoded game events ({len(decoded)})")
    ev_table.add_column("log_idx", justify="right")
    ev_table.add_column("event")
    ev_table.add_column("args", overflow="fold")
    for ev in decoded:
        args_str = json.dumps(
            {k: (str(v) if isinstance(v, (int, bytes)) else v) for k, v in ev.args.items()},
            default=str,
        )
        ev_table.add_row(str(ev.log_index), ev.event_name, args_str)
    console.print(ev_table)

    raw_flows = extract_erc20_flows(
        receipt["logs"], cfg.address, known_tokens=cfg.erc20_tokens()
    )
    label_map = {
        addr.lower(): meta.model_dump()
        for addr, meta in cfg.labels.items()
        if not addr.startswith("0xFILL_ME")
    }
    flows = classify_flows(
        raw_flows,
        decoded,
        tx_hash=tx_hash,
        ts=int(block["timestamp"]),
        label_map=label_map,
        event_names={"wager": cfg.events.wager, "payout": cfg.events.payout},
    )

    fl_table = Table(title=f"ERC-20 flows ({len(flows)})")
    fl_table.add_column("log_idx", justify="right")
    fl_table.add_column("dir")
    fl_table.add_column("token")
    fl_table.add_column("amount_raw", justify="right")
    fl_table.add_column("counterparty")
    fl_table.add_column("category")
    fl_table.add_column("label")
    unlabeled = 0
    for f in flows:
        fl_table.add_row(
            str(f.log_index),
            f.direction,
            _short(f.token),
            str(f.amount_raw),
            _short(f.counterparty),
            f.category,
            f.label_name or "",
        )
        if f.category.startswith("UNLABELED"):
            unlabeled += 1
    console.print(fl_table)

    if unlabeled:
        console.print(f"[yellow]{unlabeled} flows unlabeled; add them to config.[/yellow]")
    else:
        console.print("[green]All flows classified.[/green]")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2

    _, game_name, tx_hash = argv
    console = Console()

    cfg = load_game_config(game_name)
    if cfg.is_placeholder:
        console.print(
            f"[red]Game {game_name} has placeholder address. Fill in "
            f"config/games/{game_name}.json first.[/red]"
        )
        return 1

    settings = load_settings()
    if cfg.native_only:
        return inspect_native(console, cfg, settings, tx_hash)
    return inspect_erc20(console, cfg, settings, tx_hash)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
