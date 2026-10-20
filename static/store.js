(() => {
 const status=document.getElementById('action-feedback');
 let timer;
 function feedback(message,kind,viewUrl){
  if(!status)return;
  clearTimeout(timer);status.replaceChildren();status.className='action-feedback is-visible '+(kind==='error'?'is-error':'');
  const text=document.createElement('span');text.textContent=message;status.append(text);
  if(viewUrl){const link=document.createElement('a');link.href=viewUrl;link.textContent=viewUrl==='/favorites'?'عرض المفضلة ←':'عرض السلة ←';status.append(link);}
  timer=setTimeout(()=>status.classList.remove('is-visible'),6500);
 }
 document.addEventListener('submit',async event=>{
  const form=event.target.closest('form[data-async]');if(!form)return;
  if(form.dataset.asyncFallback==='1')return;
  event.preventDefault();if(form.dataset.busy==='1')return;
  const button=event.submitter||form.querySelector('button[type="submit"]');
  form.dataset.busy='1';if(button)button.disabled=true;
  try{
   const body=new FormData(form);
   // Submitter's + / - value is not included in FormData(form) by all browsers.
   if(form.dataset.async==='quantity'&&button?.name)body.set(button.name,button.value);
   const response=await fetch(form.action,{method:'POST',body,credentials:'same-origin',headers:{Accept:'application/json'}});
   if(!response.headers.get('content-type')?.includes('application/json')){
    // A proxy/error page must never be mistaken for a successful cart update.
    // Allow the normal HTML form route on the next click as a safe fallback.
    form.dataset.asyncFallback='1';
    throw new Error(response.status===400?'انتهت صلاحية الجلسة أو رمز الأمان. افتح السلة من جديد وحاول مرة أخرى.':`تعذر تعديل الكمية (HTTP ${response.status}). اضغط الزر مرة ثانية لاستخدام الطريقة البديلة.`);
   }
   const data=await response.json();if(!response.ok||!data.ok){
    if(response.status>=500){form.dataset.asyncFallback='1';}
    throw new Error(data.message||'حاول مرة أخرى.');
   }
   document.querySelectorAll('.cart-badge').forEach(b=>b.textContent=data.cart_count);
   if(form.dataset.async==='quantity'){
    const updated=new DOMParser().parseFromString(data.cart_html||'', 'text/html').getElementById('cart-body');
    const current=document.getElementById('cart-body');
    if(!updated||!current)throw new Error('تعذر تحديث السلة. حاول مرة أخرى.');
    current.replaceWith(document.importNode(updated,true));
    return;
   }
   if(form.dataset.async==='favorite'){
    document.querySelectorAll('form[data-async="favorite"]').forEach(other=>{
     if(other.action!==form.action)return;
     const heart=other.querySelector('button');heart.classList.toggle('is-favorite',data.favorite);heart.setAttribute('aria-pressed',String(data.favorite));
     const symbol=heart.querySelector('[data-heart-symbol]');if(symbol)symbol.textContent=data.favorite?'♥':'♡';
    });
    if(!data.favorite&&location.pathname==='/favorites'){
     form.closest('.product-card')?.remove();document.querySelectorAll('.result-count').forEach(x=>x.textContent='تم تحديث المفضلة.');
     const grid=document.querySelector('.product-grid');if(grid&&!grid.children.length){const p=document.createElement('p');p.className='empty';p.textContent='مفيش منتجات تانية في الصفحة دي. تقدر تختار صفحة تانية أو ترجع للمتجر.';grid.replaceWith(p);}
    }
   }
   feedback(data.message,'success',data.view_url);
  }catch(error){feedback(error.message,'error');}
  finally{form.dataset.busy='0';if(button)button.disabled=false;}
 });
 document.addEventListener('click',async event=>{
  const copy=event.target.closest('[data-copy]');if(!copy)return;
  try{await navigator.clipboard.writeText(copy.dataset.copy);feedback('تم نسخ كود الطلب.','success');}catch{feedback('كود الطلب: '+copy.dataset.copy,'success');}
 });
})();
