"""Games page — table of every tracked game with per-game totals, plus
a drill-down into the per-game view (the original single-tracker layout).
"""

from __future__ import annotations

import time
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
from dashboard_pages._common import (
    format_ape,
    get_db_engine,
    get_games,
    revenue_categories_in_db,
)


def _is_game(game: dict) -> bool:
    """Heuristic: a 'game' is any tracker whose self_in_category is 'wager'.
    Wallets / fee receivers / claim managers use other self-categories."""
    return game.get("self_in_category") == "wager"


def render() -> None:
    st.title("Games")
    st.caption("Every tracked game on ape.church and how much each earned for the protocol.")

    engine = get_db_engine()
    games = [g for g in get_games() if _is_game(g)]
    if not games:
        st.info("No game trackers configured yet. Add one under `config/games/<name>.json`.")
        return

    # ---------- Summary table ------------------------------------------
    revenue_cats = revenue_categories_in_db()
    rows = []
    for g in games:
        kpi_df = kpis(engine, g["name"], g["tokens"])
        wager_total = float(
            kpi_df.loc[kpi_df["category"] == "wager", "amount"].sum()
        ) if not kpi_df.empty else 0.0
        payout_total = float(
            kpi_df.loc[kpi_df["category"] == "payout", "amount"].sum()
        ) if not kpi_df.empty else 0.0
        fee_total = 0.0
        if not kpi_df.empty and revenue_cats:
            fee_total = float(
                kpi_df.loc[kpi_df["category"].isin(revenue_cats), "amount"].sum()
            )
        rows.append({
            "game": g["name"],
            "address": g["address"],
            "wagers_ape": wager_total,
            "payouts_ape": payout_total,
            "ape_church_fee": fee_total,
            "sync_block": sync_status(engine, g["name"]) or 0,
        })

    summary_df = pd.DataFrame(rows)
    if summary_df.empty:
        st.caption("No data yet.")
    else:
        total_fees = summary_df["ape_church_fee"].sum() or 1.0
        summary_df["share_%"] = summary_df["ape_church_fee"] / total_fees * 100
        summary_df = summary_df.sort_values("ape_church_fee", ascending=False)
        display = summary_df.copy()
        display["wagers_ape"] = display["wagers_ape"].apply(format_ape)
        display["payouts_ape"] = display["payouts_ape"].apply(format_ape)
        display["ape_church_fee"] = display["ape_church_fee"].apply(format_ape)
        display["share_%"] = display["share_%"].apply(lambda v: f"{v:.1f}%")
        st.dataframe(display, use_container_width=True, hide_index=True)

    # ---------- Drill-down ---------------------------------------------
    st.subheader("Drill-down")
    game_names = [g["name"] for g in games]
    selected = st.selectbox("Pick a game to inspect", game_names)
    g = next(x for x in games if x["name"] == selected)
    _render_game_detail(engine, g)


def _render_game_detail(engine, game: dict) -> None:
    tokens = game["tokens"]
    name = game["name"]
    st.caption(f"Contract: `{game['address']}`  •  Last synced block: "
               f"{sync_status(engine, name):,}" if sync_status(engine, name) else "Not synced yet")

    kpi_df = kpis(engine, name, tokens)

    # Headline row
    if kpi_df.empty:
        st.info("No flows recorded yet for this game.")
        return

    def _sum_dir(direction: str) -> Dict[str, float]:
        from sqlalchemy import text as _t
        df = pd.read_sql(
            _t(
                """
                SELECT token, direction, SUM(CAST(amount_raw AS REAL)) AS amount_raw
                FROM flows WHERE game = :g GROUP BY token, direction
                """
            ),
            engine,
            params={"g": name},
        )
        if df.empty:
            return {}
        df = df[df["direction"] == direction]
        totals: Dict[str, float] = {}
        for _, row in df.iterrows():
            meta = tokens.get(row["token"]) or {"symbol": row["token"][:8], "decimals": 18}
            sym = meta.get("symbol", row["token"][:8])
            dec = int(meta.get("decimals", 18))
            totals[sym] = totals.get(sym, 0.0) + float(row["amount_raw"] or 0) / (10 ** dec)
        return totals

    total_in = _sum_dir("in")
    total_out = _sum_dir("out")
    symbols = sorted(set(list(total_in) + list(total_out)))
    for sym in symbols:
        tin = total_in.get(sym, 0.0)
        tout = total_out.get(sym, 0.0)
        top = st.columns(3)
        top[0].metric(f"Total in ({sym})", format_ape(tin))
        top[1].metric(f"Total out ({sym})", format_ape(tout))
        top[2].metric(f"Net ({sym})", format_ape(tin - tout))

    # Per-category table
    for sym in symbols:
        sub = kpi_df[kpi_df["token_symbol"] == sym]
        if sub.empty:
            continue
        non_un = sub[~sub["category"].str.startswith("UNLABELED")]
        if non_un.empty:
            continue
        st.caption(f"**Per-category breakdown ({sym})**")
        rows = (
            non_un.groupby("category")["amount"]
            .sum()
            .sort_values(ascending=False)
            .reset_index()
        )
        rows["amount"] = rows["amount"].apply(format_ape)
        st.dataframe(rows, use_container_width=True, hide_index=True)

    # Unlabeled
    unlabeled = unlabeled_counterparties(engine, name, tokens)
    if not unlabeled.empty:
        st.warning(f"{len(unlabeled)} unlabeled counterparty rows. Add them to "
                   f"`config/games/{name}.json` and re-run with `--relabel`.")
        st.dataframe(
            unlabeled[["counterparty", "direction", "token_symbol", "amount", "n"]],
            use_container_width=True, hide_index=True,
        )

    # Daily chart
    start_ts = int(time.time()) - 30 * 86400
    daily = daily_breakdown(engine, name, tokens, start_ts=start_ts)
    if not daily.empty:
        out_daily = daily[daily["direction"] == "out"]
        chart = (
            alt.Chart(out_daily)
            .mark_area()
            .encode(
                x=alt.X("day:T"),
                y=alt.Y("amount:Q", stack="zero", title="APE"),
                color=alt.Color("category:N", legend=alt.Legend(orient="bottom")),
                tooltip=["day", "category", "amount"],
            )
            .properties(height=260)
        )
        st.altair_chart(chart, use_container_width=True)

    # Reconciliation
    ok, bad, _mismatches = reconciliation_summary(engine, name)
    rcols = st.columns(2)
    rcols[0].metric("Blocks reconciled", f"{ok:,}")
    rcols[1].metric("Blocks mismatched", f"{bad:,}")

    # Recent flows
    recent = recent_flows(engine, name, tokens, limit=50)
    if not recent.empty:
        recent["when"] = pd.to_datetime(recent["ts"], unit="s", utc=True)
        recent["apescan"] = recent["tx_hash"].apply(lambda h: f"https://apescan.io/tx/{h}")
        st.caption("Last 50 flows")
        st.dataframe(
            recent[["when", "direction", "category", "label_name",
                    "token_symbol", "amount", "counterparty", "apescan"]],
            use_container_width=True, hide_index=True,
        )
