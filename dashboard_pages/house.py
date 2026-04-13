"""House staking page — activity in the decentralized House contract,
with a focus on Ape Church's own stake position (flows to/from the
Deployer and Treasury wallets).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy import text

from dashboard_pages._common import APE_CHURCH_WALLET_TRACKERS, format_ape, get_db_engine, get_games


def render() -> None:
    st.title("House staking")
    st.caption(
        "The decentralized House contract pools losing-spin APE and lets stakers earn yield. "
        "This page isolates Ape Church's own position from the rest of the pool."
    )

    engine = get_db_engine()
    house = next((g for g in get_games() if g["name"] == "house"), None)
    if house is None:
        st.info("No `house` tracker configured.")
        return

    st.caption(f"House contract: `{house['address']}`")

    # Total flows through the house
    totals = pd.read_sql(
        text("""
            SELECT direction, SUM(CAST(amount_raw AS REAL)) AS r, COUNT(*) AS n
            FROM flows WHERE game = 'house' GROUP BY direction
        """),
        engine,
    )
    if totals.empty:
        st.info("No house flows indexed yet.")
        return

    def _dir(d: str) -> float:
        if totals.empty:
            return 0.0
        sub = totals[totals["direction"] == d]
        if sub.empty:
            return 0.0
        return float(sub["r"].iloc[0] or 0) / 1e18

    col1, col2, col3 = st.columns(3)
    col1.metric("Total APE stakes/deposits in", format_ape(_dir("in")))
    col2.metric("Total APE withdrawn/yield out", format_ape(_dir("out")))
    col3.metric("Net APE held by house", format_ape(_dir("in") - _dir("out")))

    # Ape Church's own position: flows where counterparty is one of our wallets
    st.subheader("Ape Church's position in the House")
    wallet_addrs = [g["address"].lower() for g in get_games() if g["name"] in APE_CHURCH_WALLET_TRACKERS]
    if not wallet_addrs:
        st.caption("No Ape Church wallets configured to filter against.")
        return

    q = text(
        """
        SELECT counterparty, direction,
               SUM(CAST(amount_raw AS REAL)) AS r,
               COUNT(*) AS n
        FROM flows
        WHERE game = 'house'
          AND counterparty IN :addrs
        GROUP BY counterparty, direction
        """
    )
    # SQLAlchemy doesn't support `IN :tuple` directly for arbitrary-length
    # lists in `text`, so we expand manually:
    placeholders = ",".join(f":a{i}" for i in range(len(wallet_addrs)))
    q2 = text(
        f"""
        SELECT counterparty, direction,
               SUM(CAST(amount_raw AS REAL)) AS r,
               COUNT(*) AS n
        FROM flows
        WHERE game = 'house'
          AND counterparty IN ({placeholders})
        GROUP BY counterparty, direction
        ORDER BY r DESC
        """
    )
    params = {f"a{i}": a for i, a in enumerate(wallet_addrs)}
    ours = pd.read_sql(q2, engine, params=params)

    if ours.empty:
        st.info("No flows yet between the House and Ape Church's wallets. "
                "This may mean the deployer/treasury haven't staked yet, "
                "or the house tracker is still backfilling.")
        return

    ours["ape"] = ours["r"].apply(lambda v: float(v or 0) / 1e18)
    stake_in = ours[ours["direction"] == "in"]["ape"].sum()
    stake_out = ours[ours["direction"] == "out"]["ape"].sum()
    net_position = stake_in - stake_out

    cols = st.columns(3)
    cols[0].metric("Staked in (deposits)", format_ape(stake_in))
    cols[1].metric("Unstaked out (+ yield)", format_ape(stake_out))
    cols[2].metric(
        "Net position",
        format_ape(net_position),
        delta="negative = yield profit" if net_position < 0 else None,
    )

    st.caption("Breakdown per wallet")
    display = ours[["counterparty", "direction", "ape", "n"]].copy()
    display["ape"] = display["ape"].apply(format_ape)
    st.dataframe(display, use_container_width=True, hide_index=True)
