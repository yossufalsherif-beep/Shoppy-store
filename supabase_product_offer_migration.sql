-- Forward-only, additive migration: per-product sale offer + free shipping.
-- Run ONCE in the Supabase SQL Editor BEFORE merging/deploying this release.
-- Does not modify existing products' prices, orders, customers or shipping rates.
-- Safka supplier sync never writes these columns, so manual values survive re-syncs.
BEGIN;
-- Original ("before discount") price in piastres, shown struck through. NULL = no offer.
ALTER TABLE public.products ADD COLUMN IF NOT EXISTS compare_at_price BIGINT;
ALTER TABLE public.products DROP CONSTRAINT IF EXISTS products_compare_at_price_check;
ALTER TABLE public.products ADD CONSTRAINT products_compare_at_price_check
  CHECK (compare_at_price IS NULL OR compare_at_price > 0);
-- 1 = an order containing this product ships free (merchant-funded delivery).
ALTER TABLE public.products ADD COLUMN IF NOT EXISTS free_shipping INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.products DROP CONSTRAINT IF EXISTS products_free_shipping_check;
ALTER TABLE public.products ADD CONSTRAINT products_free_shipping_check
  CHECK (free_shipping IN (0,1));
-- Immutable order snapshot: was delivery free because of a free-shipping product?
-- Used by the Safka order dry-run / profit reconciliation.
ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS free_shipping_applied INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.orders DROP CONSTRAINT IF EXISTS orders_free_shipping_applied_check;
ALTER TABLE public.orders ADD CONSTRAINT orders_free_shipping_applied_check
  CHECK (free_shipping_applied IN (0,1));
COMMIT;

-- Optional check after running:
-- SELECT column_name FROM information_schema.columns
--  WHERE table_schema='public' AND table_name IN ('products','orders')
--    AND column_name IN ('compare_at_price','free_shipping','free_shipping_applied');
