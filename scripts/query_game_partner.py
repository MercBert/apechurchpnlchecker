"""Look up the FeeReceiver-level partner assignment for a game contract.

Ape Church's FeeReceiver has a mapping `gameToPartner(address) -> uint256`
and a `partnerInfo(uint256) -> (name, recipient, cut)`. Together they tell
us whether a game has a partner registered at the FeeReceiver level (System
A in the contract reference skill), and if so, who gets the cut.

Usage:
    python scripts/query_game_partner.py 0x64b27c1c69559A795C98958614398dD7195AE1B8
    python scripts/query_game_partner.py --all    # runs the query for every game config in config/games/

Both the FeeReceiver address and the function selectors are baked in below;
you can override FeeReceiver with --fee-receiver if it ever changes (it's
reachable from GovernanceManager.feeReceiver()).
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Tuple

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import to_checksum_address
from rich.console import Console
from rich.table import Table

from ape_church_tracker.config import load_all_game_configs, load_settings
from ape_church_tracker.rpc import make_web3, with_retry


# Known addresses from .claude/skills/ape-church-contracts/SKILL.md
DEFAULT_FEE_RECEIVER = "0xd208520d57036ad72cffbeccf038c58697da5f51"

# keccak-256 function selectors
# gameToPartner(address) -> uint256
SEL_GAME_TO_PARTNER = bytes.fromhex("c6a39a4d")  # placeholder; resolved at runtime
# partnerInfo(uint256) -> (string, address, uint256)
SEL_PARTNER_INFO = bytes.fromhex("4f36a9d2")  # placeholder; resolved at runtime


def _selector(signature: str) -> bytes:
    """Compute the 4-byte function selector for a canonical Solidity signature."""
    from eth_utils import keccak
    return keccak(text=signature)[:4]


# Resolve at import time — safer than hardcoding.
SEL_GAME_TO_PARTNER = _selector("gameToPartner(address)")
SEL_PARTNER_INFO = _selector("partnerInfo(uint256)")


def query_game_to_partner(w3, fee_receiver: str, game: str) -> int:
    """Call feeReceiver.gameToPartner(game) and return the partner ID (0 if none)."""
    data = SEL_GAME_TO_PARTNER + abi_encode(["address"], [to_checksum_address(game)])
    raw = with_retry(
        lambda: w3.eth.call({"to": to_checksum_address(fee_receiver), "data": "0x" + data.hex()})
    )
    b = bytes(raw)
    if not b:
        return 0
    (partner_id,) = abi_decode(["uint256"], b)
    return int(partner_id)


def query_partner_info(w3, fee_receiver: str, partner_id: int) -> Optional[Tuple[str, str, int]]:
    """Call feeReceiver.partnerInfo(partner_id) and return (name, recipient, cut).

    Returns None if the partner id is empty / unregistered.
    """
    if partner_id == 0:
        return None
    data = SEL_PARTNER_INFO + abi_encode(["uint256"], [partner_id])
    try:
        raw = with_retry(
            lambda: w3.eth.call({"to": to_checksum_address(fee_receiver), "data": "0x" + data.hex()})
        )
    except Exception:
        return None
    b = bytes(raw)
    if not b or len(b) < 32 * 3:
        return None
    try:
        name, recipient, cut = abi_decode(["string", "address", "uint256"], b)
        return str(name), str(recipient).lower(), int(cut)
    except Exception:
        return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "address",
        nargs="?",
        help="Game contract address (omit with --all)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Query every game config in config/games/ and print a table",
    )
    parser.add_argument(
        "--fee-receiver",
        default=DEFAULT_FEE_RECEIVER,
        help=f"FeeReceiver address (default: {DEFAULT_FEE_RECEIVER})",
    )
    args = parser.parse_args(argv[1:])

    console = Console()
    settings = load_settings()
    w3 = make_web3(settings.rpc_url)
    if not w3.is_connected():
        console.print(f"[red]Could not connect to RPC {settings.rpc_url}[/red]")
        return 1

    if args.all:
        configs = load_all_game_configs()
        # Heuristic: a "game" config is one whose self_in_category is 'wager'.
        games = [c for c in configs if getattr(c, "self_in_category", None) == "wager"]
        if not games:
            console.print("[yellow]No game configs found.[/yellow]")
            return 0

        table = Table(title="Game → FeeReceiver partner mapping")
        table.add_column("config")
        table.add_column("address")
        table.add_column("partner_id", justify="right")
        table.add_column("partner_name")
        table.add_column("recipient")
        table.add_column("cut %", justify="right")
        for g in games:
            pid = query_game_to_partner(w3, args.fee_receiver, g.address)
            info = query_partner_info(w3, args.fee_receiver, pid) if pid else None
            if info is None:
                table.add_row(g.name, g.address, str(pid), "-", "-", "-")
            else:
                name, recipient, cut = info
                table.add_row(g.name, g.address, str(pid), name, recipient, str(cut))
        console.print(table)
        return 0

    if not args.address:
        parser.print_help()
        return 2

    pid = query_game_to_partner(w3, args.fee_receiver, args.address)
    console.print(f"[bold]gameToPartner({args.address})[/bold] = [cyan]{pid}[/cyan]")

    if pid == 0:
        console.print("[yellow]No partner registered at FeeReceiver level.[/yellow]")
        console.print(
            "This game either has no partner, or uses a game-level partner "
            "(System B) — check the game contract source for a `partnerAddress` / "
            "`partnerFeeCut` state variable."
        )
        return 0

    info = query_partner_info(w3, args.fee_receiver, pid)
    if info is None:
        console.print(f"[red]partnerInfo({pid}) returned empty[/red]")
        return 1

    name, recipient, cut = info
    console.print(f"[bold]partner name:[/bold]   {name}")
    console.print(f"[bold]recipient:[/bold]      {recipient}")
    console.print(f"[bold]cut:[/bold]            {cut}%")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
