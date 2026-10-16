> **ملاحظة V4.2:** قاعدة التقريب الحالية هي دائمًا لأعلى عشرة، لا لأقرب عشرة، وإرسال الطلبات مقفول برمجيًا. راجع `V4_2_RELEASE_NOTES_AR.md` أولاً.

# Shopy Store V4.1 — Safka catalog integration foundation

## Done
- Price formula: supplier price + 1/3, nearest multiple of EGP 10 using decimal rounding half up.
- Shipping: supplier rate + exactly EGP 10.
- Schema/additive migration: Safka IDs, stock, source flag, timestamps, read-only shipping rate cache.
- Safka catalog ingestion with price, category paths, images, variant validation, stock, stable product links.
- Transactional order staging, independent from WhatsApp notification status; outbox de-duplication.
- Admin indicators for order forwarding, supplier product stock and sync history.
- Safe importer accepting real verified JSON locally or authorized live API reads with explicitly configured official paths.
- Optional read-only 15-minute stock sync process, when local machine remains powered on.
- Offline unit tests for price, stock, atomic updates, no destructive imports and no duplicate order submission mocks.

## Partial / blocked
- Provider live payload shape and exact product/price list paths cannot be confirmed from publicly accessible docs; no live API credentials were provided. Live sync needs a verified representative response and marketer key.
- Order sender is intentionally NOT activated: missing verified Create Order JSON contract, upstream idempotency, marketer pricing/commission terms and end-to-end authorization.
- Automatic upstream order status updates need a documented provider status endpoint or webhook.
- Production hosting, database hardening, images storage and HTTP end-to-end testing remain outstanding.
