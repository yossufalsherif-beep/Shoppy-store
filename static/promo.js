/* Promo code forms update in place. Never reset checkout delivery fields. */
(() => {
 'use strict';
 const activeForms=new WeakSet();
 const lastValue=new WeakMap();
 const rup='application/json';

 function message(form,text,isError){
  const box=form.closest('[data-promo-box], [data-promo-admin-section]') || form;
  let output=box.querySelector('[data-promo-feedback]');
  if(!output)output=form.querySelector('[data-promo-feedback]');
  if(!output){
   output=document.createElement('p');
   output.setAttribute('role','status');
   output.setAttribute('aria-live','polite');
   output.dataset.promoFeedback='';
   form.insertAdjacentElement('afterend',output);
  }
  output.hidden=false;
  output.textContent=text;
  output.classList.toggle('is-error',!!isError);
 }

 function syncKind(form){
  if(!form?.matches('.promo-admin-form'))return;
  const kind=form.querySelector('[name="kind"]')?.value;
  const visible=['fixed_order','fixed_item','shipping_amount'].includes(kind);
  for(const field of form.querySelectorAll('[data-promo-field]')){
   field.hidden=(field.dataset.promoField==='percent' ? kind!=='percent'
     : field.dataset.promoField==='amount' ? !visible : kind!=='fixed_item');
   // Hidden fields must not block native constraint validation.
   field.querySelectorAll('input').forEach(input=>{
    input.disabled=field.hidden;
    if(!field.hidden&&field.dataset.promoField==='percent')input.required=true;
    if(!field.hidden&&field.dataset.promoField==='amount')input.required=true;
   });
  }
 }

 function syncAllAdmin(){
  document.querySelectorAll('form.promo-admin-form').forEach(syncKind);
 }

 function setCustomerState(data){
  document.querySelectorAll('[data-promo-box]').forEach(box=>{
   const state=box.querySelector('[data-promo-state]');
   const removal=box.querySelector('[data-promo-remove]');
   const entry=box.querySelector('input[name="promo_code"]');
   if(entry)entry.value=data?.code||'';
   if(state){
    const bits=data?.active?[
     'الكود '+data.code+' مضاف — '+(data.kind_label||'خصم'),
     data.kind==='percent'?data.percent+'%':data.kind==='free_shipping'?'':(Number(data.amount_piastres||0)/100).toFixed(2)+' جنيه',
     'الحد الأدنى '+data.min_items+' قطعة'
    ].filter(Boolean):[data?.message||''];
    state.textContent=data?.active?bits.join(' · '):data?.message||'';
    state.hidden=!data;
   }
   if(removal)removal.hidden=!data;
  });
 }

 async function readJSON(form,submitter){
  // The rendered form can outlive a session change in another tab.
  // Refresh before the mutation; never retry an admin write after an error.
  const sessionResponse=await fetch('/api/csrf-token',{credentials:'same-origin',cache:'no-store',
        headers:{'Accept':rup}});
  if(!sessionResponse.ok)throw new Error('تعذر تحديث الجلسة؛ حدّث الصفحة وحاول مرة أخرى.');
  const sessionData=await sessionResponse.json();
  if(typeof sessionData.csrf!=='string'||sessionData.csrf.length<16)
   throw new Error('تعذر تحديث الجلسة؛ حدّث الصفحة وحاول مرة أخرى.');
  if(form.dataset.asyncPromo==='admin'&&!sessionData.authenticated)
   throw new Error('انتهت الجلسة؛ سجل الدخول مرة أخرى وجرّب.');
  form.querySelector('input[name="csrf"]').value=sessionData.csrf;
  const payload=new FormData(form);
  if(submitter?.name)payload.set(submitter.name,submitter.value);
  const response=await fetch(form.action,{method:'POST',body:payload,credentials:'same-origin',
        headers:{'Accept':rup,'X-Requested-With':'XMLHttpRequest'},redirect:'follow'});
  const ctype=response.headers.get('content-type')||'';
  if(!ctype.includes('application/json'))throw new Error(
    response.redirected?'انتهت الجلسة؛ سجل الدخول مرة أخرى وجرّب.':'تعذر تنفيذ العملية الآن؛ حاول مرة أخرى.');
  const result=await response.json();
  if(!response.ok||!result.ok)throw new Error(result.message||'تعذر حفظ التغيير.');
  return result;
 }

 async function updateAdminList(){
  const oldSection=document.getElementById('promo-code-list');
  if(!oldSection)return;
  const response=await fetch('/admin/promo-codes',{credentials:'same-origin',cache:'no-store'});
  if(!response.ok||response.redirected)throw new Error('اتحفظ التعديل، لكن تعذر تحديث الجدول. افتح الصفحة لاحقًا.');
  const html=await response.text();
  const newSection=new DOMParser().parseFromString(html,'text/html').getElementById('promo-code-list');
  if(!newSection)throw new Error('اتحفظ التعديل، لكن تعذر تحديث الجدول. افتح الصفحة لاحقًا.');
  oldSection.replaceWith(document.importNode(newSection,true));
  syncAllAdmin();
 }

 document.addEventListener('change',event=>{
  const form=event.target.closest('form.promo-admin-form');
  if(form&&event.target.matches('[name="kind"]'))syncKind(form);
 });

 document.addEventListener('click',event=>{
  const preset=event.target.closest('[data-select-free-shipping]');
  if(!preset)return;
  const form=preset.closest('[data-promo-admin-section]')?.querySelector('form.promo-admin-form');
  const select=form?.querySelector('[name="kind"]');
  if(!select)return;
  select.value='free_shipping';
  select.dispatchEvent(new Event('change',{bubbles:true}));
  select.focus();
  message(form,'تم اختيار شحن مجاني بالكامل لهذا الكود. كمّل باقي الإعدادات واضغط إنشاء الكود.',false);
 });

 document.addEventListener('submit',async event=>{
  const form=event.target.closest('form[data-async-promo]');
  if(!form)return;
  event.preventDefault();
  if(activeForms.has(form))return;
  const button=event.submitter||form.querySelector('button[type="submit"]');
  activeForms.add(form);
  if(button)button.disabled=true;
  try{
   const result=await readJSON(form,button);
   if(form.dataset.asyncPromo==='customer'){
    setCustomerState(result.promo_state||null);
    message(form,result.message||'تم تحديث الكود.',false);
    // The quote also contains a signed token and must be recalculated on the server.
    document.dispatchEvent(new Event('promo:changed'));
   }else{
    if(form.getAttribute('action')?.endsWith('/create')){
     form.reset();
     syncKind(form);
    }
    await updateAdminList();
    const status=document.querySelector('[data-promo-admin-feedback]');
    if(status){status.hidden=false;status.textContent=result.message||'تم حفظ التعديل.';}
   }
  }catch(error){
   message(form,error.message||'حدث خطأ، حاول مرة أخرى.',true);
  }finally{
   activeForms.delete(form);
   if(button?.isConnected)button.disabled=false;
  }
 });

 syncAllAdmin();
})();
