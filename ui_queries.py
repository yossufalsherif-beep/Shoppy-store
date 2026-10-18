"""Bounded SQL pages and validated filters shared by the storefront and admin."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from safka_status import SUPPLIER_STATUS_LABELS

ORDER_LABELS={'new':'تم استلام الطلب','confirmed':'تم تأكيد الطلب','preparing':'جارٍ التجهيز',
              'shipped':'تم الشحن','delivered':'تم التسليم','cancelled':'ملغي'}

def positive_page(value):
    try:return min(1000000,max(1,int(value)))
    except (ValueError,TypeError):return 1

def price_filter(value):
    if value in ('',None):return None
    try:
        amount=Decimal(value)
        if not amount.is_finite() or amount<0 or amount>1000000000 or amount.as_tuple().exponent < -2:
            raise ValueError()
        return int(amount*100)
    except (InvalidOperation,ValueError,TypeError):
        raise ValueError('اكتب سعرًا صحيحًا موجبًا، بحد أقصى منزلتين عشريتين.') from None

def paginate(conn,select_sql,count_sql,params,order_sql,requested,size,param='page',anchor=''):
    total=conn.execute(count_sql,params).fetchone()[0]
    pages=max(1,(total+size-1)//size)
    page=min(positive_page(requested),pages)
    rows=conn.execute(select_sql+' ORDER BY '+order_sql+' LIMIT ? OFFSET ?',
                      [*params,size,(page-1)*size]).fetchall()
    numbers=sorted({1,pages,*range(max(1,page-2),min(pages,page+2)+1)})
    links=[]
    for n in numbers:
        if links and n>links[-1]+1:links.append(0)
        links.append(n)
    return rows,{'total':total,'pages':pages,'page':page,'size':size,'param':param,
                 'anchor':anchor,'numbers':links,'start':(page-1)*size+1 if total else 0,
                 'end':min(page*size,total)}

def order_filters(args):
    terms=[];params=[]
    query=args.get('order_q','').strip()[:100]
    if query:
        columns={'reference':['o.reference'],'name':['o.customer'],'phone':['o.phone']}.get(
            args.get('order_field'),['o.reference','o.customer','o.phone','o.address'])
        terms.append('('+' OR '.join('LOWER('+column+') LIKE LOWER(?)' for column in columns)+')')
        params.extend(['%'+query+'%']*len(columns))
    status=args.get('order_status','')
    if status in ORDER_LABELS:terms.append('o.status=?');params.append(status)
    supplier=args.get('order_supplier_status','')
    if supplier in SUPPLIER_STATUS_LABELS:
        terms.append('EXISTS(SELECT 1 FROM safka_order_outbox so WHERE so.order_id=o.id AND so.supplier_status=?)')
        params.append(supplier)
    gov=args.get('order_governorate','').strip()[:80]
    if gov:terms.append('o.governorate=?');params.append(gov)
    for key,operator in [('order_from','>='),('order_to','<')]:
        raw=args.get(key,'')
        if raw:
            try:
                value=date.fromisoformat(raw)
                if key=='order_to':value+=timedelta(days=1)
            except (ValueError,OverflowError):raise ValueError('راجع تاريخ بداية ونهاية البحث.') from None
            terms.append('o.created_at'+operator+'?');params.append(value.isoformat())
    low=price_filter(args.get('order_min',''));high=price_filter(args.get('order_max',''))
    if low is not None and high is not None and low>high:raise ValueError('الحد الأدنى للسعر أكبر من الحد الأقصى.')
    for value,operator in [(low,'>='),(high,'<=')]:
        if value is not None:terms.append('o.total'+operator+'?');params.append(value)
    sort={'oldest':'o.id ASC','newest':'o.id DESC','amount_low':'o.total ASC,o.id ASC',
          'amount_high':'o.total DESC,o.id ASC'}.get(args.get('order_sort'),'o.id ASC')
    return (' WHERE '+' AND '.join(terms) if terms else ''),params,sort

def grouped(conn,table,key,values,fields='*',order=''):
    """One query per relationship, regardless of the number of rows on a page."""
    result={value:[] for value in values}
    if not values:return result
    sql='SELECT '+fields+' FROM '+table+' WHERE '+key+' IN ('+','.join('?' for _ in values)+')'
    if order:sql+=' ORDER BY '+order
    for row in conn.execute(sql,values).fetchall():result[row[key]].append(row)
    return result
