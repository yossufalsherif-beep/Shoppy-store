"""Customer-safe text, transparent availability, verified ratings and store categories.

Never invent supplier ratings or translate "active" into a confirmed stock count.
"""
import html
import re
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

class _DescriptionText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]
        self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','svg','iframe','object','template'):
            self.skip+=1
        elif not self.skip and tag in ('p','div','br','li','h1','h2','h3','h4','tr'):
            self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style','svg','iframe','object','template') and self.skip:
            self.skip-=1
        elif not self.skip and tag in ('p','div','li','h1','h2','h3','h4','tr'):
            self.parts.append('\n')
    def handle_data(self,data):
        if not self.skip:
            self.parts.append(data)

def clean_description(value):
    """Remove HTML/CSS/JS rather than escape markup into the visible product text."""
    if not isinstance(value,str):
        return ''
    source=html.unescape(value[:30000])
    source=re.sub(r'```[\s\S]*?```',' ',source)
    parser=_DescriptionText()
    try:parser.feed(source);text=''.join(parser.parts)
    except ValueError:text=re.sub(r'<[^>]*>',' ',source)
    text=re.sub(r'(?im)^\s*(?:const|let|var|function)\s+[^\n]{10,}$','',text)
    text=re.sub(r'(?im)^\s*[.#]?[a-z][a-z0-9 _.,#:\[\]"\'>=-]{0,80}\s*\{[^\n]*$','',text)
    text=re.sub(r'[\t\r\f\v ]+',' ',text)
    text=re.sub(r' *\n *','\n',text)
    text=re.sub(r'\n{3,}','\n\n',text).strip()
    return text[:5000]

# These are Shopy Store browsing groups, not claims about Safka's supplied taxonomy.
CATEGORY_RULES=(
 ('إلكترونيات وإكسسوارات',('سماعة','سماعات','بلوتوث','شاحن','كابل','موبايل','هاتف','usb','speaker','headphone','earphone','كاميرا','ساعة ذكية','باور بانك')),
 ('الجمال والعناية',('شعر','مكواة','سيشوار','استشوار','بشرة','ماكينة حلاقة','حلاقة','مساج الوجه','عناية','beauty','makeup','hair')),
 ('رياضة ولياقة',('رياضة','رياضي','الركبة','الظهر','تمرين','تمارين','جيم','لياقة','gym','fitness','support')),
 ('المنزل والمطبخ',('مطبخ','مروحة','تنظيف','مكنسة','الخضار','منزل','ثلاجة','خلاط','كبة','أطباق','دفاية','المنزلي','ممسحة')),
 ('ملابس وإكسسوارات',('حذاء','شنطة','حقيبة','ملابس','فستان','تيشيرت','جاكيت','محفظة','نظارة','bag','shirt','dress')),
 ('ألعاب وأطفال',('طفل','أطفال','لعبة','ألعاب','تعليمي','toy','kids')),
)
def suggested_category(title):
    value=(title or '').casefold()
    for group,words in CATEGORY_RULES:
        if any(word in value for word in words):
            return group
    return 'منتجات متنوعة'

def availability_from_source(raw,stock):
    """True/False only for a verified boolean supplier listing flag or numeric stock."""
    if not isinstance(raw,dict):
        return None
    status=raw.get('is_active')
    if status is False:
        return False
    if status is True:
        # This is a LISTING availability indicator, not proof of positive quantity.
        return True
    return bool(stock) if stock is not None else None

def extract_rating(raw):
    """Only accept an explicit numerical aggregate with positive review count."""
    if not isinstance(raw,dict):
        return None,None
    value=raw.get('rating',raw.get('average_rating'))
    count=raw.get('review_count',raw.get('ratings_count'))
    if isinstance(value,dict):
        value=value.get('average')
    if isinstance(value,bool) or isinstance(count,bool):
        return None,None
    try:
        r=Decimal(str(value))
        n=int(count)
        if r.is_finite() and 0<=r<=5 and n>0 and str(count).strip()==str(n):
            return float(r),n
    except (TypeError,ValueError,InvalidOperation):
        pass
    return None,None
