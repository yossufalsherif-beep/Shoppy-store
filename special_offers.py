"""Fixed per-product offers that live in code, not in the database.

The owner sets these offers here (no Supabase migration and no admin field
needed). An offer product:
  * sells at ``price`` (piastres) whatever the synced supplier price is, so a
    Safka sync can never undo the offer;
  * shows ``compare_at`` struck through with a "خصم N%" badge;
  * ships free: an order that contains it pays no delivery (merchant-funded,
    still guarded by the minimum-contribution check in profit_pricing);
  * stays OUTSIDE the "EGP 95 per extra piece" bundle offer: no +95 premium on
    its price and it is not counted as a bundle piece.

Keys are product slugs (the /products/<slug> URL).

IMPORTANT, to keep already-placed orders reconcilable with Safka:
  * To END an offer, set ``'active': False``. Never delete the entry.
  * To CHANGE the price, move the old price into ``'past_prices'``.
Orders store only the line price, so old orders are recognised as offer orders
by (slug, price they were sold at) against this table.
"""

SPECIAL_OFFERS = {
    # https://shoppy-store-one.vercel.app/products/safka-79797df9bf355ce5d686
    'safka-79797df9bf355ce5d686': {
        'label': 'فرشاة الأسنان الاوتوماتيكية',
        'price': 49900,        # EGP 499 sale price
        'compare_at': 99900,   # EGP 999 original price, shown struck through
        'free_shipping': True,
        'active': True,
        'past_prices': [],     # earlier offer prices, kept for placed orders
    },
}


def _field(product, name):
    try:
        keys = product.keys()
    except AttributeError:
        return None
    return product[name] if name in keys else None


def offer_for_slug(slug):
    """The ACTIVE offer for a slug (used for prices shown and charged now)."""
    offer = SPECIAL_OFFERS.get(slug) if isinstance(slug, str) else None
    return offer if offer and offer.get('active', True) else None


def offer_sold_at(slug, unit_price):
    """The offer an existing order line was sold under, active or ended."""
    offer = SPECIAL_OFFERS.get(slug) if isinstance(slug, str) else None
    if offer and unit_price in [offer['price'], *offer.get('past_prices', [])]:
        return offer
    return None


def product_offer(product):
    """The active fixed offer for a product row/dict, or None."""
    return offer_for_slug(_field(product, 'slug'))
