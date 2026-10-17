# Automatic customer checkout

The customer submits name, mobile, governorate, city, area and delivery address.
The server resolves supplier product/property IDs and official city/pricing IDs.
No owner draft or per-order owner approval is used by this checkout.

- Product price: supplier cost × 4/3, rounded up to a multiple of EGP 10.
- Shipping shown separately: official Safka fee + EGP 10.
- `total`: full customer COD amount, including delivery.
- `commission`: COD amount minus supplier products and supplier delivery cost.
- Signed delivery quotes expire after 30 minutes. Changed totals require review.
- A committed outbox claim permits only one supplier POST. Timeout/unknown
  responses are never retried automatically. Only a provider response with its
  order ID records acceptance. Provider errors are never exposed to customers.

## Supplier reads

Official documentation: https://public-api-docs.safka-eg.com/

`GET /api/v1/public/price-list` is collected to its authoritative governorate
count, preserving each city `id`. The complete snapshot replaces the cache
atomically. Refresh occurs automatically at checkout, at most once per minute,
with a one-hour successful cache TTL. Incomplete snapshots preserve old data
and block new checkout attempts until a valid refresh.

Product pages refresh supplier details with a five-minute cache; submission
verifies the exact product, property, available quantity and price again.
Equal-price property choices use the supplier's exact labels. Ambiguous choices
or differently priced properties without an established local price mapping
are blocked before saving/sending; the server never guesses a supplier variant.

## Production configuration

Set these **production-only** flags to `1`:
`SHOPY_PUBLIC_CHECKOUT_ENABLED`, `SHOPY_SAFKA_ORDER_PUSH_ACTIVE`,
`SHOPY_SAFKA_ORDER_SCHEMA_VERIFIED`, `SHOPY_SAFKA_COMMISSION_POLICY_VERIFIED`,
`SHOPY_SAFKA_READ_ENABLED`. Existing `SHOPY_SAFKA_API_KEY` and `DATABASE_URL`
remain server secrets. Keep preview order flags disabled.

Additive DB changes (applied to the connected database):

```sql
ALTER TABLE public.safka_shipping_rates ADD COLUMN IF NOT EXISTS city_id TEXT;
ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS safka_city_id TEXT;
```

SQLite development adds the same columns through `init_safka_schema`.
For a fresh Supabase database, apply these after the base schema.

## Validation

Run `python -m unittest discover -s tests -q`.
Tests include pagination/all-in-one shipping, cache failure preservation,
property stock, cumulative quantity, exact generated payload/commission/city,
duplicate checkout, changed quote rejection and unknown supplier outcomes.
Transport is mocked in tests; no invented recipient or live supplier order is
created. A real provider acceptance can only be confirmed after a customer
submits genuine delivery details through the activated checkout.
