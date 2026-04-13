"""SQLite schema + upsert helpers.

The schema is flow-first: one row in `flows` per value movement in or out of
a game contract, with a `category` label. `game_events` and `raw_events` are
audit/enrichment tables. `tx_reconciliation` stores per-tx balance checks.
`labels` is materialized from each game's config so we can relabel historical
flows without re-indexing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, Optional

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine


METADATA = MetaData()


sync_state = Table(
    "sync_state",
    METADATA,
    Column("game", String, primary_key=True),
    Column("last_block", Integer, nullable=False),
)


flows = Table(
    "flows",
    METADATA,
    Column("game", String, nullable=False),
    Column("tx_hash", String, nullable=False),
    Column("log_index", Integer, nullable=False),
    Column("block_number", Integer, nullable=False),
    Column("ts", Integer, nullable=False),
    Column("direction", String, nullable=False),  # 'in' | 'out'
    Column("token", String, nullable=False),      # 'native' or ERC-20 address (lower)
    Column("amount_raw", String, nullable=False),
    Column("counterparty", String, nullable=False),
    Column("category", String, nullable=False),
    Column("label_name", String),
)


game_events = Table(
    "game_events",
    METADATA,
    Column("game", String, nullable=False),
    Column("tx_hash", String, nullable=False),
    Column("log_index", Integer, nullable=False),
    Column("block_number", Integer, nullable=False),
    Column("ts", Integer, nullable=False),
    Column("event_name", String, nullable=False),
    Column("args_json", String, nullable=False),
)


raw_events = Table(
    "raw_events",
    METADATA,
    Column("game", String, nullable=False),
    Column("tx_hash", String, nullable=False),
    Column("log_index", Integer, nullable=False),
    Column("block_number", Integer, nullable=False),
    Column("ts", Integer, nullable=False),
    Column("address", String, nullable=False),
    Column("topics_json", String, nullable=False),
    Column("data", String, nullable=False),
)


block_reconciliation = Table(
    "block_reconciliation",
    METADATA,
    Column("game", String, nullable=False),
    Column("block_number", Integer, nullable=False),
    Column("token", String, nullable=False),
    Column("sum_in_raw", String, nullable=False),
    Column("sum_out_raw", String, nullable=False),
    Column("balance_delta_raw", String, nullable=False),
    Column("ok", Integer, nullable=False),
)


labels = Table(
    "labels",
    METADATA,
    Column("game", String, nullable=False),
    Column("address", String, nullable=False),  # lowercase
    Column("category", String, nullable=False),
    Column("name", String, nullable=False),
)


def get_engine(db_path: str) -> Engine:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", future=True)
    # Apply schema + composite-key indexes (SQLAlchemy can't express
    # the multi-column PKs we want because some tables have composite PKs
    # that span 3+ columns — simpler to do it with raw DDL below.)
    with engine.begin() as conn:
        conn.execute(text("PRAGMA journal_mode = WAL"))
        conn.execute(text("PRAGMA synchronous = NORMAL"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS sync_state (
                    game TEXT PRIMARY KEY,
                    last_block INTEGER NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS flows (
                    game TEXT NOT NULL,
                    tx_hash TEXT NOT NULL,
                    log_index INTEGER NOT NULL,
                    block_number INTEGER NOT NULL,
                    ts INTEGER NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('in','out')),
                    token TEXT NOT NULL,
                    amount_raw TEXT NOT NULL,
                    counterparty TEXT NOT NULL,
                    category TEXT NOT NULL,
                    label_name TEXT,
                    PRIMARY KEY (game, tx_hash, log_index)
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS flows_game_ts ON flows (game, ts)"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS flows_game_cat_ts ON flows (game, category, ts)")
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS flows_counterparty ON flows (game, counterparty)"
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS game_events (
                    game TEXT NOT NULL,
                    tx_hash TEXT NOT NULL,
                    log_index INTEGER NOT NULL,
                    block_number INTEGER NOT NULL,
                    ts INTEGER NOT NULL,
                    event_name TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    PRIMARY KEY (game, tx_hash, log_index)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS raw_events (
                    game TEXT NOT NULL,
                    tx_hash TEXT NOT NULL,
                    log_index INTEGER NOT NULL,
                    block_number INTEGER NOT NULL,
                    ts INTEGER NOT NULL,
                    address TEXT NOT NULL,
                    topics_json TEXT NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (game, tx_hash, log_index)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS block_reconciliation (
                    game TEXT NOT NULL,
                    block_number INTEGER NOT NULL,
                    token TEXT NOT NULL,
                    sum_in_raw TEXT NOT NULL,
                    sum_out_raw TEXT NOT NULL,
                    balance_delta_raw TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    PRIMARY KEY (game, block_number, token)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS labels (
                    game TEXT NOT NULL,
                    address TEXT NOT NULL,
                    category TEXT NOT NULL,
                    name TEXT NOT NULL,
                    PRIMARY KEY (game, address)
                )
                """
            )
        )
    return engine


def get_last_block(engine: Engine, game: str) -> Optional[int]:
    with engine.connect() as conn:
        row = conn.execute(
            select(sync_state.c.last_block).where(sync_state.c.game == game)
        ).first()
        return int(row[0]) if row else None


def set_last_block(conn, game: str, block: int) -> None:
    conn.execute(
        text(
            "INSERT INTO sync_state (game, last_block) VALUES (:g, :b) "
            "ON CONFLICT(game) DO UPDATE SET last_block = excluded.last_block"
        ),
        {"g": game, "b": block},
    )


def upsert_labels(engine: Engine, game: str, mapping: Dict[str, dict]) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM labels WHERE game = :g"), {"g": game})
        if not mapping:
            return
        conn.execute(
            labels.insert(),
            [
                {"game": game, "address": addr.lower(), "category": v["category"], "name": v["name"]}
                for addr, v in mapping.items()
            ],
        )


def insert_many_ignore(conn, table: Table, rows: Iterable[dict]) -> int:
    """Insert, ignoring rows that collide with existing primary keys."""
    rows = list(rows)
    if not rows:
        return 0
    stmt = insert(table).prefix_with("OR IGNORE")
    conn.execute(stmt, rows)
    return len(rows)


def relabel_flows(engine: Engine, game: str, label_map: Dict[str, dict]) -> int:
    """Rewrite category/label_name for existing flows based on label_map.

    Also resets rows whose counterparty is no longer in the map back to
    UNLABELED — so removing a label takes effect too.

    Returns the number of rows updated.
    """
    updated = 0
    with engine.begin() as conn:
        # Reset everything that isn't a `wager` or `payout` (those come from
        # game-event enrichment and aren't tied to the address label map).
        conn.execute(
            text(
                """
                UPDATE flows SET category = 'UNLABELED', label_name = NULL
                WHERE game = :g
                  AND direction = 'out'
                  AND category NOT IN ('payout')
                """
            ),
            {"g": game},
        )
        conn.execute(
            text(
                """
                UPDATE flows SET category = 'UNLABELED_IN', label_name = NULL
                WHERE game = :g
                  AND direction = 'in'
                  AND category NOT IN ('wager')
                """
            ),
            {"g": game},
        )
        for addr, meta in label_map.items():
            res = conn.execute(
                text(
                    """
                    UPDATE flows
                    SET category = :cat, label_name = :name
                    WHERE game = :g AND counterparty = :addr
                      AND category IN ('UNLABELED', 'UNLABELED_IN')
                    """
                ),
                {
                    "cat": meta["category"],
                    "name": meta["name"],
                    "g": game,
                    "addr": addr.lower(),
                },
            )
            updated += res.rowcount or 0
    return updated
