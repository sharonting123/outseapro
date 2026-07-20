from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .catalog import sample_catalog


DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "soutui.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spus (
  spu_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  brand TEXT NOT NULL,
  cate_l1 TEXT NOT NULL,
  cate_l2 TEXT NOT NULL,
  rating REAL NOT NULL,
  keywords_json TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  merchant_id TEXT NOT NULL DEFAULT 'merchant_demo',
  status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS skus (
  sku_id TEXT PRIMARY KEY,
  spu_id TEXT NOT NULL REFERENCES spus(spu_id),
  price REAL NOT NULL,
  stock INTEGER NOT NULL,
  sales INTEGER NOT NULL,
  attrs_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cart_items (
  user_id TEXT NOT NULL,
  sku_id TEXT NOT NULL,
  qty INTEGER NOT NULL,
  request_id TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (user_id, sku_id)
);

CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  total REAL NOT NULL,
  status TEXT NOT NULL,
  created_at REAL NOT NULL,
  currency TEXT NOT NULL DEFAULT 'cny',
  payment_provider TEXT NOT NULL DEFAULT '',
  payment_session_id TEXT NOT NULL DEFAULT '',
  paid_at REAL
);

CREATE TABLE IF NOT EXISTS order_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT NOT NULL REFERENCES orders(order_id),
  spu_id TEXT NOT NULL,
  sku_id TEXT NOT NULL,
  title TEXT NOT NULL,
  attrs_json TEXT NOT NULL,
  price REAL NOT NULL,
  qty INTEGER NOT NULL,
  request_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  request_id TEXT NOT NULL DEFAULT '',
  user_id TEXT NOT NULL,
  scene TEXT NOT NULL DEFAULT '',
  query TEXT NOT NULL DEFAULT '',
  spu_id TEXT NOT NULL DEFAULT '',
  sku_id TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL DEFAULT -1,
  is_ad INTEGER NOT NULL DEFAULT 0,
  ad_id TEXT NOT NULL DEFAULT '',
  pctr REAL,
  pcvr REAL,
  features_json TEXT NOT NULL DEFAULT '{}',
  extra_json TEXT NOT NULL DEFAULT '{}',
  ts REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(event_type, ts);
CREATE INDEX IF NOT EXISTS idx_events_req_sku ON events(request_id, sku_id);
CREATE INDEX IF NOT EXISTS idx_skus_spu ON skus(spu_id);

CREATE TABLE IF NOT EXISTS users (
  user_id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'customer',
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS model_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  artifact_path TEXT NOT NULL DEFAULT '',
  metrics_json TEXT NOT NULL DEFAULT '{}',
  sample_count INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  finished_at REAL
);

CREATE TABLE IF NOT EXISTS model_artifacts (
  run_id TEXT PRIMARY KEY REFERENCES model_runs(run_id) ON DELETE CASCADE,
  artifact_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
"""

_POSTGRES_SCHEMA = _SCHEMA.replace(
    "id INTEGER PRIMARY KEY AUTOINCREMENT", "id BIGSERIAL PRIMARY KEY"
)


def _is_postgres_url(value: str) -> bool:
    return value.startswith(("postgres://", "postgresql://"))


def _adapt_postgres_sql(sql: str) -> str:
    sql = sql.replace("BEGIN IMMEDIATE", "BEGIN")
    sql = sql.replace("MAX(0,sales-?)", "GREATEST(0,sales-?)")
    return sql.replace("?", "%s")


class _PgConnection:
    """Small DB-API compatibility layer so the repository works on SQLite and psycopg."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._conn = pool.getconn()
        self._closed = False

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] | None = None):
        return self._conn.execute(_adapt_postgres_sql(sql), params)

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            if statement.strip():
                self._conn.execute(statement)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        if not self._closed:
            self._pool.putconn(self._conn)
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()


class Store:
    """SQLite for local development; PostgreSQL/Supabase when DATABASE_URL is set."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        target = str(db_path) if db_path is not None else os.getenv("DATABASE_URL", str(DEFAULT_DB))
        self.is_postgres = _is_postgres_url(target)
        self.database_url = target if self.is_postgres else ""
        self.db_path = DEFAULT_DB if self.is_postgres else Path(target)
        self._pool = None
        if self.is_postgres:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(
                conninfo=self.database_url,
                min_size=0,
                max_size=int(os.getenv("DB_POOL_SIZE", "10")),
                # Supabase's transaction pooler does not support named prepared
                # statements consistently across borrowed server connections.
                kwargs={"row_factory": dict_row, "prepare_threshold": None},
                open=True,
            )
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def connect(self):
        if self.is_postgres:
            return _PgConnection(self._pool)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def close(self) -> None:
        """Release PostgreSQL pool resources (SQLite connections are per operation)."""
        if self._pool is not None:
            self._pool.close()

    def _init_db(self) -> None:
        with self._lock:
            conn = self.connect()
            try:
                conn.executescript(_POSTGRES_SCHEMA if self.is_postgres else _SCHEMA)
                if not self.is_postgres:
                    self._migrate(conn)
                n = conn.execute("SELECT COUNT(*) AS c FROM spus").fetchone()["c"]
                if n == 0:
                    self._seed(conn)
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Small idempotent migrations for databases created by older releases."""
        additions = {
            "spus": [("merchant_id", "TEXT NOT NULL DEFAULT 'merchant_demo'"), ("status", "TEXT NOT NULL DEFAULT 'active'")],
            "cart_items": [("request_id", "TEXT NOT NULL DEFAULT ''")],
            "orders": [
                ("currency", "TEXT NOT NULL DEFAULT 'cny'"),
                ("payment_provider", "TEXT NOT NULL DEFAULT ''"),
                ("payment_session_id", "TEXT NOT NULL DEFAULT ''"),
                ("paid_at", "REAL"),
            ],
            "order_items": [("request_id", "TEXT NOT NULL DEFAULT ''")],
        }
        for table, columns in additions.items():
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _seed(self, conn: Any) -> None:
        spus, skus = sample_catalog()
        for s in spus:
            conn.execute(
                "INSERT INTO spus(spu_id,title,brand,cate_l1,cate_l2,rating,keywords_json,tags_json) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(spu_id) DO NOTHING",
                (
                    s.spu_id,
                    s.title,
                    s.brand,
                    s.cate_l1,
                    s.cate_l2,
                    s.rating,
                    json.dumps(list(s.keywords), ensure_ascii=False),
                    json.dumps(list(s.tags), ensure_ascii=False),
                ),
            )
        for k in skus:
            conn.execute(
                "INSERT INTO skus(sku_id,spu_id,price,stock,sales,attrs_json) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(sku_id) DO NOTHING",
                (
                    k.sku_id,
                    k.spu_id,
                    k.price,
                    k.stock,
                    k.sales,
                    json.dumps(k.attrs, ensure_ascii=False),
                ),
            )

    def reset_seed(self) -> None:
        """测试用：清空并重新灌种子。"""
        with self._lock:
            conn = self.connect()
            try:
                for t in ("order_items", "orders", "cart_items", "events", "skus", "spus"):
                    conn.execute(f"DELETE FROM {t}")
                self._seed(conn)
                conn.commit()
            finally:
                conn.close()

    # ----- catalog -----

    def get_spu(self, spu_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self.connect()
            try:
                row = conn.execute("SELECT * FROM spus WHERE spu_id=?", (spu_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def load_catalog(self):
        from .models import Sku, Spu
        seed_embeddings = {s.spu_id: s.embedding for s in sample_catalog()[0]}
        with self._lock:
            conn = self.connect()
            try:
                spus = [
                    Spu(
                        spu_id=r["spu_id"], title=r["title"], brand=r["brand"],
                        cate_l1=r["cate_l1"], cate_l2=r["cate_l2"], rating=float(r["rating"]),
                        keywords=tuple(json.loads(r["keywords_json"])), tags=tuple(json.loads(r["tags_json"])),
                        embedding=seed_embeddings.get(r["spu_id"], ()),
                    )
                    for r in conn.execute("SELECT * FROM spus WHERE status='active'").fetchall()
                ]
                ids = {s.spu_id for s in spus}
                skus = [
                    Sku(
                        sku_id=r["sku_id"], spu_id=r["spu_id"], price=float(r["price"]),
                        stock=int(r["stock"]), sales=int(r["sales"]), attrs=json.loads(r["attrs_json"]),
                    )
                    for r in conn.execute("SELECT * FROM skus").fetchall() if r["spu_id"] in ids
                ]
                return spus, skus
            finally:
                conn.close()

    def list_skus(self, spu_id: str) -> list[dict[str, Any]]:
        with self._lock:
            conn = self.connect()
            try:
                rows = conn.execute("SELECT * FROM skus WHERE spu_id=? ORDER BY sales DESC", (spu_id,)).fetchall()
                out = []
                for r in rows:
                    d = dict(r)
                    d["attrs"] = json.loads(d.pop("attrs_json"))
                    out.append(d)
                return out
            finally:
                conn.close()

    def get_sku(self, sku_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self.connect()
            try:
                row = conn.execute("SELECT * FROM skus WHERE sku_id=?", (sku_id,)).fetchone()
                if not row:
                    return None
                d = dict(row)
                d["attrs"] = json.loads(d.pop("attrs_json"))
                return d
            finally:
                conn.close()

    def get_stock(self, sku_id: str) -> int:
        sku = self.get_sku(sku_id)
        return int(sku["stock"]) if sku else 0

    def set_stock(self, sku_id: str, stock: int) -> None:
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("UPDATE skus SET stock=? WHERE sku_id=?", (max(0, int(stock)), sku_id))
                conn.commit()
            finally:
                conn.close()

    def dec_stock(self, sku_id: str, qty: int) -> bool:
        """原子扣减；库存不足返回 False。"""
        with self._lock:
            conn = self.connect()
            try:
                cur = conn.execute(
                    "UPDATE skus SET stock = stock - ?, sales = sales + ? "
                    "WHERE sku_id=? AND stock >= ?",
                    (qty, qty, sku_id, qty),
                )
                conn.commit()
                return cur.rowcount == 1
            finally:
                conn.close()

    # ----- cart -----

    def cart_list(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock:
            conn = self.connect()
            try:
                rows = conn.execute(
                    "SELECT c.qty, s.*, p.title, p.brand, p.spu_id AS spu_id2 "
                    "FROM cart_items c "
                    "JOIN skus s ON s.sku_id=c.sku_id "
                    "JOIN spus p ON p.spu_id=s.spu_id "
                    "WHERE c.user_id=?",
                    (user_id,),
                ).fetchall()
                out = []
                for r in rows:
                    d = dict(r)
                    d["attrs"] = json.loads(d.pop("attrs_json"))
                    d["spu_id"] = d.get("spu_id") or d.get("spu_id2")
                    out.append(d)
                return out
            finally:
                conn.close()

    def cart_set(self, user_id: str, sku_id: str, qty: int, request_id: str = "") -> None:
        with self._lock:
            conn = self.connect()
            try:
                if qty <= 0:
                    conn.execute("DELETE FROM cart_items WHERE user_id=? AND sku_id=?", (user_id, sku_id))
                else:
                    conn.execute(
                        "INSERT INTO cart_items(user_id,sku_id,qty,request_id) VALUES(?,?,?,?) "
                        "ON CONFLICT(user_id,sku_id) DO UPDATE SET qty=excluded.qty, "
                        "request_id=CASE WHEN excluded.request_id='' THEN cart_items.request_id ELSE excluded.request_id END",
                        (user_id, sku_id, qty, request_id),
                    )
                conn.commit()
            finally:
                conn.close()

    def cart_add(self, user_id: str, sku_id: str, qty: int = 1, request_id: str = "") -> int:
        with self._lock:
            conn = self.connect()
            try:
                row = conn.execute(
                    "SELECT qty FROM cart_items WHERE user_id=? AND sku_id=?",
                    (user_id, sku_id),
                ).fetchone()
                new_qty = (row["qty"] if row else 0) + qty
                if new_qty <= 0:
                    conn.execute("DELETE FROM cart_items WHERE user_id=? AND sku_id=?", (user_id, sku_id))
                    new_qty = 0
                else:
                    conn.execute(
                        "INSERT INTO cart_items(user_id,sku_id,qty,request_id) VALUES(?,?,?,?) "
                        "ON CONFLICT(user_id,sku_id) DO UPDATE SET qty=excluded.qty, "
                        "request_id=CASE WHEN excluded.request_id='' THEN cart_items.request_id ELSE excluded.request_id END",
                        (user_id, sku_id, new_qty, request_id),
                    )
                conn.commit()
                return new_qty
            finally:
                conn.close()

    def cart_clear(self, user_id: str) -> None:
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("DELETE FROM cart_items WHERE user_id=?", (user_id,))
                conn.commit()
            finally:
                conn.close()

    def cart_count(self, user_id: str) -> int:
        with self._lock:
            conn = self.connect()
            try:
                row = conn.execute(
                    "SELECT COALESCE(SUM(qty),0) AS c FROM cart_items WHERE user_id=?",
                    (user_id,),
                ).fetchone()
                return int(row["c"])
            finally:
                conn.close()

    # ----- orders -----

    def create_order(
        self,
        order_id: str,
        user_id: str,
        total: float,
        items: list[dict[str, Any]],
        created_at: float,
    ) -> None:
        with self._lock:
            conn = self.connect()
            try:
                conn.execute(
                    "INSERT INTO orders(order_id,user_id,total,status,created_at) VALUES(?,?,?,?,?)",
                    (order_id, user_id, total, "paid", created_at),
                )
                for it in items:
                    conn.execute(
                        "INSERT INTO order_items(order_id,spu_id,sku_id,title,attrs_json,price,qty) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            order_id,
                            it["spu_id"],
                            it["sku_id"],
                            it["title"],
                            json.dumps(it.get("attrs") or {}, ensure_ascii=False),
                            it["price"],
                            it["qty"],
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self.connect()
            try:
                order = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
                if not order:
                    return None
                items = conn.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()
                out = dict(order)
                out["items"] = []
                for r in items:
                    d = dict(r)
                    d["attrs"] = json.loads(d.pop("attrs_json"))
                    out["items"].append(d)
                return out
            finally:
                conn.close()

    def get_order_by_payment_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self.connect()
            try:
                row = conn.execute("SELECT order_id FROM orders WHERE payment_session_id=?", (session_id,)).fetchone()
            finally:
                conn.close()
        return self.get_order(row["order_id"]) if row else None

    def reserve_cart_order(self, order_id: str, user_id: str, created_at: float) -> dict[str, Any]:
        """Atomically validate cart, reserve stock, create a pending order and clear cart."""
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT c.qty,c.request_id,s.sku_id,s.spu_id,s.price,s.stock,s.attrs_json,p.title "
                    "FROM cart_items c JOIN skus s ON s.sku_id=c.sku_id "
                    "JOIN spus p ON p.spu_id=s.spu_id WHERE c.user_id=?",
                    (user_id,),
                ).fetchall()
                if not rows:
                    raise ValueError("购物车为空")
                total = 0.0
                items: list[dict[str, Any]] = []
                for row in rows:
                    qty = int(row["qty"])
                    if qty <= 0 or int(row["stock"]) < qty:
                        raise ValueError(f"{row['title']} 库存不足")
                    cur = conn.execute(
                        "UPDATE skus SET stock=stock-?,sales=sales+? WHERE sku_id=? AND stock>=?",
                        (qty, qty, row["sku_id"], qty),
                    )
                    if cur.rowcount != 1:
                        raise ValueError(f"{row['title']} 库存锁定失败")
                    item = dict(row)
                    item["attrs"] = json.loads(item.pop("attrs_json"))
                    item["qty"] = qty
                    total += float(row["price"]) * qty
                    items.append(item)
                total = round(total, 2)
                conn.execute(
                    "INSERT INTO orders(order_id,user_id,total,status,created_at,currency) VALUES(?,?,?,?,?,?)",
                    (order_id, user_id, total, "pending_payment", created_at, "cny"),
                )
                for it in items:
                    conn.execute(
                        "INSERT INTO order_items(order_id,spu_id,sku_id,title,attrs_json,price,qty,request_id) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (order_id, it["spu_id"], it["sku_id"], it["title"], json.dumps(it["attrs"], ensure_ascii=False), it["price"], it["qty"], it["request_id"]),
                    )
                conn.execute("DELETE FROM cart_items WHERE user_id=?", (user_id,))
                conn.commit()
                return {"order_id": order_id, "user_id": user_id, "total": total, "status": "pending_payment", "items": items}
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def set_payment_session(self, order_id: str, provider: str, session_id: str) -> None:
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("UPDATE orders SET payment_provider=?,payment_session_id=? WHERE order_id=?", (provider, session_id, order_id))
                conn.commit()
            finally:
                conn.close()

    def mark_order_paid(self, order_id: str, payment_session_id: str = "") -> bool:
        """Idempotently transition pending -> paid. Returns True only once."""
        with self._lock:
            conn = self.connect()
            try:
                cur = conn.execute(
                    "UPDATE orders SET status='paid',paid_at=?,payment_session_id=CASE WHEN ?='' THEN payment_session_id ELSE ? END "
                    "WHERE order_id=? AND status='pending_payment'",
                    (time.time(), payment_session_id, payment_session_id, order_id),
                )
                conn.commit()
                return cur.rowcount == 1
            finally:
                conn.close()

    def cancel_order(self, order_id: str, *, restore_cart: bool = False) -> bool:
        """Idempotently cancel a pending order and release reserved inventory."""
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                order = conn.execute("SELECT user_id,status FROM orders WHERE order_id=?", (order_id,)).fetchone()
                if not order or order["status"] != "pending_payment":
                    conn.rollback()
                    return False
                items = conn.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()
                for it in items:
                    conn.execute("UPDATE skus SET stock=stock+?,sales=MAX(0,sales-?) WHERE sku_id=?", (it["qty"], it["qty"], it["sku_id"]))
                    if restore_cart:
                        conn.execute(
                            "INSERT INTO cart_items(user_id,sku_id,qty,request_id) VALUES(?,?,?,?) "
                            "ON CONFLICT(user_id,sku_id) DO UPDATE SET qty=cart_items.qty+excluded.qty",
                            (order["user_id"], it["sku_id"], it["qty"], it["request_id"]),
                        )
                conn.execute("UPDATE orders SET status='cancelled' WHERE order_id=?", (order_id,))
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ----- users / sessions -----

    def create_user(self, user_id: str, email: str, password_hash: str, display_name: str, role: str = "customer") -> dict[str, Any]:
        with self._lock:
            conn = self.connect()
            try:
                conn.execute(
                    "INSERT INTO users(user_id,email,password_hash,display_name,role,created_at) VALUES(?,?,?,?,?,?)",
                    (user_id, email.strip().lower(), password_hash, display_name.strip(), role, time.time()),
                )
                conn.commit()
                return self._public_user(conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())
            finally:
                conn.close()

    @staticmethod
    def _public_user(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d.pop("password_hash", None)
        return d

    def get_user_by_email(self, email: str, include_hash: bool = False) -> dict[str, Any] | None:
        with self._lock:
            conn = self.connect()
            try:
                row = conn.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
                if not row:
                    return None
                d = dict(row)
                if not include_hash:
                    d.pop("password_hash", None)
                return d
            finally:
                conn.close()

    def create_session(self, token: str, user_id: str, expires_at: float) -> None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
                conn.execute("INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)", (token_hash, user_id, expires_at, time.time()))
                conn.commit()
            finally:
                conn.close()

    def session_user(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            conn = self.connect()
            try:
                row = conn.execute(
                    "SELECT u.* FROM sessions s JOIN users u ON u.user_id=s.user_id "
                    "WHERE s.token_hash=? AND s.expires_at>? AND u.is_active=1",
                    (token_hash, time.time()),
                ).fetchone()
                return self._public_user(row) if row else None
            finally:
                conn.close()

    def delete_session(self, token: str) -> None:
        if not token:
            return
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
                conn.commit()
            finally:
                conn.close()

    def update_password(self, user_id: str, password_hash: str) -> None:
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute("UPDATE users SET password_hash=? WHERE user_id=?", (password_hash, user_id))
                if cur.rowcount != 1:
                    raise ValueError("用户不存在")
                conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ----- merchant / models -----

    def merchant_products(self, merchant_id: str) -> list[dict[str, Any]]:
        with self._lock:
            conn = self.connect()
            try:
                rows = conn.execute(
                    "SELECT p.spu_id,p.title,p.brand,p.status,s.sku_id,s.price,s.stock,s.sales,s.attrs_json "
                    "FROM spus p JOIN skus s ON s.spu_id=p.spu_id WHERE p.merchant_id=? ORDER BY p.spu_id,s.sku_id",
                    (merchant_id,),
                ).fetchall()
                out=[]
                for r in rows:
                    d=dict(r); d["attrs"]=json.loads(d.pop("attrs_json")); out.append(d)
                return out
            finally:
                conn.close()

    def merchant_orders(self, merchant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            conn = self.connect()
            try:
                return [dict(r) for r in conn.execute(
                    "SELECT DISTINCT o.* FROM orders o JOIN order_items i ON i.order_id=o.order_id "
                    "JOIN spus p ON p.spu_id=i.spu_id WHERE p.merchant_id=? ORDER BY o.created_at DESC LIMIT ?",
                    (merchant_id, limit),
                ).fetchall()]
            finally:
                conn.close()

    def update_merchant_sku(self, merchant_id: str, sku_id: str, price: float, stock: int) -> bool:
        with self._lock:
            conn=self.connect()
            try:
                cur=conn.execute(
                    "UPDATE skus SET price=?,stock=? WHERE sku_id=? AND spu_id IN (SELECT spu_id FROM spus WHERE merchant_id=?)",
                    (round(float(price),2),max(0,int(stock)),sku_id,merchant_id),
                ); conn.commit(); return cur.rowcount==1
            finally:
                conn.close()

    def create_merchant_product(
        self, merchant_id: str, *, spu_id: str, sku_id: str, title: str, brand: str,
        cate_l1: str, cate_l2: str, price: float, stock: int,
    ) -> None:
        if not all(x.strip() for x in (spu_id, sku_id, title, brand, cate_l1, cate_l2)):
            raise ValueError("商品字段不能为空")
        if price <= 0 or stock < 0:
            raise ValueError("价格或库存无效")
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO spus(spu_id,title,brand,cate_l1,cate_l2,rating,keywords_json,tags_json,merchant_id,status) "
                    "VALUES(?,?,?,?,?,4.5,?,?,?,'active')",
                    (spu_id.strip(), title.strip(), brand.strip(), cate_l1.strip(), cate_l2.strip(), json.dumps([title, brand, cate_l2], ensure_ascii=False), "[]", merchant_id),
                )
                conn.execute(
                    "INSERT INTO skus(sku_id,spu_id,price,stock,sales,attrs_json) VALUES(?,?,?,?,0,'{}')",
                    (sku_id.strip(), spu_id.strip(), round(float(price), 2), int(stock)),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def add_model_run(self, run_id: str, status: str, artifact_path: str = "") -> None:
        with self._lock:
            conn=self.connect()
            try:
                conn.execute("INSERT INTO model_runs(run_id,status,artifact_path,created_at) VALUES(?,?,?,?)", (run_id,status,artifact_path,time.time())); conn.commit()
            finally: conn.close()

    def finish_model_run(self, run_id: str, status: str, metrics: dict[str, Any], sample_count: int) -> None:
        with self._lock:
            conn=self.connect()
            try:
                conn.execute("UPDATE model_runs SET status=?,metrics_json=?,sample_count=?,finished_at=? WHERE run_id=?", (status,json.dumps(metrics,ensure_ascii=False),sample_count,time.time(),run_id)); conn.commit()
            finally: conn.close()

    def latest_model_run(self) -> dict[str, Any] | None:
        with self._lock:
            conn=self.connect()
            try:
                row=conn.execute("SELECT * FROM model_runs ORDER BY created_at DESC LIMIT 1").fetchone()
                if not row: return None
                d=dict(row); d["metrics"]=json.loads(d.pop("metrics_json")); return d
            finally: conn.close()

    def save_model_artifact(self, run_id: str, artifact: dict[str, Any]) -> None:
        payload = json.dumps(artifact, ensure_ascii=False)
        with self._lock:
            conn = self.connect()
            try:
                conn.execute(
                    "INSERT INTO model_artifacts(run_id,artifact_json,created_at) VALUES(?,?,?) "
                    "ON CONFLICT(run_id) DO UPDATE SET artifact_json=excluded.artifact_json,created_at=excluded.created_at",
                    (run_id, payload, time.time()),
                )
                conn.commit()
            finally:
                conn.close()

    def latest_model_artifact(self) -> dict[str, Any] | None:
        with self._lock:
            conn = self.connect()
            try:
                row = conn.execute(
                    "SELECT a.artifact_json FROM model_artifacts a JOIN model_runs r ON r.run_id=a.run_id "
                    "WHERE r.status='ready' ORDER BY r.finished_at DESC LIMIT 1"
                ).fetchone()
                return json.loads(row["artifact_json"]) if row else None
            finally:
                conn.close()

    # ----- events -----

    def insert_event(
        self,
        *,
        event_type: str,
        user_id: str,
        request_id: str = "",
        scene: str = "",
        query: str = "",
        spu_id: str = "",
        sku_id: str = "",
        position: int = -1,
        is_ad: bool = False,
        ad_id: str = "",
        pctr: float | None = None,
        pcvr: float | None = None,
        features: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        ts: float,
    ) -> int:
        with self._lock:
            conn = self.connect()
            try:
                sql = (
                    "INSERT INTO events(event_type,request_id,user_id,scene,query,spu_id,sku_id,"
                    "position,is_ad,ad_id,pctr,pcvr,features_json,extra_json,ts) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                )
                if self.is_postgres:
                    sql += " RETURNING id"
                cur = conn.execute(
                    sql,
                    (
                        event_type,
                        request_id,
                        user_id,
                        scene,
                        query,
                        spu_id,
                        sku_id,
                        position,
                        1 if is_ad else 0,
                        ad_id,
                        pctr,
                        pcvr,
                        json.dumps(features or {}, ensure_ascii=False),
                        json.dumps(extra or {}, ensure_ascii=False),
                        ts,
                    ),
                )
                event_id = int(cur.fetchone()["id"]) if self.is_postgres else int(cur.lastrowid)
                conn.commit()
                return event_id
            finally:
                conn.close()

    def list_events(self, limit: int = 100, event_type: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            conn = self.connect()
            try:
                if event_type:
                    rows = conn.execute(
                        "SELECT * FROM events WHERE event_type=? ORDER BY id DESC LIMIT ?",
                        (event_type, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()


_store: Store | None = None


def get_store(db_path: Path | str | None = None) -> Store:
    global _store
    if db_path is not None:
        return Store(db_path)
    if _store is None:
        _store = Store()
    return _store
