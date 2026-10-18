"""Documented order.status.updated events; supplier facts stay separate from local edits."""
import re
from datetime import datetime, timezone, timedelta

SUPPLIER_STATUS_LABELS={
 'pending':'بانتظار تأكيد المورد','preparing':'جارٍ التجهيز','printing':'جارٍ تجهيز مستندات الشحن',
 'shipped':'في الشحن','available':'تم التوصيل','collected':'تم التحصيل','skip':'جارٍ الاسترجاع',
 'holding':'مؤجل','ask_to_exchange':'طلب استبدال','returned_exchange':'مرتجع استبدال',
 'ask_to_return':'طلب استرجاع','returned1':'مرتجع','returned2':'مرتجع بعد التسليم',
 'declined1':'ملغي','declined2':'ملغي بعد التجهيز'}

def apply_order_status_event(conn,payload):
    if payload.get('event')!='order.status.updated':return False
    order=payload.get('order')
    if not isinstance(order,dict):return False
    ident=order.get('_id');status=order.get('status');stamp=order.get('updated_at')
    if not isinstance(ident,str) or not re.fullmatch(r'[a-fA-F0-9]{24}',ident):return False
    if not isinstance(status,str) or status not in SUPPLIER_STATUS_LABELS or not isinstance(stamp,str) or len(stamp)>64:return False
    try:
        updated=datetime.fromisoformat(stamp.replace('Z','+00:00'))
        if updated.tzinfo is None:return False
        updated=updated.astimezone(timezone.utc)
        if updated>datetime.now(timezone.utc)+timedelta(minutes=10):return False
    except ValueError:return False
    canonical_stamp=updated.isoformat(timespec='microseconds')
    rows=conn.execute('SELECT order_id FROM safka_order_outbox WHERE supplier_order_id=?',(ident,)).fetchall()
    if len(rows)!=1:return False
    result=conn.execute('''UPDATE safka_order_outbox SET supplier_status=?,supplier_status_updated_at=?,
        updated_at=CURRENT_TIMESTAMP WHERE order_id=? AND
        (supplier_status_updated_at IS NULL OR supplier_status_updated_at<?)''',
        (status,canonical_stamp,rows[0]['order_id'],canonical_stamp))
    return result.rowcount==1
