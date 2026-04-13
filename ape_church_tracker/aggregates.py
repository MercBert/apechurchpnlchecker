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
    """Convert `amount_raw` (string base units) to a float `amount` column
    using the decimals info from the game config.
    """
    if df.empty:
        df["amount"] = pd.Series(dtype=float)
        df["token_symbol"] = pd.Series(dtype=str)
        return df

    def decode(row):
        meta = tokens.get(row["token"]) or tokens.get("native") or {"decimals": 18, "symbol": row["token"]}
        dec = int(meta.get("decimals", 18))
        return float(Decimal(row["amount_raw"]) / (Decimal(10) ** dec))

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
    df["amount_raw"] = df["amount_raw"].astype("Int64").astype(str)
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
    df["amount_raw"] = df["amount_raw"].astype("Int64").astype(str)
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
    df["amount_raw"] = df["amount_raw"].astype("Int64").astype(str)
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
