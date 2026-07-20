-- Supabase/PostgreSQL schema. The application runs these idempotent statements at startup too.
CREATE TABLE IF NOT EXISTS spus (
  spu_id TEXT PRIMARY KEY, title TEXT NOT NULL, brand TEXT NOT NULL,
  cate_l1 TEXT NOT NULL, cate_l2 TEXT NOT NULL, rating REAL NOT NULL,
  keywords_json TEXT NOT NULL, tags_json TEXT NOT NULL,
  merchant_id TEXT NOT NULL DEFAULT 'merchant_demo', status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS skus (
  sku_id TEXT PRIMARY KEY, spu_id TEXT NOT NULL REFERENCES spus(spu_id),
  price REAL NOT NULL, stock INTEGER NOT NULL, sales INTEGER NOT NULL, attrs_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cart_items (
  user_id TEXT NOT NULL, sku_id TEXT NOT NULL, qty INTEGER NOT NULL,
  request_id TEXT NOT NULL DEFAULT '', PRIMARY KEY (user_id, sku_id)
);
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, total REAL NOT NULL,
  status TEXT NOT NULL, created_at REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'cny',
  payment_provider TEXT NOT NULL DEFAULT '', payment_session_id TEXT NOT NULL DEFAULT '', paid_at REAL
);
CREATE TABLE IF NOT EXISTS order_items (
  id BIGSERIAL PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(order_id),
  spu_id TEXT NOT NULL, sku_id TEXT NOT NULL, title TEXT NOT NULL, attrs_json TEXT NOT NULL,
  price REAL NOT NULL, qty INTEGER NOT NULL, request_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
  id BIGSERIAL PRIMARY KEY, event_type TEXT NOT NULL, request_id TEXT NOT NULL DEFAULT '',
  user_id TEXT NOT NULL, scene TEXT NOT NULL DEFAULT '', query TEXT NOT NULL DEFAULT '',
  spu_id TEXT NOT NULL DEFAULT '', sku_id TEXT NOT NULL DEFAULT '', position INTEGER NOT NULL DEFAULT -1,
  is_ad INTEGER NOT NULL DEFAULT 0, ad_id TEXT NOT NULL DEFAULT '', pctr REAL, pcvr REAL,
  features_json TEXT NOT NULL DEFAULT '{}', extra_json TEXT NOT NULL DEFAULT '{}', ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(event_type, ts);
CREATE INDEX IF NOT EXISTS idx_events_req_sku ON events(request_id, sku_id);
CREATE INDEX IF NOT EXISTS idx_skus_spu ON skus(spu_id);
CREATE TABLE IF NOT EXISTS users (
  user_id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
  display_name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'customer', is_active INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  expires_at REAL NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS model_runs (
  run_id TEXT PRIMARY KEY, status TEXT NOT NULL, artifact_path TEXT NOT NULL DEFAULT '',
  metrics_json TEXT NOT NULL DEFAULT '{}', sample_count INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS model_artifacts (
  run_id TEXT PRIMARY KEY REFERENCES model_runs(run_id) ON DELETE CASCADE,
  artifact_json TEXT NOT NULL, created_at REAL NOT NULL
);
