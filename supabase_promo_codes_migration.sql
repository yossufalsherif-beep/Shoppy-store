-- Forward-only migration; execute only after merchant approval, before production deploy.
-- Does not modify existing orders, prices or customers.
BEGIN;
CREATE TABLE IF NOT EXISTS public.promo_codes(
 code TEXT PRIMARY KEY,
 kind TEXT NOT NULL DEFAULT 'percent',
 percent INTEGER NOT NULL DEFAULT 0 CHECK(percent BETWEEN 0 AND 100),
 amount_piastres BIGINT NOT NULL DEFAULT 0 CHECK(amount_piastres>=0),
 min_items INTEGER NOT NULL DEFAULT 1 CHECK(min_items BETWEEN 1 AND 1000),
 max_discounted_items INTEGER CHECK(max_discounted_items IS NULL OR max_discounted_items BETWEEN 1 AND 1000),
 max_uses INTEGER CHECK(max_uses IS NULL OR max_uses>0),
 used_count INTEGER NOT NULL DEFAULT 0 CHECK(used_count>=0),
 starts_at BIGINT NOT NULL,
 expires_at BIGINT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 created_at BIGINT NOT NULL,
 CHECK(expires_at>starts_at)
);
-- Existing staging coupon tables, if any, can evolve without resetting uses.
ALTER TABLE public.promo_codes ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'percent';
ALTER TABLE public.promo_codes ADD COLUMN IF NOT EXISTS amount_piastres BIGINT NOT NULL DEFAULT 0;
ALTER TABLE public.promo_codes ADD COLUMN IF NOT EXISTS min_items INTEGER NOT NULL DEFAULT 1;
ALTER TABLE public.promo_codes ADD COLUMN IF NOT EXISTS max_discounted_items INTEGER;
ALTER TABLE public.promo_codes DROP CONSTRAINT IF EXISTS promo_codes_percent_check;
ALTER TABLE public.promo_codes ADD CONSTRAINT promo_codes_percent_check CHECK(percent BETWEEN 0 AND 100);
ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS promo_code TEXT NOT NULL DEFAULT '';
ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS promo_percent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS promo_discount BIGINT NOT NULL DEFAULT 0;
ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS promo_shipping_discount BIGINT NOT NULL DEFAULT 0;
ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS promo_kind TEXT NOT NULL DEFAULT '';
ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS promo_value BIGINT NOT NULL DEFAULT 0;
ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS promo_min_items INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS promo_max_items INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.promo_codes ENABLE ROW LEVEL SECURITY;
REVOKE ALL PRIVILEGES ON TABLE public.promo_codes FROM anon,authenticated;
COMMIT;
