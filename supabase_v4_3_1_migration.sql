-- Additive migration for an EXISTING Supabase project. Run once in SQL Editor.
-- Does not delete orders, shipping records, products, or users.
ALTER TABLE public.safka_shipping_rates
  ADD COLUMN IF NOT EXISTS pricing_document_id TEXT;
ALTER TABLE public.orders
  ADD COLUMN IF NOT EXISTS safka_shipping_pricing_document_id TEXT;
ALTER TABLE public.safka_order_outbox
  ADD COLUMN IF NOT EXISTS submitted_commission_piastres BIGINT;
