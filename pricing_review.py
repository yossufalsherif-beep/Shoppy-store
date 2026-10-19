"""Build a reviewable pricing plan from dated research, never guessed market prices.

This module does not fetch data, mutate the DB, or submit orders on import.
Raw competitor snippets are not published in the repository.
"""
import json
from datetime import datetime, timezone
from profit_pricing import minimum_sale_price


def build_plan(catalog,research,verified,checked_at=None):
    dated=checked_at or datetime.now(timezone.utc).isoformat()
    observed={p['id']:p for p in verified}
    sources={p['id']:p.get('sources',[]) for p in research}
    plans=[]
    for p in catalog:
        cost=p['supplier_cost'];current=p['price'];market=observed.get(p['id'])
        if not isinstance(cost,int) or cost<=0:continue
        floor=minimum_sale_price(cost)
        target=None;status='needs_review';basis='cost';reference=None;url=''
        if current<floor:
            notes='السعر الحالي قريب من التكلفة ولا يسمح بحد الربح للقطعة؛ لم يُرفع السعر تلقائيًا. لا يُمنح خصم شحن إلا إذا سمح ربح الطلب كاملًا.'
        elif market:
            reference=int(round(market['competitor']*100));url=market['url']
            # Strictly below a matched competitor when the margin permits it.
            target=min(current,max(floor,((reference-1)//500)*500))
            status='verified';basis='market';notes=market['notes']
            if floor>reference:notes+=' حد الربح أعلى من عرض المنافس، لذلك لم نكسر حد الأمان.'
        else:
            # A conservative cost-based reduction, explicitly NOT a market quote.
            target=min(current,((current+floor+999)//1000)*500)
            status='cost_based'
            notes='لم يثبت سعر منافس مطابق متاح. تخفيض محسوب إلى منتصف المسافة بين السعر السابق وحد الربح، مقربًا لأعلى إلى 5 جنيه. هذا تسعير مبني على التكلفة وليس مقارنة سوق مؤكدة.'
        plans.append({'product_id':p['id'],'supplier_product_id':p['supplier_product_id'],
            'previous_price':current,'target_price':target,'verified_cost':cost,'competitor_price':reference,
            'source_url':url,'checked_at':dated,'status':status,'basis':basis,'notes':notes,
            'evidence_json':json.dumps([{'title':s['title'],'url':s['url']} for s in sources.get(p['id'],[])[:5]],ensure_ascii=False)})
    return plans
