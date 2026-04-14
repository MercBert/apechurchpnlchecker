"""Shared helpers for the dashboard pages — engine caching, config
loading, formatting, and the "revenue categories" detection logic that
every page uses to decide what counts as Ape Church revenue.
"""

from __future__ import annotations

import time
from typing import Dict, List, Tuple

import streamlit as st

from ape_church_tracker.aggregates import all_distinct_categories
from ape_church_tracker.config import load_all_game_configs, load_settings
from ape_church_tracker.db import get_engine


# Categories the Executive Summary counts as "Ape Church revenue".
# We pick terminal-destination categories only, never intermediate hops,
# so the same APE isn't counted twice as it flows through multiple trackers.
#
# Correct path for a wager on a Blizzard Blitz-style game:
#   game.ape_church_fee  →  fee_receiver.*  →  deployer.fee_from_ape_church_fee_receiver
#                                            ↘  game_ape_vault.fee_from_ape_church_fee_receiver
#
# We count only the last step (the terminal destination). The earlier
# `ape_church_fee` category on the game contract sees the same money
# passing through FeeReceiver — including it would triple-count.
#
# `nft_royalty` is the equivalent terminal category on the (future)
# NFT royalty tracker.
REVENUE_CATEGORY_PREFIXES: Tuple[str, ...] = (
    "fee_from_ape_church_fee_receiver",
    "nft_royalty",
)

# Categories that represent Ape Church-owned operations (the tracker wallets
# we treat as "ours"). Used to identify trackers to exclude from
# double-counting and highlight on the Treasury / House pages.
APE_CHURCH_WALLET_TRACKERS = ("ape_church_deployer", "ape_church_treasury")


@st.cache_resource
def get_db_engine():
    return get_engine(load_settings().db_path)


@st.cache_data(ttl=15)
def get_games() -> List[dict]:
    """Load every tracker config into a pickleable list of dicts for caching."""
    return [
        {
            "name": c.name,
            "address": c.address,
            "tokens": {k: v.model_dump() for k, v in c.tokens.items()},
            "is_placeholder": c.is_placeholder,
            "self_in_category": c.self_in_category,
            "self_out_category": c.self_out_category,
            "labels": {
                k: v.model_dump() for k, v in c.labels.items()
                if not k.startswith("0xFILL_ME")
            },
        }
        for c in load_all_game_configs()
    ]


@st.cache_data(ttl=15)
def revenue_categories_in_db(_engine_id: int = 0) -> List[str]:
    """Return every DB category whose name starts with a
    REVENUE_CATEGORY_PREFIXES prefix. Cached per 15s to keep pages fast."""
    engine = get_db_engine()
    all_cats = all_distinct_categories(engine)
    return [
        c for c in all_cats
        if any(c.startswith(p) for p in REVENUE_CATEGORY_PREFIXES)
    ]


def format_ape(amount: float) -> str:
    """Pretty-print a number of APE. Handles M/k suffixes."""
    if amount is None:
        return "—"
    if abs(amount) >= 1_000_000:
        return f"{amount / 1_000_000:,.2f}M"
    if abs(amount) >= 1_000:
        return f"{amount / 1_000:,.2f}k"
    return f"{amount:,.4f}"


def time_range_sidebar(default_days: int = 30) -> Tuple[int, int]:
    """Render a days-back slider and return (start_ts, end_ts) in unix seconds."""
    days = st.sidebar.slider("Days back", 1, 365, default_days, key="days_back")
    now = int(time.time())
    return now - days * 86400, now
