"""NFT royalties page — placeholder until an NFT royalty tracker config
is added. Once a tracker with `kind = "nft_royalty"` exists, this page
renders lifetime royalty income, a daily chart, and recent sales.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy import text

from dashboard_pages._common import format_ape, get_db_engine, get_games


def render() -> None:
    st.title("NFT royalties")
    st.caption("Royalty income from ape.church NFT collections sold on OpenSea, Blur, etc.")

    engine = get_db_engine()
    nft_trackers = [
        g for g in get_games()
        if "nft" in g["name"].lower() or g.get("kind") == "nft_royalty"
    ]

    if not nft_trackers:
        st.info(
            "No NFT royalty trackers configured yet.\n\n"
            "To add one, create `config/games/ape_church_nft.json` with:\n\n"
            "```json\n"
            "{\n"
            '  "name": "ape_church_nft",\n'
            '  "address": "<royalty receiver wallet>",\n'
            '  "start_block": 0,\n'
            '  "tokens": {\n'
            '    "native": {"symbol": "ETH", "decimals": 18}\n'
            '  },\n'
            '  "self_in_category": "royalty_in",\n'
            '  "self_out_category": "self_out",\n'
            '  "labels": {\n'
            '    "0x00000000000000adc04c56bf30ac9d3c0aaf14dc": '
            '{"category": "nft_royalty_opensea", "name": "OpenSea Seaport 1.5"}\n'
            '  }\n'
            "}\n"
            "```\n\n"
            "You'll need the chain the collection is on (Ethereum, Base, Polygon, or "
            "Ape Chain), the royalty receiver wallet, and marketplace contract "
            "addresses (OpenSea Seaport 1.5 on Ethereum is the one above)."
        )
        return

    for tracker in nft_trackers:
        st.subheader(tracker["name"])
        st.caption(f"Royalty wallet: `{tracker['address']}`")

        totals = pd.read_sql(
            text("""
                SELECT category, SUM(CAST(amount_raw AS REAL)) AS r, COUNT(*) AS n
                FROM flows
                WHERE game = :g AND direction = 'in'
                GROUP BY category
                ORDER BY r DESC
            """),
            engine, params={"g": tracker["name"]},
        )

        if totals.empty:
            st.caption("No royalty flows indexed yet.")
            continue

        totals["amount_ape_or_eth"] = totals["r"].apply(lambda v: float(v or 0) / 1e18)
        lifetime = totals["amount_ape_or_eth"].sum()
        st.metric("Lifetime royalty income", format_ape(lifetime))

        display = totals[["category", "amount_ape_or_eth", "n"]].copy()
        display.columns = ["category", "amount", "count"]
        display["amount"] = display["amount"].apply(format_ape)
        st.dataframe(display, use_container_width=True, hide_index=True)
