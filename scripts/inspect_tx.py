"""Pretty-print every log + every flow for a single tx.

Use this against the reference tx *before* running the full backfill, to
confirm the ABI decodes wager/payout events and every counterparty is labeled.

Usage:
    python scripts/inspect_tx.py <game_name> <tx_hash>
"""

from __future__ import annotations

import json
import sys

from rich.console import Console
from rich.table import Table

from ape_church_tracker.config import load_game_config, load_settings
from ape_church_tracker.decoder import (
    build_topic_index,
    classify_flows,
    decode_game_events,
    extract_erc20_flows,
)
from ape_church_tracker.rpc import make_web3, with_retry


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2

    _, game_name, tx_hash = argv
    console = Console()

    cfg = load_game_config(game_name)
    if cfg.is_placeholder:
        console.print(
            f"[red]Game {game_name} has placeholder address. Fill in config/games/{game_name}.json first.[/red]"
        )
        return 1

    settings = load_settings()
    w3 = make_web3(settings.rpc_url)
    if not w3.is_connected():
        console.print(f"[red]Could not connect to RPC {settings.rpc_url}[/red]")
        return 1

    receipt = with_retry(lambda: w3.eth.get_transaction_receipt(tx_hash))
    block = with_retry(lambda: w3.eth.get_block(int(receipt["blockNumber"])))

    console.print(
        f"[bold]tx:[/bold] {tx_hash}  "
        f"[bold]block:[/bold] {int(receipt['blockNumber'])}  "
        f"[bold]ts:[/bold] {int(block['timestamp'])}  "
        f"[bold]status:[/bold] {int(receipt['status'])}"
    )
    console.print(
        f"[bold]game contract:[/bold] {cfg.address}  "
        f"[bold]tokens tracked:[/bold] {', '.join(cfg.tokens) or '(none)'}"
    )

    topic_index = build_topic_index(cfg.abi)
    decoded = decode_game_events(receipt["logs"], cfg.address, topic_index)

    ev_table = Table(title=f"Game events decoded via ABI ({len(decoded)})", show_lines=False)
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

    fl_table = Table(title=f"ERC-20 flows touching game contract ({len(flows)})")
    fl_table.add_column("log_idx", justify="right")
    fl_table.add_column("dir")
    fl_table.add_column("token")
    fl_table.add_column("amount_raw", justify="right")
    fl_table.add_column("counterparty")
    fl_table.add_column("category")
    fl_table.add_column("label")
    unlabeled_count = 0
    for f in flows:
        fl_table.add_row(
            str(f.log_index),
            f.direction,
            f.token[:10] + "…" if len(f.token) > 12 else f.token,
            str(f.amount_raw),
            f.counterparty,
            f.category,
            f.label_name or "",
        )
        if f.category.startswith("UNLABELED"):
            unlabeled_count += 1
    console.print(fl_table)

    # Per-token reconciliation
    per_token: dict[str, tuple[int, int]] = {}
    for f in flows:
        s_in, s_out = per_token.get(f.token, (0, 0))
        if f.direction == "in":
            s_in += f.amount_raw
        else:
            s_out += f.amount_raw
        per_token[f.token] = (s_in, s_out)

    rec_table = Table(title="Per-token in/out totals (native and trace skipped)")
    rec_table.add_column("token")
    rec_table.add_column("sum_in", justify="right")
    rec_table.add_column("sum_out", justify="right")
    rec_table.add_column("net", justify="right")
    for tok, (s_in, s_out) in per_token.items():
        rec_table.add_row(tok, str(s_in), str(s_out), str(s_in - s_out))
    console.print(rec_table)

    if unlabeled_count:
        console.print(
            f"[yellow]{unlabeled_count} flow(s) are UNLABELED. Add their counterparty "
            f"addresses to config/games/{game_name}.json under `labels` before backfilling.[/yellow]"
        )
    else:
        console.print("[green]All flows classified.[/green]")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
