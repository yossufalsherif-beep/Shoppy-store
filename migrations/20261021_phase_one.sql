-- Idempotent schema preparation for Phase 1 (Supabase PostgreSQL).
-- Applied to production via Supabase migration: phase_one_customer_phone_consent_funnel_20261021.
-- Existing customer and order records are retained.
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS phone TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS public.analytics_counters (
  day TEXT NOT NULL,
  stage TEXT NOT NULL CHECK (stage IN (
   'home','product_view','cart_add','cart_view','checkout_view','shipping_quote','order_created')),
  visits INTEGER NOT NULL DEFAULT 0 CHECK (visits >= 0),
  PRIMARY KEY (day,stage)
);
ALTER TABLE public.analytics_counters ENABLE ROW LEVEL SECURITY;
REVOKE ALL PRIVILEGES ON public.analytics_counters FROM anon, authenticated;
