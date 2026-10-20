-- Run after explicit owner approval, BEFORE the new app deployment.
-- Additive/idempotent; retains all existing customers, products, orders, commissions.
BEGIN;
CREATE TABLE IF NOT EXISTS public.promo_codes(
 code TEXT PRIMARY KEY,
 percent INTEGER NOT NULL CHECK(percent BETWEEN 1 AND 100),
 max_uses INTEGER CHECK(max_uses IS NULL OR max_uses>0),
 used_count INTEGER NOT NULL DEFAULT 0 CHECK(used_count>=0),
 starts_at BIGINT NOT NULL,
 expires_at BIGINT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 created_at BIGINT NOT NULL,
 CHECK(expires_at>starts_at)
);
ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS promo_code TEXT NOT NULL DEFAULT '';
ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS promo_percent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS promo_discount BIGINT NOT NULL DEFAULT 0;
ALTER TABLE public.promo_codes ENABLE ROW LEVEL SECURITY;
REVOKE ALL PRIVILEGES ON TABLE public.promo_codes FROM anon,authenticated;
COMMIT;
