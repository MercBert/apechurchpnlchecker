"""Multi-page Streamlit dashboard for ape-church-tracker.

This file is a thin navigation entry point. Each page lives in
`dashboard_pages/<name>.py` and exposes a top-level `render()` function
that Streamlit invokes when the user selects that page.

Run:
    streamlit run dashboard.py
"""

from __future__ import annotations

import streamlit as st

from dashboard_pages import games, health, house, nft, summary, treasury


st.set_page_config(
    page_title="Ape Church tracker",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="expanded",
)


# Streamlit 1.30+ supports st.Page + st.navigation with `render` callables.
PAGES = [
    st.Page(summary.render,  title="Executive Summary", icon="📊", default=True),
    st.Page(games.render,    title="Games",             icon="🎰"),
    st.Page(treasury.render, title="Treasury",          icon="🏦"),
    st.Page(house.render,    title="House staking",     icon="🏠"),
    st.Page(nft.render,      title="NFT royalties",     icon="🖼️"),
    st.Page(health.render,   title="System health",     icon="🩺"),
]


nav = st.navigation(PAGES)
nav.run()
