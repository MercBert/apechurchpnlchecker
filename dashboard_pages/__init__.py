"""Multi-page Streamlit dashboard for ape-church-tracker.

Each page is a self-contained Streamlit script that calls into
`ape_church_tracker.aggregates` for its data. `dashboard.py` at the repo
root uses `st.navigation([...])` to wire them together.
"""
