# Social login and pricing release

## Authentication

The existing Flask password accounts and sessions remain authoritative. Google
and Apple use Supabase OAuth with PKCE; the callback exchanges the code using
a browser-bound verifier, calls the verified `/user` endpoint, and maps the
immutable provider subject into a local customer account. Tokens are discarded
and never put in HTML or the Flask cookie. Email matching never silently links
an existing password account or grants admin access. Explicit admin linking also
requires the administrator's password.

Provider setup and a real-account round trip are **not completed by deploying
this code**. Both flags default to off. See `/admin/social-login` for the exact
Google client, Supabase callback and redirect allow-list instructions, plus the
Apple Services ID/App ID/private-key requirements. Provider secrets stay in the
Supabase provider dashboard. The application uses a public publishable key.

Required public configuration: `SHOPY_AUTH_SUPABASE_URL`,
`SHOPY_AUTH_PUBLISHABLE_KEY`, `SHOPY_PUBLIC_URL`. Set
`SHOPY_AUTH_GOOGLE_ENABLED=1` or `SHOPY_AUTH_APPLE_ENABLED=1` only after configuring
the provider and testing it. The start route additionally checks the actual
provider setting and fails safely when it is unavailable.

## Pricing

The user selected a retained contribution of **max(EGP 30, 15% of merchandise
revenue)** after supplier goods and delivery cost, before advertising, returns,
COD losses, tax, and other expenses. This is not a guarantee of final net profit.

A matched competitor offer is identified by the actual product and pack/model,
not its lowest search-snippet number. Promotions, crossed-out prices, supplier
commissions embedded in descriptions and out-of-stock prices are not accepted
as current comparable offers.

Where no reliable match was confirmed, the plan explicitly uses `cost_based`:
the midpoint between the previous price and the margin-safe minimum, rounded
up to EGP 5, without raising any existing price. It does not label this fallback
as a verified market price. Existing low-margin items are left unchanged and
flagged. The audit remains private in `market_pricing`; raw search extracts and
merchant cost snapshots are not committed to the repository.

`pricing_review.build_plan` builds a reviewable dated plan without DB writes.
Only rows whose supplier ID, cost and previous price still match the snapshot
are applied. `/admin/pricing` displays the method, source links and review notes.
Approved rules persist across supplier synchronization. An increased supplier
cost re-applies the margin floor and flags the item for review. Explicit manual
edits disable the rule until the owner approves another market price.

## Delivery offers

Base Safka tariffs and their pricing document/city IDs remain unchanged. For a
fully costed supplier cart, the server spends only the surplus above the chosen
retained contribution on delivery, never below zero. The customer sees the
base fee and funded discount before placing the order. A signed quote binds
address, cart, subtotal and discounted fee. The checkout recomputes them from
fresh DB rows and saves base fee, discount, final fee and retained floor.
The dry run validates those snapshots; the supplier commission remains exactly
customer COD total minus supplier goods and supplier delivery. Old orders with
no discount preserve the original +EGP 10 rule and are never repriced.

Toggle: `/admin/pricing` writes `margin_shipping_discount_enabled` in private
store settings. Unknown costs and insufficient contribution fund no discount.

## Verification

Offline tests cover PKCE browser/state binding, replay/expiry, verified email,
account collision, explicit linking, ignored user-supplied roles, quote changes,
merchant-funded delivery down to zero, supplier payload/commission totals,
tampered snapshots, market-price persistence and cost-floor recalculation.
Real provider sign-in still requires owner configuration and a real-account
round trip; tests do not create or send actual supplier orders.
