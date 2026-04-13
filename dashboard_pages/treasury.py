"""Treasury page — Ape Church Deployer wallet + Treasury multisig.

Shows the two wallets that hold Ape Church's revenue: where the money is
coming in from, where it's going out, net balance change.
"""

from __future__ import annotations

import time

import pandas as pd
import streamlit as st
from sqlalchemy import text

from ape_church_tracker.aggregates import (
    kpis,
    recent_flows,
    sync_status,
)
from dashboard_pages._common import APE_CHURCH_WALLET_TRACKERS, format_ape, get_db_engine, get_games


def render() -> None:
    st.title("Treasury & wallets")
    st.caption("The two Ape Church wallets that hold revenue: Deployer (hot) and Treasury multisig (cold).")

    engine = get_db_engine()
    all_games = get_games()
    wallet_trackers = [
        g for g in all_games if g["name"] in APE_CHURCH_WALLET_TRACKERS
    ]
    if not wallet_trackers:
        st.info(
            "No Ape Church wallet trackers configured. Expected names: "
            + ", ".join(f"`{n}`" for n in APE_CHURCH_WALLET_TRACKERS)
        )
        return

    tabs = st.tabs([g["name"] for g in wallet_trackers])
    for tab, game in zip(tabs, wallet_trackers):
        with tab:
            _render_wallet(engine, game)


def _render_wallet(engine, game: dict) -> None:
    name = game["name"]
    tokens = game["tokens"]
    st.caption(f"Address: `{game['address']}`  •  Last synced block: "
               f"{sync_status(engine, name) or '—'}")

    kpi_df = kpis(engine, name, tokens)
    if kpi_df.empty:
        st.info("No flows recorded yet — the indexer may still be backfilling this tracker.")
        return

    # Net balance change = total in - total out
    total_in = pd.read_sql(
        text("""SELECT token, SUM(CAST(amount_raw AS REAL)) AS r
                FROM flows WHERE game = :g AND direction = 'in' GROUP BY token"""),
        engine, params={"g": name},
    )
    total_out = pd.read_sql(
        text("""SELECT token, SUM(CAST(amount_raw AS REAL)) AS r
                FROM flows WHERE game = :g AND direction = 'out' GROUP BY token"""),
        engine, params={"g": name},
    )

    def _row(df, tkn):
        if df.empty:
            return 0.0
        sub = df[df["token"] == tkn]
        if sub.empty:
            return 0.0
        return float(sub["r"].iloc[0] or 0) / 1e18

    tin = _row(total_in, "native")
    tout = _row(total_out, "native")
    top = st.columns(3)
    top[0].metric("Total APE received", format_ape(tin))
    top[1].metric("Total APE sent out", format_ape(tout))
    top[2].metric("Net APE change", format_ape(tin - tout),
                  delta=f"{(tin - tout) / tin * 100:+.1f}% of in" if tin else None)

    # Inflow sources by category
    inflows = kpi_df[~kpi_df["category"].str.startswith("UNLABELED")]
    st.subheader("Where money comes from")
    in_cats = (
        inflows.groupby("category")["amount"]
        .sum()
        .sort_values(ascending=False)
        .reset_index()
    )
    if not in_cats.empty:
        in_cats["amount"] = in_cats["amount"].apply(format_ape)
        st.dataframe(in_cats, use_container_width=True, hide_index=True)

    # Recent activity
    st.subheader("Recent activity (last 50)")
    recent = recent_flows(engine, name, tokens, limit=50)
    if not recent.empty:
        recent["when"] = pd.to_datetime(recent["ts"], unit="s", utc=True)
        recent["apescan"] = recent["tx_hash"].apply(lambda h: f"https://apescan.io/tx/{h}")
        st.dataframe(
            recent[["when", "direction", "category", "label_name",
                    "token_symbol", "amount", "counterparty", "apescan"]],
            use_container_width=True, hide_index=True,
        )
