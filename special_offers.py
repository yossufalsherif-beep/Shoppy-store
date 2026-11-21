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

To end an offer, delete its entry. Keys are product slugs (the /products/<slug>
URL). Products are matched by slug, so a renamed slug ends the offer.
"""

SPECIAL_OFFERS = {
    # https://shoppy-store-one.vercel.app/products/safka-79797df9bf355ce5d686
    'safka-79797df9bf355ce5d686': {
        'label': 'فرشاة الأسنان الاوتوماتيكية',
        'price': 49900,        # EGP 499 sale price
        'compare_at': 99900,   # EGP 999 original price, shown struck through
        'free_shipping': True,
    },
}


def _field(product, name):
    try:
        keys = product.keys()
    except AttributeError:
        return None
    return product[name] if name in keys else None


def offer_for_slug(slug):
    return SPECIAL_OFFERS.get(slug) if isinstance(slug, str) else None


def product_offer(product):
    """The fixed offer for a product row/dict, or None."""
    return offer_for_slug(_field(product, 'slug'))
