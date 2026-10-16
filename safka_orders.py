"""Safeguard for Shopy Store V4.2: supplier WRITE operations are disabled.

V4.2 is a READ + LOCAL DRY-RUN milestone. It deliberately has no Safka
order-create network transport. Even if a caller passes enabled=True or a
provider callback, this function NEVER invokes it. Real submits are a
separately reviewed, owner-authorized V4.3 feature.
"""


def submit_order_once(conn, order_id, provider_submit=None, *, enabled=False):
    """Legacy interface; write operations hard-blocked in V4.2."""
    return {'status': 'blocked_v42', 'sent': False,
            'reason': 'Safka order submission is disabled in V4.2'}
