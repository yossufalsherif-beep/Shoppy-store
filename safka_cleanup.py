"""Safely replace a manually maintained local catalog with a verified Safka catalog.

This module NEVER touches Safka or sends orders. All irreversible local cleanup
requires an explicit authenticated action, an independently created database
backup, and successful REAL API synchronization of products and shipping.
"""
from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3

CONFIRM_TEXT = 'حذف البيانات اليدوية'


class CleanupNotReady(ValueError):
    pass


def summary(conn):
    """Return server-side evidence of a successful live read-only sync."""
    def count(sql):
        return int(conn.execute(sql).fetchone()[0])
    latest = {}
    for kind in ('products', 'shipping'):
        row = conn.execute('''SELECT updated,created_at,source_label FROM safka_sync_runs
                              WHERE kind=? ORDER BY id DESC LIMIT 1''', (kind,)).fetchone()
        latest[kind] = dict(row) if row is not None and row['source_label']=='api' and row['updated']>0 else None
    result = {
        'manual_products': count("SELECT COUNT(*) FROM products WHERE source='manual'"),
        'manual_shipping': count('SELECT COUNT(*) FROM shipping'),
        'manual_zones': count('SELECT COUNT(*) FROM shipping_zones'),
        'safka_products': count("SELECT COUNT(*) FROM products WHERE source='safka'"),
        'available_safka_products': count("SELECT COUNT(*) FROM products WHERE source='safka' AND active=1"),
        'safka_shipping': count('SELECT COUNT(*) FROM safka_shipping_rates'),
        'api_product_sync': latest['products'],
        'api_shipping_sync': latest['shipping'],
    }
    result['ready'] = bool(result['available_safka_products'] and result['safka_shipping']
                           and latest['products'] and latest['shipping'])
    return result


def cleanup(conn, db_path, backup_dir, confirmation):
    """Backup and remove manual catalog, never customers/orders/order item snapshots.

    Backups stay PRIVATE outside Flask's static web root. Local media files are
    deliberately kept for manual restoration instead of unlinking anything.
    """
    if confirmation != CONFIRM_TEXT:
        raise CleanupNotReady('جملة التأكيد غير صحيحة، لم يتم الحذف')
    state = summary(conn)
    if not state['ready']:
        raise CleanupNotReady('اربط صفقة واستورد المنتجات والشحن بنجاح من API الحقيقي أولًا؛ لم يتم حذف أي بيانات')
    db_path = Path(db_path).resolve(strict=True)
    backup_dir = Path(backup_dir).resolve()
    if not db_path.is_file() or db_path.stat().st_size == 0:
        raise CleanupNotReady('قاعدة بيانات غير صالحة للحفظ الاحتياطي')
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup_file = backup_dir / f'shopy-before-manual-cleanup-{timestamp}.sqlite3'
    try:
        with sqlite3.connect(backup_file) as target:
            conn.backup(target)
        # Verify integrity and presence of order history BEFORE changes.
        with sqlite3.connect(backup_file) as verify:
            if verify.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise CleanupNotReady('فشل اختبار سلامة النسخة الاحتياطية')
            verify.execute('SELECT COUNT(*) FROM orders').fetchone()
    except Exception:
        backup_file.unlink(missing_ok=True)
        raise
    try:
        conn.execute('BEGIN IMMEDIATE')
        if not summary(conn)['ready']:
            raise CleanupNotReady('انتهت صلاحية بيانات صفقة أثناء التحديث')
        # Preserve historical order snapshots, including prices, even for removed products.
        conn.execute("UPDATE order_items SET product_id=NULL WHERE product_id IN (SELECT id FROM products WHERE source='manual')")
        conn.execute("DELETE FROM products WHERE source='manual'")
        conn.execute('DELETE FROM shipping_zones')
        conn.execute('DELETE FROM shipping')
        conn.execute("INSERT INTO store_settings(key,value) VALUES('catalog_mode','safka_only') ON CONFLICT(key) DO UPDATE SET value='safka_only'")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {'removed_products': state['manual_products'],
            'removed_shipping': state['manual_shipping'],
            'removed_zones': state['manual_zones'],
            'backup_path': str(backup_file)}
