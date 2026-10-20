# Shoppy Store — 20 October 2026 staging changes

This is a **review branch**, not a production release. The bundle offer remains OFF
by default. Never enable it without validating real Safka shipping, margins,
data migration and a live manual checkout preview. Existing orders keep their prices.

## Bundle offer (owner-requested arithmetic)

- Every product's displayed unit price = existing recorded selling price + EGP 95.
  The recorded price remains unchanged, including after a Safka refresh.
- Cart with 2+ total pieces (different products or repeated quantities):
  visible full-price line items minus EGP 95 for EACH piece after the first,
  and delivery fee = EGP 0. The promotion appears as separate discount line.
- Cart with 1 piece: no piece discount; shipping follows the verified Safka
  governorate/city fee minus the **owner-configured shipping reduction**. Shipping
  must remain positive, and the discount cannot exceed the collected fee.
- Example using supplier delivery EGP 85, current customer shipping EGP 95,
  and OPTIONAL reduction EGP 45 (not activated):
  one piece shows EGP 50 shipping; 2+ pieces show free delivery.
  The owner gave EGP 40 only as an illustration, not a final approved tariff.
- Updated bundle margin policy (new offer version 2): keep the **same +95 EGP**
  display premium regardless of destination; no blanket product price increases.
  New bundle orders may earn less than the former 15% / EGP 30 guardrail,
  but must retain at least **EGP 1 positive contribution** after supplier
  product cost and actual Safka shipping. That amount excludes ad spend,
  returns, payment costs, and other overhead, so it is not net profit.
- Existing version 1 bundle order snapshots retain their original 15%/EGP 30
  validation. Orders that fall below EGP 1 contribution are blocked, not
  silently repriced or charged extra shipping. Verify supplier tariffs first.
- Old checkout signatures, orders and saved order item snapshots are preserved.
  New orders explicitly snapshot the piece discount, version and unit count.

## Activation sequence

1. Test the branch with the new offline suite.
2. Apply additive schema changes via a reviewed migration/normal startup:
   orders.bundle_discount, orders.bundle_offer_version, orders.bundle_unit_count.
3. Review all currently active products and Safka fees so combinations cannot
   become unprofitable or unavailable.
4. Agree a single-item subsidy figure. Set
   `SHOPY_SINGLE_ITEM_SHIPPING_REDUCTION_EGP` on Vercel (EGP integer),
   leaving its default 0 until owner approval.
5. Only then set `SHOPY_BUNDLE_OFFER_ENABLED=1` on Production, deploy,
   and test one, two, three pieces, multiple variants and locations.
6. Verify no unwanted previous `margin_shipping_discount_enabled` conflicts:
   the new bundle offer takes precedence only when enabled.

## Login

Google-only buttons; Apple is hidden and disabled (existing user identities are
not deleted). Google PKCE uses the already implemented Supabase authorization
bridge. The OAuth Google Client ID and Client Secret must be added to the
**Supabase provider settings**, never in source. The callback and allowed
redirect URL instructions are visible at `/admin/social-login`. Verify a
real Google customer login and returning account link before enabling
`SHOPY_AUTH_GOOGLE_ENABLED=1` in Vercel. Facebook Login has not been
implemented: it is generally possible to set up without a per-login charge,
but requires its own Meta app configuration and public-access review.

## WhatsApp support and owner order alerts

The storefront now has one WhatsApp support link pointing to the owner-provided
Business number. The digits are not printed on the page, but WhatsApp and the
underlying URL necessarily reveal the number to the person opening the chat;
the link is NOT phone-number anonymity.

Order notification templates and the private notification queue already exist
in `shopy_notify.py`. This branch does NOT claim that automatic sends are live.
Set up the official Meta WhatsApp Business Cloud API, an approved template with
exactly nine variables, business sender Phone Number ID, private API access
token, authorized notification recipients, and an authenticated sender
invocation. Sending proactive templates may incur per-message charges.
The business number used as the **sender cannot receive its own outgoing
notifications**: give another WhatsApp-enabled recipient number, or use a
separate sending number. Never expose API tokens in chats or GitHub.
