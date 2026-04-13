"""SQL-backed rollup queries used by the Streamlit dashboard.

All functions return pandas DataFrames. Amounts are kept as strings in the DB
and decoded to float/Decimal here using each token's known decimals.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine


def _apply_decimals(df: pd.DataFrame, tokens: Dict[str, dict]) -> pd.DataFrame:
    """Convert `amount_raw` base-unit values to a float `amount` column using
    the decimals info from the game config.

    `amount_raw` can be either a string (per-row queries) or a float (aggregate
    queries that SUM CAST(amount_raw AS REAL) in SQL). Both are handled.
    Int64 is *not* used because summed totals routinely exceed int64 max
    (e.g. 1M APE in wei = 1e24, int64 caps at ~9.2e18).
    """
    if df.empty:
        df["amount"] = pd.Series(dtype=float)
        df["token_symbol"] = pd.Series(dtype=str)
        return df

    def decode(row):
        meta = tokens.get(row["token"]) or tokens.get("native") or {"decimals": 18, "symbol": row["token"]}
        dec = int(meta.get("decimals", 18))
        raw = row["amount_raw"]
        # Strings — exact decimal math.
        if isinstance(raw, str):
            try:
                return float(Decimal(raw) / (Decimal(10) ** dec))
            except Exception:
                return 0.0
        # Floats (from SQL SUM) — direct divide. Float64 has 15-17 digits of
        # precision which is plenty for display (we show 4-6 decimals).
        if raw is None or pd.isna(raw):
            return 0.0
        return float(raw) / (10 ** dec)

    def symbol(row):
        meta = tokens.get(row["token"])
        if meta:
            return meta.get("symbol", row["token"])
        return row["token"][:8]

    df["amount"] = df.apply(decode, axis=1)
    df["token_symbol"] = df.apply(symbol, axis=1)
    return df


def kpis(engine: Engine, game: str, tokens: Dict[str, dict]) -> pd.DataFrame:
    """Lifetime totals per (token, category)."""
    q = text(
        """
        SELECT token, category, SUM(CAST(amount_raw AS REAL)) AS amount_raw,
               COUNT(*) AS n
        FROM flows
        WHERE game = :g
        GROUP BY token, category
        """
    )
    df = pd.read_sql(q, engine, params={"g": game})
    return _apply_decimals(df, tokens)


def daily_breakdown(
    engine: Engine,
    game: str,
    tokens: Dict[str, dict],
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> pd.DataFrame:
    """Daily totals per (token, category, direction) — the stacked-area data."""
    clauses = ["game = :g"]
    params: Dict[str, object] = {"g": game}
    if start_ts is not None:
        clauses.append("ts >= :s")
        params["s"] = start_ts
    if end_ts is not None:
        clauses.append("ts <= :e")
        params["e"] = end_ts
    q = text(
        f"""
        SELECT date(ts, 'unixepoch') AS day,
               token, category, direction,
               SUM(CAST(amount_raw AS REAL)) AS amount_raw,
               COUNT(*) AS n
        FROM flows
        WHERE {' AND '.join(clauses)}
        GROUP BY day, token, category, direction
        ORDER BY day
        """
    )
    df = pd.read_sql(q, engine, params=params)
    return _apply_decimals(df, tokens)


def unlabeled_counterparties(
    engine: Engine,
    game: str,
    tokens: Dict[str, dict],
) -> pd.DataFrame:
    """Distinct counterparties currently categorized as UNLABELED / UNLABELED_IN."""
    q = text(
        """
        SELECT counterparty, token, direction,
               SUM(CAST(amount_raw AS REAL)) AS amount_raw,
               COUNT(*) AS n
        FROM flows
        WHERE game = :g AND category LIKE 'UNLABELED%'
        GROUP BY counterparty, token, direction
        ORDER BY amount_raw DESC
        """
    )
    df = pd.read_sql(q, engine, params={"g": game})
    return _apply_decimals(df, tokens)


def reconciliation_summary(engine: Engine, game: str) -> Tuple[int, int, pd.DataFrame]:
    """Return (ok_count, mismatch_count, mismatches_df).

    Reconciliation is block-level: per (block, token), sum(flow.in) -
    sum(flow.out) must equal the contract's on-chain balance delta for that
    block. `native` token rows are recorded as SKIPPED because log-based
    extraction can't see native-value moves without trace RPC.
    """
    with engine.connect() as conn:
        ok = conn.execute(
            text("SELECT COUNT(*) FROM block_reconciliation WHERE game = :g AND ok = 1"),
            {"g": game},
        ).scalar_one()
        bad = conn.execute(
            text("SELECT COUNT(*) FROM block_reconciliation WHERE game = :g AND ok = 0"),
            {"g": game},
        ).scalar_one()
    df = pd.read_sql(
        text(
            "SELECT block_number, token, sum_in_raw, sum_out_raw, balance_delta_raw "
            "FROM block_reconciliation WHERE game = :g AND ok = 0 "
            "ORDER BY block_number DESC LIMIT 200"
        ),
        engine,
        params={"g": game},
    )
    return int(ok), int(bad), df


def recent_flows(
    engine: Engine,
    game: str,
    tokens: Dict[str, dict],
    limit: int = 100,
    categories: Optional[List[str]] = None,
) -> pd.DataFrame:
    clauses = ["game = :g"]
    params: Dict[str, object] = {"g": game, "lim": limit}
    if categories:
        placeholders = ",".join(f":c{i}" for i in range(len(categories)))
        clauses.append(f"category IN ({placeholders})")
        for i, c in enumerate(categories):
            params[f"c{i}"] = c
    q = text(
        f"""
        SELECT ts, block_number, tx_hash, direction, token, amount_raw,
               counterparty, category, label_name
        FROM flows
        WHERE {' AND '.join(clauses)}
        ORDER BY ts DESC, log_index DESC
        LIMIT :lim
        """
    )
    df = pd.read_sql(q, engine, params=params)
    return _apply_decimals(df, tokens)


def sync_status(engine: Engine, game: str) -> Optional[int]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT last_block FROM sync_state WHERE game = :g"),
            {"g": game},
        ).first()
        return int(row[0]) if row else None


# --------------------------------------------------------------------------
# Cross-tracker rollups for the team dashboard
# --------------------------------------------------------------------------


def _native_amount(raw: object) -> float:
    """Convert a wei-valued base-unit amount (float or string) to native APE/ETH
    floats. Assumes 18 decimals — true for APE and ETH."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return 0.0
    if isinstance(raw, str):
        try:
            return float(Decimal(raw) / (Decimal(10) ** 18))
        except Exception:
            return 0.0
    return float(raw) / 1e18


def category_totals_across_games(
    engine: Engine,
    categories: List[str],
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> pd.DataFrame:
    """Sum a set of categories across every tracker in the DB.

    Used by the Executive Summary page to compute, for example,
    "total ape.church protocol fee income across all games" in a single query.

    Returns: DataFrame with columns [game, category, token, amount_raw, amount].
    """
    if not categories:
        return pd.DataFrame(
            columns=["game", "category", "token", "amount_raw", "amount"]
        )
    placeholders = ",".join(f":c{i}" for i in range(len(categories)))
    clauses = [f"category IN ({placeholders})"]
    params: Dict[str, object] = {f"c{i}": c for i, c in enumerate(categories)}
    if start_ts is not None:
        clauses.append("ts >= :s")
        params["s"] = start_ts
    if end_ts is not None:
        clauses.append("ts <= :e")
        params["e"] = end_ts
    q = text(
        f"""
        SELECT game, category, token,
               SUM(CAST(amount_raw AS REAL)) AS amount_raw
        FROM flows
        WHERE {' AND '.join(clauses)}
        GROUP BY game, category, token
        ORDER BY amount_raw DESC
        """
    )
    df = pd.read_sql(q, engine, params=params)
    if df.empty:
        df["amount"] = pd.Series(dtype=float)
        return df
    df["amount"] = df["amount_raw"].apply(_native_amount)
    return df


def lifetime_revenue(
    engine: Engine,
    revenue_categories: List[str],
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> float:
    """Single scalar: total APE revenue across every tracker in the DB,
    summed across the given revenue categories. Used for the big headline
    card on the Executive Summary page.

    For Ape Church, `revenue_categories` is typically something like:
        ["ape_church_fee", "fee_from_ape_church_fee_receiver", "nft_royalty"]
    — i.e. every category name across trackers that represents money that
    ultimately lands with us.
    """
    df = category_totals_across_games(engine, revenue_categories, start_ts, end_ts)
    if df.empty:
        return 0.0
    return float(df["amount"].sum())


def daily_revenue_stacked(
    engine: Engine,
    revenue_categories: List[str],
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> pd.DataFrame:
    """Daily revenue per (game, category) for the Executive Summary stacked
    area chart. Returns columns [day, game, category, amount]."""
    if not revenue_categories:
        return pd.DataFrame(columns=["day", "game", "category", "amount"])
    placeholders = ",".join(f":c{i}" for i in range(len(revenue_categories)))
    clauses = [f"category IN ({placeholders})"]
    params: Dict[str, object] = {
        f"c{i}": c for i, c in enumerate(revenue_categories)
    }
    if start_ts is not None:
        clauses.append("ts >= :s")
        params["s"] = start_ts
    if end_ts is not None:
        clauses.append("ts <= :e")
        params["e"] = end_ts
    q = text(
        f"""
        SELECT date(ts, 'unixepoch') AS day,
               game, category,
               SUM(CAST(amount_raw AS REAL)) AS amount_raw
        FROM flows
        WHERE {' AND '.join(clauses)}
        GROUP BY day, game, category
        ORDER BY day
        """
    )
    df = pd.read_sql(q, engine, params=params)
    if df.empty:
        df["amount"] = pd.Series(dtype=float)
        return df
    df["amount"] = df["amount_raw"].apply(_native_amount)
    return df


def top_games(
    engine: Engine,
    fee_category: str = "ape_church_fee",
    limit: int = 10,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> pd.DataFrame:
    """Top N games by protocol-fee revenue in a time window.

    Returns columns [game, fees_ape, n_flows].
    """
    clauses = ["category = :cat"]
    params: Dict[str, object] = {"cat": fee_category, "lim": limit}
    if start_ts is not None:
        clauses.append("ts >= :s")
        params["s"] = start_ts
    if end_ts is not None:
        clauses.append("ts <= :e")
        params["e"] = end_ts
    q = text(
        f"""
        SELECT game,
               SUM(CAST(amount_raw AS REAL)) AS amount_raw,
               COUNT(*) AS n_flows
        FROM flows
        WHERE {' AND '.join(clauses)}
        GROUP BY game
        ORDER BY amount_raw DESC
        LIMIT :lim
        """
    )
    df = pd.read_sql(q, engine, params=params)
    if df.empty:
        df["fees_ape"] = pd.Series(dtype=float)
        return df
    df["fees_ape"] = df["amount_raw"].apply(_native_amount)
    return df[["game", "fees_ape", "n_flows"]]


def all_trackers_health(engine: Engine) -> pd.DataFrame:
    """One row per tracker summarizing sync state, reconciliation, and
    UNLABELED backlog. Used on the System Health page.

    Columns: [game, last_block, flow_count, unlabeled_count, recon_ok, recon_bad].
    """
    q = text(
        """
        SELECT
          s.game,
          s.last_block,
          COALESCE(f.flow_count,     0) AS flow_count,
          COALESCE(f.unlabeled_count, 0) AS unlabeled_count,
          COALESCE(r.recon_ok,       0) AS recon_ok,
          COALESCE(r.recon_bad,      0) AS recon_bad
        FROM sync_state s
        LEFT JOIN (
          SELECT game,
                 COUNT(*) AS flow_count,
                 SUM(CASE WHEN category LIKE 'UNLABELED%' THEN 1 ELSE 0 END)
                   AS unlabeled_count
          FROM flows
          GROUP BY game
        ) f ON f.game = s.game
        LEFT JOIN (
          SELECT game,
                 SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS recon_ok,
                 SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS recon_bad
          FROM block_reconciliation
          GROUP BY game
        ) r ON r.game = s.game
        ORDER BY s.game
        """
    )
    return pd.read_sql(q, engine)


def all_distinct_categories(engine: Engine) -> List[str]:
    """Every category that currently appears in the DB. Useful for
    'revenue_categories' auto-detection."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT category FROM flows ORDER BY category")
        ).fetchall()
    return [r[0] for r in rows]
