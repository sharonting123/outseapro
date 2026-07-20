from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soutui.store import Store


TABLES = [
    "users", "spus", "skus", "cart_items", "orders", "order_items",
    "events", "sessions", "model_runs", "model_artifacts",
]
PRIMARY_KEYS = {
    "users": ("user_id",), "spus": ("spu_id",), "skus": ("sku_id",),
    "cart_items": ("user_id", "sku_id"), "orders": ("order_id",),
    "order_items": ("id",), "events": ("id",), "sessions": ("token_hash",),
    "model_runs": ("run_id",), "model_artifacts": ("run_id",),
}


def sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def postgres_columns(conn, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=? ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [row["column_name"] for row in rows]


def migrate(source_path: Path, database_url: str, *, replace: bool = False) -> dict[str, int]:
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    target_store = Store(database_url)
    if not target_store.is_postgres:
        raise ValueError("target must be a PostgreSQL DATABASE_URL")
    target = target_store.connect()
    counts: dict[str, int] = {}
    try:
        target.execute("BEGIN")
        if replace:
            for table in reversed(TABLES):
                target.execute(f"DELETE FROM {table}")
        for table in TABLES:
            source_cols = sqlite_columns(source, table)
            if not source_cols:
                counts[table] = 0
                continue
            target_cols = set(postgres_columns(target, table))
            columns = [c for c in source_cols if c in target_cols]
            rows = source.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()
            if not rows:
                counts[table] = 0
                continue
            keys = PRIMARY_KEYS[table]
            updates = [c for c in columns if c not in keys]
            conflict = ",".join(keys)
            action = "DO NOTHING" if not updates else "DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in updates)
            sql = (
                f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
                f"ON CONFLICT({conflict}) {action}"
            )
            for row in rows:
                target.execute(sql, tuple(row[c] for c in columns))
            counts[table] = len(rows)

        for table in ("events", "order_items"):
            target.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}','id'), "
                f"GREATEST(COALESCE((SELECT MAX(id) FROM {table}),1),1), "
                f"EXISTS(SELECT 1 FROM {table}))"
            )
        target.commit()
        return counts
    except Exception:
        target.rollback()
        raise
    finally:
        target.close()
        target_store.close()
        source.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate soutui SQLite data into Supabase/PostgreSQL")
    parser.add_argument("--source", default="data/soutui.db")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--replace", action="store_true", help="Delete target data before importing")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    counts = migrate(Path(args.source), args.database_url, replace=args.replace)
    for table, count in counts.items():
        print(f"{table}: {count}")


if __name__ == "__main__":
    main()
