"""Streamlit dashboard for ape-church-tracker.

Shows "where is all the APE going" for each configured game: headline KPIs,
a daily stacked breakdown of outflows by category, per-category totals, an
UNLABELED-flows alert card, a reconciliation panel, and recent activity.

Run:
    streamlit run dashboard.py
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import altair as alt
import pandas as pd
import streamlit as st

from ape_church_tracker.aggregates import (
    daily_breakdown,
    kpis,
    recent_flows,
    reconciliation_summary,
    sync_status,
    unlabeled_counterparties,
)
from ape_church_tracker.config import load_all_game_configs, load_settings
from ape_church_tracker.db import get_engine


st.set_page_config(page_title="ape-church-tracker", layout="wide")


@st.cache_resource
def _engine():
    return get_engine(load_settings().db_path)


@st.cache_data(ttl=15)
def _games() -> List[dict]:
    return [
        {
            "name": c.name,
            "address": c.address,
            "tokens": {k: v.model_dump() for k, v in c.tokens.items()},
            "is_placeholder": c.is_placeholder,
        }
        for c in load_all_game_configs()
    ]


def _fmt_amount(x: float) -> str:
    if x is None:
        return "—"
    if abs(x) >= 1_000_000:
        return f"{x/1_000_000:,.2f}M"
    if abs(x) >= 1_000:
        return f"{x/1_000:,.2f}k"
    return f"{x:,.4f}"


def main() -> None:
    st.title("ape-church-tracker")
    st.caption("flow-first accounting for ape.church games on Ape Chain")

    engine = _engine()
    games = _games()
    if not games:
        st.error("No games configured. Add a file under `config/games/`.")
        return

    with st.sidebar:
        st.header("Controls")
        game_names = [g["name"] for g in games]
        game_name = st.selectbox("Game", game_names)
        game = next(g for g in games if g["name"] == game_name)
        if game["is_placeholder"]:
            st.warning("This game config still has placeholder addresses. Edit `config/games/…` and restart.")

        days_back = st.slider("Days back", 1, 365, 30)
        end_ts = int(time.time())
        start_ts = end_ts - days_back * 86400

        last_block = sync_status(engine, game_name)
        st.metric("Last synced block", f"{last_block:,}" if last_block else "—")
        if st.button("Refresh now"):
            st.cache_data.clear()
            st.rerun()

    tokens = game["tokens"]
    kpi_df = kpis(engine, game_name, tokens)

    # ----- Headline KPIs ------------------------------------------------------
    st.subheader("Lifetime totals")

    def _sum(df: pd.DataFrame, category: str) -> Dict[str, float]:
        sub = df[df["category"] == category]
        return sub.groupby("token_symbol")["amount"].sum().to_dict()

    wagers = _sum(kpi_df, "wager")
    payouts = _sum(kpi_df, "payout")

    # Dynamic per-category totals: every category that isn't wager/payout/UNLABELED
    # gets its own headline metric. This way custom categories the user defines
    # in config.labels (fee_distributor_primary, oracle_fee, etc.) show up here
    # automatically — not just the hardcoded protocol_fee/partner_fee/house/etc.
    outflow_cats_df = kpi_df[
        (~kpi_df["category"].isin(["wager", "payout"]))
        & (~kpi_df["category"].str.startswith("UNLABELED"))
    ]
    fee_cats = sorted(outflow_cats_df["category"].unique().tolist())

    symbols = sorted(set(list(wagers) + list(payouts)))
    if not symbols:
        st.info("No flows recorded yet. Run the indexer first: `python -m ape_church_tracker.indexer`")
    for sym in symbols:
        # Top row: wagers + payouts + net + effective house edge
        top = st.columns(4)
        w = wagers.get(sym, 0)
        p = payouts.get(sym, 0)
        top[0].metric(f"Wagers in ({sym})", _fmt_amount(w))
        top[1].metric(f"Payouts out ({sym})", _fmt_amount(p))
        top[2].metric(f"Net contract PNL ({sym})", _fmt_amount(w - p))
        edge = (w - p) / w * 100 if w else 0
        top[3].metric(f"House edge ({sym})", f"{edge:.2f}%")

        # Second row: one metric per fee category defined in the config.
        if fee_cats:
            cat_cols = st.columns(min(len(fee_cats), 6) or 1)
            for i, cat in enumerate(fee_cats):
                sub = kpi_df[(kpi_df["category"] == cat) & (kpi_df["token_symbol"] == sym)]
                amount = float(sub["amount"].sum()) if not sub.empty else 0.0
                # Use the first human-readable label_name we can find, if any
                label_name = None
                if not sub.empty:
                    # amount column is derived; fetch label via separate query
                    pass
                cat_cols[i % len(cat_cols)].metric(
                    f"{cat} ({sym})",
                    _fmt_amount(amount),
                )

        st.caption(
            f"Fee categories above come from `config/games/{game_name}.json`. "
            "Edit `labels` there and run "
            f"`python -m ape_church_tracker.indexer --relabel --game {game_name}` "
            "to re-tag without re-indexing."
        )

    # ----- Unlabeled alert card -----------------------------------------------
    unlabeled = unlabeled_counterparties(engine, game_name, tokens)
    st.subheader("⚠️ Unlabeled counterparties")
    if unlabeled.empty:
        st.success("All flows are classified. Nothing in `UNLABELED` bucket.")
    else:
        st.error(
            f"{len(unlabeled)} unlabeled counterparty rows. "
            "Add them to `config/games/<game>.json` and run "
            "`python -m ape_church_tracker.indexer --relabel`."
        )
        show = unlabeled[["counterparty", "direction", "token_symbol", "amount", "n"]].copy()
        show["apescan"] = show["counterparty"].apply(
            lambda a: f"https://apescan.io/address/{a}"
        )
        st.dataframe(show, use_container_width=True, hide_index=True)

    # ----- Daily breakdown chart ----------------------------------------------
    st.subheader(f"Daily flow breakdown (last {days_back} days)")
    daily = daily_breakdown(engine, game_name, tokens, start_ts=start_ts, end_ts=end_ts)
    if daily.empty:
        st.info("No data in selected window.")
    else:
        out_daily = daily[daily["direction"] == "out"].copy()
        chart = (
            alt.Chart(out_daily)
            .mark_area()
            .encode(
                x=alt.X("day:T", title="Day"),
                y=alt.Y("amount:Q", stack="zero", title="Amount"),
                color=alt.Color("category:N", title="Category"),
                tooltip=["day", "category", "token_symbol", "amount", "n"],
            )
            .properties(height=340)
        )
        st.altair_chart(chart, use_container_width=True)

        wager_daily = daily[(daily["direction"] == "in") & (daily["category"] == "wager")]
        if not wager_daily.empty:
            st.caption("Daily wager inflow")
            st.bar_chart(
                wager_daily.groupby("day")["amount"].sum(),
                use_container_width=True,
            )

    # ----- Per-category table -------------------------------------------------
    st.subheader("Per-category totals")
    if not kpi_df.empty:
        pivot = (
            kpi_df.groupby(["category", "token_symbol"])
            .agg(lifetime=("amount", "sum"), rows=("n", "sum"))
            .reset_index()
            .sort_values("lifetime", ascending=False)
        )
        st.dataframe(pivot, use_container_width=True, hide_index=True)

    # ----- Reconciliation -----------------------------------------------------
    st.subheader("Reconciliation")
    ok, bad, mismatches = reconciliation_summary(engine, game_name)
    cols = st.columns(2)
    cols[0].metric("Reconciled (block, token) pairs", f"{ok:,}")
    cols[1].metric("Mismatched", f"{bad:,}", delta=None, delta_color="inverse")
    st.caption(
        "Per block and token, sum(in) − sum(out) must equal the contract's "
        "on-chain balance delta for that block. Mismatches indicate either a "
        "classifier bug or a contract balance change we're not indexing."
    )
    if bad:
        st.warning("Mismatched blocks:")
        st.dataframe(mismatches, use_container_width=True, hide_index=True)

    # ----- Recent activity ----------------------------------------------------
    st.subheader("Recent flows")
    recent = recent_flows(engine, game_name, tokens, limit=100)
    if recent.empty:
        st.caption("No recent flows.")
    else:
        recent["when"] = pd.to_datetime(recent["ts"], unit="s", utc=True)
        recent["apescan"] = recent["tx_hash"].apply(lambda h: f"https://apescan.io/tx/{h}")
        st.dataframe(
            recent[
                [
                    "when",
                    "direction",
                    "category",
                    "label_name",
                    "token_symbol",
                    "amount",
                    "counterparty",
                    "apescan",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )


if __name__ == "__main__":
    main()
