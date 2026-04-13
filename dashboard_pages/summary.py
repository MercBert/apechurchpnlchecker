"""Executive Summary — team landing page.

Big headline cards (lifetime / 7d / 24h), a daily stacked revenue chart,
and a top-5 games table. Designed to answer "how much did Ape Church
make?" in 5 seconds, before anyone drills into a specific tracker.
"""

from __future__ import annotations

import time

import altair as alt
import pandas as pd
import streamlit as st

from ape_church_tracker.aggregates import (
    daily_revenue_stacked,
    lifetime_revenue,
    top_games,
)
from dashboard_pages._common import (
    format_ape,
    get_db_engine,
    revenue_categories_in_db,
    time_range_sidebar,
)


def render() -> None:
    st.title("Ape Church — Executive Summary")
    st.caption("Lifetime revenue across every tracked game, wallet, and NFT source.")

    engine = get_db_engine()
    revenue_cats = revenue_categories_in_db()

    if not revenue_cats:
        st.info(
            "No revenue-categorized flows yet. Backfill the trackers first:\n"
            "```\npython -m ape_church_tracker.indexer --once\n```"
        )
        return

    st.caption(
        "Revenue categories currently rolled up: "
        + ", ".join(f"`{c}`" for c in revenue_cats)
    )

    # ---------- Headline cards --------------------------------------------
    now = int(time.time())
    life = lifetime_revenue(engine, revenue_cats)
    last_7d = lifetime_revenue(engine, revenue_cats, start_ts=now - 7 * 86400)
    prev_7d = lifetime_revenue(
        engine, revenue_cats,
        start_ts=now - 14 * 86400, end_ts=now - 7 * 86400,
    )
    last_24h = lifetime_revenue(engine, revenue_cats, start_ts=now - 86400)
    prev_24h = lifetime_revenue(
        engine, revenue_cats,
        start_ts=now - 2 * 86400, end_ts=now - 86400,
    )

    cols = st.columns(3)
    cols[0].metric("Lifetime revenue", f"{format_ape(life)} APE")

    delta_7d = last_7d - prev_7d
    delta_7d_pct = (delta_7d / prev_7d * 100) if prev_7d else None
    cols[1].metric(
        "Last 7 days",
        f"{format_ape(last_7d)} APE",
        delta=f"{delta_7d_pct:+.1f}% vs prior 7d" if delta_7d_pct is not None else None,
    )

    delta_24h = last_24h - prev_24h
    delta_24h_pct = (delta_24h / prev_24h * 100) if prev_24h else None
    cols[2].metric(
        "Last 24 hours",
        f"{format_ape(last_24h)} APE",
        delta=f"{delta_24h_pct:+.1f}% vs prior day" if delta_24h_pct is not None else None,
    )

    # ---------- Daily stacked revenue chart -------------------------------
    start_ts, end_ts = time_range_sidebar(default_days=30)
    st.subheader("Daily revenue")
    daily = daily_revenue_stacked(engine, revenue_cats, start_ts=start_ts, end_ts=end_ts)
    if daily.empty:
        st.caption("No revenue in the selected window.")
    else:
        chart = (
            alt.Chart(daily)
            .mark_area()
            .encode(
                x=alt.X("day:T", title="Day"),
                y=alt.Y("amount:Q", stack="zero", title="APE"),
                color=alt.Color(
                    "game:N", title="Tracker",
                    legend=alt.Legend(orient="bottom"),
                ),
                tooltip=["day", "game", "category", "amount"],
            )
            .properties(height=320)
        )
        st.altair_chart(chart, use_container_width=True)

    # ---------- Top earning games -----------------------------------------
    st.subheader(f"Top earning games (last {(end_ts - start_ts) // 86400} days)")
    for fee_cat in revenue_cats:
        top = top_games(engine, fee_category=fee_cat, limit=10,
                        start_ts=start_ts, end_ts=end_ts)
        if top.empty:
            continue
        st.caption(f"By category: `{fee_cat}`")
        display = top.rename(columns={"fees_ape": "APE earned", "n_flows": "flows"})
        display["APE earned"] = display["APE earned"].apply(format_ape)
        st.dataframe(display, use_container_width=True, hide_index=True)

    st.caption(
        "Drill into any tracker via the sidebar navigation. "
        "Visit **Games** for a full table across all tracked games, "
        "**Treasury** for wallet balances, **House** for staking, "
        "**NFT** for royalty income, or **System health** for sync status."
    )
