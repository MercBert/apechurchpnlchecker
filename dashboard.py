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
# Every page function is named `render`, so we pass explicit url_path values
# to avoid "URL pathnames must be unique" collisions.
PAGES = [
    st.Page(summary.render,  title="Executive Summary", icon="📊", url_path="summary", default=True),
    st.Page(games.render,    title="Games",             icon="🎰", url_path="games"),
    st.Page(treasury.render, title="Treasury",          icon="🏦", url_path="treasury"),
    st.Page(house.render,    title="House staking",     icon="🏠", url_path="house"),
    st.Page(nft.render,      title="NFT royalties",     icon="🖼️", url_path="nft"),
    st.Page(health.render,   title="System health",     icon="🩺", url_path="health"),
]


nav = st.navigation(PAGES)
nav.run()
