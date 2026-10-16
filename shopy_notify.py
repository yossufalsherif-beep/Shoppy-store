"""Safe WhatsApp Business Cloud API notifications for an existing SQLite shop.

This module does not run itself or send anything unless the store explicitly
queues events and an authorized operator runs the worker with valid settings.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping

TEMPLATE_NAME = "shopy_new_order_v1"


def validate_eg_mobile(value: str) -> str:
    """Return E.164 digits for Egyptian mobile numbers, else raise ValueError."""
    digits = re.sub(r"[\s()+\-]", "", value or "")
    if re.fullmatch(r"0(?:10|11|12|15)\d{8}", digits):
        digits = "2" + digits  # 010... -> 2010...
    if not re.fullmatch(r"20(?:10|11|12|15)\d{8}", digits):
        raise ValueError("رقم الموبايل المصري لازم يكون 11 رقم محليًا أو 12 رقم بعد حذف + دوليًا")
    return digits


def _one_line(value: Any, limit: int = 400) -> str:
    # WhatsApp template parameter values should not include newlines/tabs.
    clean = " ".join(str(value if value is not None else "-").split())
    return clean[:limit] or "-"


def _egp(piasters: Any) -> str:
    # Caller must pass integer piasters, not floats and not untrusted cart prices.
    if isinstance(piasters, bool) or not isinstance(piasters, int) or piasters < 0:
        raise ValueError("Use nonnegative integer piastres calculated by server")
    return f"{Decimal(piasters) / Decimal(100):,.2f}"


def build_template_parameters(order: Mapping[str, Any]) -> list[str]:
    """Build 9 parameters matching message_template_ar.txt, from persisted order snapshot."""
    items = order.get("items", [])
    if not items:
        raise ValueError("Order must contain persisted line items")
    item_parts = []
    for item in items:
        qty = item.get("qty", 1)
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            raise ValueError("Invalid item quantity")
        part = f"{qty}× {_one_line(item.get('name', 'منتج'))}"
        options = item.get("options")
        if options:
            if isinstance(options, dict):
                part += " (" + ", ".join(f"{_one_line(k)}: {_one_line(v)}" for k, v in options.items()) + ")"
            else:
                part += f" ({_one_line(options)})"
        item_parts.append(part)
    products_total = order["products_total_piasters"]
    shipping = order["shipping_piasters"]
    total = order["total_piasters"]
    if total != products_total + shipping:
        raise ValueError("Total must match persisted order amounts (discounts need explicit integration)")
    return [
        _one_line(order["order_ref"], 60),
        _one_line(order["customer_name"], 100),
        _one_line(order["customer_phone"], 30),
        _one_line(" ؛ ".join(item_parts), 600),
        _one_line(" - ".join(filter(None, [order.get("governorate"), order.get("city"), order.get("area")])), 180),
        _egp(products_total),
        _egp(shipping),
        _egp(total),
        _one_line(order.get("payment_method", "الدفع عند الاستلام"), 70),
    ]


def preview_message(order: Mapping[str, Any]) -> str:
    """For local preview only; final WhatsApp layout must be approved in Meta Manager."""
    p = build_template_parameters(order)
    return (
        "🛍️ طلب جديد | Shopy Store\n"
        f"رقم الطلب: {p[0]}\n"
        f"اسم العميل: {p[1]}\n"
        f"رقم العميل: {p[2]}\n"
        f"المنتجات: {p[3]}\n"
        f"المنطقة: {p[4]}\n"
        "────────────────\n"
        f"سعر المنتجات: {p[5]} ج.م\n"
        f"تكلفة الشحن: {p[6]} ج.م\n"
        f"الإجمالي: {p[7]} ج.م\n"
        f"طريقة الدفع: {p[8]}\n"
        "راجع التفاصيل الكاملة من لوحة الإدارة الخاصة بالمتجر."
    )


def configured_recipients(raw: str | None = None) -> list[str]:
    """Parse up to 5 distinct private destinations; do not hardcode them in source."""
    raw = os.getenv("SHOPY_WA_RECIPIENTS", "") if raw is None else raw
    if not raw.strip():
        return []
    pieces = [p.strip() for p in raw.split(",")]
    if len(pieces) > 5 or any(not p for p in pieces):
        raise ValueError("Use 1–5 comma-separated valid recipients")
    recipients = list(dict.fromkeys(validate_eg_mobile(p) for p in pieces))
    return recipients


def init_notification_store(conn: sqlite3.Connection) -> None:
    """New per-recipient outbox. No changes to existing order or item tables."""
    conn.execute("""
    CREATE TABLE IF NOT EXISTS notification_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT NOT NULL UNIQUE,
        order_ref TEXT NOT NULL,
        recipient TEXT NOT NULL,
        template_params_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('disabled','pending','sending','accepted','failed','unknown','delivered')),
        provider_message_id TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notification_outbox_status ON notification_outbox(status, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notification_outbox_order ON notification_outbox(order_ref)")


def queue_new_order(
    conn: sqlite3.Connection,
    order: Mapping[str, Any],
    *,
    recipients: list[str] | None = None,
    enabled: bool = False,
) -> int:
    """Queue ONE event per recipient within same transaction as order insert.

    Disabled mode records events as disabled; they do not auto-send later.
    No configured recipients -> zero events; dashboard still displays the order.
    """
    recipients = configured_recipients() if recipients is None else list(dict.fromkeys(validate_eg_mobile(p) for p in recipients))
    if not recipients:
        return 0
    params = build_template_parameters(order)
    order_ref = str(order["order_ref"])
    if not order_ref.strip():
        raise ValueError("Missing order reference")
    now = datetime.now(timezone.utc).isoformat()
    for recipient in recipients:
        conn.execute("""
          INSERT OR IGNORE INTO notification_outbox
            (event_key,order_ref,recipient,template_params_json,status,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?)
        """, (f"order-created:{order_ref}:{recipient}", order_ref, recipient,
              json.dumps(params, ensure_ascii=False),
              "pending" if enabled else "disabled", now, now))
    return len(recipients)


def make_payload(recipient: str, params: list[str], template_name: str, language: str) -> dict:
    if len(params) != 9:
        raise ValueError("Nine approved template variables are required")
    return {
        "messaging_product": "whatsapp",
        "to": validate_eg_mobile(recipient),
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": [{"type": "body", "parameters": [
                {"type": "text", "text": str(p)} for p in params
            ]}],
        },
    }


def send_one_pending(
    conn: sqlite3.Connection,
    *,
    authorized: bool = False,
    post: Callable[[str, bytes, dict], dict] | None = None,
) -> str:
    """Process ONE per-recipient event, preserving individual result status.

    Must be explicitly authorized + env-enabled. Ambiguous attempts are never
    auto retried, because Meta may have accepted the first request.
    """
    if not authorized or os.getenv("SHOPY_WA_ENABLED") != "1":
        return "disabled"
    token = os.environ.get("SHOPY_WA_ACCESS_TOKEN", "")
    phone_id = os.environ.get("SHOPY_WA_PHONE_NUMBER_ID", "")
    version = os.environ.get("SHOPY_WA_GRAPH_VERSION", "")
    template_name = os.environ.get("SHOPY_WA_TEMPLATE_NAME", TEMPLATE_NAME)
    language = os.environ.get("SHOPY_WA_TEMPLATE_LANG", "ar")
    if not (token and re.fullmatch(r"\d{7,25}", phone_id) and re.fullmatch(r"v\d+\.\d+", version)):
        raise ValueError("Missing official Meta credentials, phone-number ID, or Graph API version")
    row = conn.execute("SELECT id, recipient, template_params_json FROM notification_outbox WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
    if row is None:
        return "no_pending"
    id_, recipient, encoded = row
    payload = make_payload(recipient, json.loads(encoded), template_name, language)
    url = f"https://graph.facebook.com/{version}/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    # Atomic reservation prevents another worker from claiming this event.
    claimed = conn.execute("UPDATE notification_outbox SET status='sending',updated_at=? WHERE id=? AND status='pending'",
                           (datetime.now(timezone.utc).isoformat(), id_))
    conn.commit()
    if claimed.rowcount != 1:
        return "busy"
    if post is None:
        post = _post_json
    try:
        response = post(url, json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers)
        message_id = ((response.get("messages") or [{}])[0]).get("id")
        if not message_id:
            raise ValueError("Provider response missing message ID")
        conn.execute("UPDATE notification_outbox SET status='accepted',provider_message_id=?,updated_at=? WHERE id=?",
                     (str(message_id), datetime.now(timezone.utc).isoformat(), id_))
        conn.commit()
        return "accepted"
    except Exception as exc:
        # Not safe to blindly retry; status unknown until investigated.
        conn.execute("UPDATE notification_outbox SET status='unknown',error=?,updated_at=? WHERE id=?",
                     (_one_line(type(exc).__name__, 50), datetime.now(timezone.utc).isoformat(), id_))
        conn.commit()
        return "unknown"


def _post_json(url: str, data: bytes, headers: dict) -> dict:
    request = urllib.request.Request(url, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)
