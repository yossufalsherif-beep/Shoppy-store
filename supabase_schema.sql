-- Shopy Store V4.2.2 - STAGING ONLY. Paste in Supabase SQL Editor.
-- All tables have RLS enabled and NO anon/authenticated policies.
-- The Flask server connects using private PostgreSQL credentials.
-- No demo products, no manual shipping fees, no orders created.
BEGIN;
CREATE TABLE IF NOT EXISTS users(
 id BIGSERIAL PRIMARY KEY,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,
 role TEXT NOT NULL DEFAULT 'customer'
);
CREATE TABLE IF NOT EXISTS products(
 id BIGSERIAL PRIMARY KEY,title TEXT NOT NULL,slug TEXT UNIQUE NOT NULL,
 description TEXT NOT NULL DEFAULT '',price BIGINT NOT NULL CHECK(price>=0),
 image TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
 options_json TEXT NOT NULL DEFAULT '{}',category TEXT NOT NULL DEFAULT '',
 supplier_cost BIGINT,source TEXT NOT NULL DEFAULT 'manual',
 supplier_product_id TEXT,supplier_product_code TEXT,supplier_stock INTEGER,
 supplier_last_sync TEXT,supplier_available INTEGER,supplier_rating REAL,
 supplier_review_count INTEGER,supplier_listed INTEGER NOT NULL DEFAULT 1,
 supplier_catalog_generation TEXT
);
CREATE TABLE IF NOT EXISTS shipping(
 id BIGSERIAL PRIMARY KEY,governorate TEXT UNIQUE NOT NULL,
 fee BIGINT NOT NULL CHECK(fee>=0),supplier_cost BIGINT
);
CREATE TABLE IF NOT EXISTS orders(
 id BIGSERIAL PRIMARY KEY,reference TEXT UNIQUE NOT NULL,user_id BIGINT REFERENCES users(id),
 customer TEXT NOT NULL,phone TEXT NOT NULL,governorate TEXT NOT NULL,
 address TEXT NOT NULL,shipping_fee BIGINT NOT NULL,total BIGINT NOT NULL,
 status TEXT NOT NULL DEFAULT 'new',created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
 checkout_key TEXT,utm_source TEXT NOT NULL DEFAULT '',utm_campaign TEXT NOT NULL DEFAULT '',
 shipping_supplier_cost BIGINT, safka_shipping_pricing_document_id TEXT
);
CREATE TABLE IF NOT EXISTS order_items(
 id BIGSERIAL PRIMARY KEY,order_id BIGINT NOT NULL REFERENCES orders(id),
 product_id BIGINT REFERENCES products(id),title TEXT NOT NULL,unit_price BIGINT NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity>0),variant_json TEXT NOT NULL DEFAULT '{}',
 unit_cost BIGINT
);
CREATE TABLE IF NOT EXISTS product_media(
 id BIGSERIAL PRIMARY KEY,product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('image','video')),
 path TEXT NOT NULL,position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS store_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS shipping_zones(
 id BIGSERIAL PRIMARY KEY,governorate TEXT NOT NULL,city TEXT NOT NULL DEFAULT '',
 area TEXT NOT NULL DEFAULT '',fee BIGINT NOT NULL CHECK(fee>=0),supplier_cost BIGINT,
 UNIQUE(governorate,city,area)
);
CREATE TABLE IF NOT EXISTS notification_outbox(
 id BIGSERIAL PRIMARY KEY,event_key TEXT NOT NULL UNIQUE,order_ref TEXT NOT NULL,
 recipient TEXT NOT NULL,template_params_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('disabled','pending','sending','accepted','failed','unknown','delivered')),
 provider_message_id TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS safka_shipping_rates(
 id BIGSERIAL PRIMARY KEY,governorate TEXT NOT NULL,city TEXT NOT NULL DEFAULT '',
 area TEXT NOT NULL DEFAULT '',supplier_fee BIGINT NOT NULL,
 fee BIGINT NOT NULL,updated_at TEXT NOT NULL,
 pricing_document_id TEXT,UNIQUE(governorate,city,area)
);
CREATE TABLE IF NOT EXISTS safka_sync_runs(
 id BIGSERIAL PRIMARY KEY,kind TEXT NOT NULL,updated INTEGER NOT NULL,
 hidden INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,
 source_label TEXT NOT NULL DEFAULT 'local-json'
);
CREATE TABLE IF NOT EXISTS safka_order_outbox(
 id BIGSERIAL PRIMARY KEY,order_id BIGINT NOT NULL UNIQUE REFERENCES orders(id),
 status TEXT NOT NULL DEFAULT 'awaiting_provider_configuration',
 supplier_order_id TEXT,error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 attempt_count INTEGER NOT NULL DEFAULT 0, submitted_commission_piastres BIGINT
);
CREATE TABLE IF NOT EXISTS safka_schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS product_overrides(
 product_id BIGINT PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
 title TEXT NOT NULL,description TEXT NOT NULL,category TEXT NOT NULL,
 price BIGINT NOT NULL,options_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS favorites(
 user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
 created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
 PRIMARY KEY(user_id,product_id)
);
CREATE TABLE IF NOT EXISTS product_reviews(
 id BIGSERIAL PRIMARY KEY,
 product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
 user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
 comment TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
 updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
 UNIQUE(product_id,user_id),
 CHECK(rating > 3 OR length(trim(comment))>=10)
);
CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id);
CREATE INDEX IF NOT EXISTS idx_reviews_product ON product_reviews(product_id,created_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_checkout_key ON orders(checkout_key);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(active,category);
CREATE INDEX IF NOT EXISTS idx_orders_utm ON orders(utm_source,utm_campaign);
CREATE UNIQUE INDEX IF NOT EXISTS idx_supplier_product ON products(source,supplier_product_id);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_status ON notification_outbox(status,id);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_order ON notification_outbox(order_ref);
-- Supabase Data API protection. No policies = no table access via public keys.
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE products ENABLE ROW LEVEL SECURITY;
ALTER TABLE shipping ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_media ENABLE ROW LEVEL SECURITY;
ALTER TABLE store_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE shipping_zones ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE safka_shipping_rates ENABLE ROW LEVEL SECURITY;
ALTER TABLE safka_sync_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE safka_order_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE safka_schema_meta ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_overrides ENABLE ROW LEVEL SECURITY;
ALTER TABLE favorites ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_reviews ENABLE ROW LEVEL SECURITY;
COMMIT;
