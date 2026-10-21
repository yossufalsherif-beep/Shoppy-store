/* First-party funnel counts only after explicit opt-in, no pixels or external scripts. */
(() => {
 const consent=document.body?.dataset.analyticsConsent||'unset';
 const stage=document.body?.dataset.analyticsStage||'';
 const banner=document.getElementById('analytics-choice');
 const csrf=document.querySelector('meta[name="csrf-token"]')?.content;
 if(!csrf)return;
 const send=async (endpoint,data)=>{
  const response=await fetch(endpoint,{method:'POST',credentials:'same-origin',
   headers:{'Accept':'application/json','Content-Type':'application/x-www-form-urlencoded'},
   body:new URLSearchParams({csrf,...data})});
  if(!response.ok)throw new Error('Network request rejected');
  return response.json();
 };
 const view=()=>{
  if(!stage)return;
  send('/analytics/event',{stage}).catch(()=>{});
 };
 if(consent==='yes')view();
 banner?.addEventListener('click',async(event)=>{
  const button=event.target.closest('[data-analytics-choice]');
  if(!button)return;
  const choice=button.dataset.analyticsChoice;
  banner.querySelectorAll('button').forEach(b=>b.disabled=true);
  try{
   await send('/analytics/consent',{choice});
   banner.remove();
   if(choice==='yes')view();
  }catch{
   banner.querySelectorAll('button').forEach(b=>b.disabled=false);
  }
 });
})();