"""System health page — one row per tracker showing sync block, flow
count, unlabeled backlog, and reconciliation pass/fail counts. Engineering
audit view to make sure the numbers on the other pages are trustworthy.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ape_church_tracker.aggregates import all_trackers_health
from dashboard_pages._common import get_db_engine


def render() -> None:
    st.title("System health")
    st.caption(
        "One row per tracker. Every new data point you rely on (headline revenue, "
        "per-game totals, house position) is only as trustworthy as the last line "
        "in this table."
    )

    engine = get_db_engine()
    df = all_trackers_health(engine)

    if df.empty:
        st.info(
            "No trackers have synced yet. Start the indexer with:\n"
            "```\npython -m ape_church_tracker.indexer --once\n```"
        )
        return

    # Summary metrics across all trackers
    total_flows = int(df["flow_count"].sum())
    total_unlabeled = int(df["unlabeled_count"].sum())
    total_recon_ok = int(df["recon_ok"].sum())
    total_recon_bad = int(df["recon_bad"].sum())

    cols = st.columns(4)
    cols[0].metric("Total flows indexed", f"{total_flows:,}")
    cols[1].metric(
        "UNLABELED flows",
        f"{total_unlabeled:,}",
        delta="non-zero = labels needed" if total_unlabeled else None,
        delta_color="inverse",
    )
    cols[2].metric("Blocks reconciled", f"{total_recon_ok:,}")
    cols[3].metric(
        "Blocks mismatched",
        f"{total_recon_bad:,}",
        delta="classifier bug" if total_recon_bad else None,
        delta_color="inverse",
    )

    # Per-tracker table
    st.subheader("Per-tracker status")
    display = df.copy()
    display["last_block"] = display["last_block"].apply(lambda v: f"{int(v):,}")
    display["flow_count"] = display["flow_count"].apply(lambda v: f"{int(v):,}")
    display.columns = [
        "tracker",
        "last synced block",
        "flows",
        "unlabeled",
        "blocks ok",
        "blocks mismatched",
    ]
    st.dataframe(display, use_container_width=True, hide_index=True)

    if total_unlabeled:
        st.warning(
            "There are UNLABELED flows. Go to **Games** → pick the tracker → "
            "read the UNLABELED counterparty addresses → add them to "
            "`config/games/<tracker>.json` → run "
            "`python -m ape_church_tracker.indexer --relabel --game <tracker>` "
            "to retag without re-indexing."
        )

    if total_recon_bad:
        st.error(
            "There are reconciliation mismatches. Per block, sum(in) - sum(out) "
            "should equal the contract's on-chain balance delta. A mismatch means "
            "either the classifier missed a flow or the contract's balance changed "
            "via a path we don't index. Investigate before trusting the numbers."
        )
