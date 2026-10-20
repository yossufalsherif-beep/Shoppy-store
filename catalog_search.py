"""Arabic/English catalog search with conservative, explainable relevance ranking.

Runs only for explicit searches. Strong product title/category matches take
priority; description-only matches are used when no better match exists.
No paid embeddings, remote search service, or invented products.
"""
from difflib import SequenceMatcher
import re
import unicodedata
from ui_queries import positive_page

STOPWORDS = {'عايز','عاوزه','عاوز','عايزه','اريد','محتاج','محتاجه','منتج','منتجات',
             'عندي','عندك','من','في','على','الي','الى','عن','اللي','هذا','هذه',
             'او','و','مع','ل','ابحث','بحث','دور','هات','افضل','احسن','لل'}
GROUPS = (
 ('سشوار','سيشوار','استشوار','مجفف'),
 ('مكنسه','مساحه','vacuum'),
 ('كوتشي','حذاء','جزمه'),
 ('سماعه','هيدفون','هيدسيت','headphone','headset'),
 ('شاحن','شارجر','charger'),
 ('جوال','موبايل','هاتف','تليفون','phone'),
 ('فرشاه','مشط','brush'),
 ('بلوتوث','bluetooth'),
 ('شنطه','حقيبه','bag'),
)

def normalize(text):
    text = unicodedata.normalize('NFKC', str(text or '')).casefold()
    text = re.sub(r'[\u064b-\u065f\u0670\u0640]', '', text)
    text = re.sub('[إأآٱ]', 'ا', text).replace('ى','ي').replace('ة','ه').replace('ؤ','و').replace('ئ','ي')
    text = re.sub(r'[^\w\u0600-\u06ff]+', ' ', text).strip()
    # Egyptian colloquial ضهر and standard Arabic ظهر mean the same body part.
    # Replace only whole words (plus common attached particles/pronouns), not
    # every ض or ظ: a blanket character swap would introduce false results.
    return re.sub(r'(?<!\w)(بال|وال|لل|ال|ب|ل|و)?ضهر(ها|هم|كم|نا|ي|ك|ه)?(?!\w)',
                  lambda match:(match.group(1) or '')+'ظهر'+(match.group(2) or ''),text)

def words(text,ignore_stopwords=True):
    output=[]
    for word in normalize(text).split():
        if word in ('بالظهر','للظهر','والظهر','بظهر','لظهر','وظهر'):
            word='ظهر'
        elif word.startswith('ال') and len(word)>=5:
            word=word[2:]
        if len(word)>=2 and (not ignore_stopwords or word not in STOPWORDS):
            output.append(word)
    return output[:20]

SYNONYMS={}
for group in GROUPS:
    variants={word for item in group for word in words(item)}
    for term in variants:
        SYNONYMS[term] = variants

def _match(word, candidates, allow_fuzzy=True):
    if word in candidates:
        return 1.0
    if any(len(word)>=3 and (x.startswith(word) or word.startswith(x) and len(x)>=4) for x in candidates):
        return .83
    if allow_fuzzy and len(word)>=4:
        best=max((SequenceMatcher(None,word,x).ratio() for x in candidates if abs(len(word)-len(x))<=2),default=0)
        if best>=.84:return .58
    return 0

def _field_match(term, tokens):
    exact=_match(term,tokens)
    if exact:return exact
    return max((_match(alt,tokens,False)*.60 for alt in SYNONYMS.get(term,()) if alt!=term),default=0)

def relevance(row, query_tokens, original_phrase):
    title=words(row['title'],False); category=words(row['category'],False); desc=words(row['description'],False)
    if not title:return None
    score=0; strong_matches=0; title_matches=0
    for term in query_tokens:
        t=_field_match(term,title)
        c=_field_match(term,category)
        d=_field_match(term,desc)
        if not max(t,c,d):
            return None
        if t:
            title_matches+=1
            strong_matches+=1
            score+=int(30*t)
        elif c:
            strong_matches+=1
            score+=int(13*c)
        else:
            score+=int(2*d)
    clean_title=normalize(row['title'])
    clean_query=normalize(original_phrase)
    if clean_query and clean_query in clean_title:
        score+=55
    if title_matches==len(query_tokens):
        score+=25
    return (strong_matches>0,score,int(row['id']))

def search_ranked(products,query,sort='relevance'):
    terms=list(dict.fromkeys(words(query) or words(query,False)))[:10]
    if not terms:return []
    strong=[];weak=[]
    for row in products:
        match=relevance(row,terms,query)
        if match is None:continue
        (strong if match[0] else weak).append((match,row))
    # Don't bury useful title results under generic description matches.
    pool=strong if strong else weak
    if sort=='price_low':
        pool.sort(key=lambda entry:(entry[1]['price'],-entry[0][1],-entry[0][2]))
    elif sort=='price_high':
        pool.sort(key=lambda entry:(-entry[1]['price'],-entry[0][1],-entry[0][2]))
    elif sort=='newest':
        pool.sort(key=lambda entry:-entry[0][2])
    else:
        pool.sort(key=lambda entry:(-entry[0][1],-entry[0][2]))
    return [row for _,row in pool]

def page_ranked(items,requested,size=30,anchor='catalog'):
    total=len(items);pages=max(1,(total+size-1)//size)
    page=min(positive_page(requested),pages)
    numbers=sorted({1,pages,*range(max(1,page-2),min(pages,page+2)+1)})
    links=[]
    for number in numbers:
        if links and number>links[-1]+1:links.append(0)
        links.append(number)
    pager={'total':total,'pages':pages,'page':page,'size':size,'param':'page',
           'anchor':anchor,'numbers':links,'start':(page-1)*size+1 if total else 0,
           'end':min(page*size,total)}
    return items[(page-1)*size:page*size],pager
