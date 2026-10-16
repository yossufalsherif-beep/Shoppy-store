# V4 feature status

| Feature | Status | Comment |
|---|---|---|
| Public home, optional login | Implemented | Based on V3.1 |
| Smart checkout / live shipping quote | Implemented, unverified in Flask runtime | Regional quote server-side fallback, persistent order snapshots |
| Image/video product gallery, lightbox | Implemented, browser unverified | Existing uploads from V3; extra image zoom and category-based related products |
| Compare three products | Implemented, unverified in Flask runtime | Session-backed, customers do not need account |
| Guided shopping assistant | Implemented | Rule mode default, optional local Ollama if installed and configured |
| UTM campaign and order analytics | Implemented, unverified in Flask runtime | Only records order metrics, not validated revenue |
| Supplier cost / estimated margins | Implemented | Only on orders with costs recorded, before ads and other expenses |
| Admin integration center | Implemented (UI) | Actual payment/Safka connection disabled |
| Public support phone and email | Implemented | Admin-configured only; separate from private WhatsApp recipients |
| Guest order lookup | Omitted by request | No public order number lookup |
| API payment | Not connected | Credentials, approved account, and webhook verification required |
| Safka API sync | Not connected | Marketer account authorization and credentials required |
| Actual WhatsApp send | Disabled | Prior outbox integration remains, no real sends without explicit approval |
| Production security + customer deployment | Not ready | Requires security review and hosting/persistent storage |
