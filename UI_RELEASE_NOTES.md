# Shoppee UI and bounded lists

- Storefront, category results and favorites: server-side pages of 30 products.
- Admin products: 30 per page, independent product search/category controls.
- Admin orders: 20 per page; oldest first by default. Search by reference,
  customer name or phone, plus local/supplier status, governorate, dates and
  order total. Newest/lowest/highest value ordering is available. Page links
  retain the current filters and the other admin list's page.
- Existing SH- order references stay immutable and can be copied from the table.
- Customer accounts show 20 orders per page. Overall admin metrics include all
  records, independent of filters/pages.
- Related images/items/notifications are loaded in batches for the current page.
  User/settings lookups are cached only for the current request, avoiding
  cross-user cache leaks. CSS/JS URLs have deployment content fingerprints.
- Product cards add to cart and toggle favorites with progressive AJAX forms.
  Variant choices remain mandatory; no size/color is guessed. Buttons report
  success through an accessible toast with a cart/favorites link.
- Guest favorites work in the current browser and merge into the signed-in
  account on login; customer reviews still require login.
- Sticky WhatsApp support links: 01004351876 and 01000163572. These links open
  a user-controlled chat; the application sends no support message itself.
- Visible store name is Shoppee (double p).

## Supplier order status

Safka documents `order.status.updated` events with an `order` object containing
`_id`, `status` and `updated_at`. The authenticated orderHook accepts only known
provider order IDs and recognized statuses with timezone-aware timestamps.
Duplicates, older updates, unknown IDs and unrecognized statuses are ignored.
Supplier facts are stored separately from manual store status and are shown in
customer/admin views. Customer/phone/total fields in events are not trusted.

In Safka's integration settings, configure **orderHook** to the current secret
URL in **Admin → الربط والدعم → روابط استقبال صفقة**. Do not paste that URL in a
public message. Until Safka delivers events to that URL, the UI shows the saved
local status and supplier-submission state. The documentation does not promise
an independent 'customer called/not called' state; the store does not invent it.

Columns `supplier_status` and `supplier_status_updated_at` and paging/relationship
indexes were added through the connected Supabase migration. RLS stays enabled;
no new public Data API policies were added. SQLite development initializes the
same fields and indexes.

## Verification

147 offline tests pass, covering 30/20 page bounds, category pages, combined
filters, stable sorting, guest favorites, AJAX cart and supplier-event ordering.
A query-budget test verifies that admin query count stays bounded as the catalog
grows. Synthetic orders and customer details are used only in local tests.
