/* Anonymous first-party aggregate page events. No popup, visitor IDs or analytics cookies. */
(() => {
 const stage=document.body?.dataset.analyticsStage||'';
 const csrf=document.querySelector('meta[name="csrf-token"]')?.content;
 if(!stage||!csrf)return;
 fetch('/analytics/event',{
  method:'POST',credentials:'same-origin',
  headers:{'Accept':'application/json','Content-Type':'application/x-www-form-urlencoded'},
  body:new URLSearchParams({csrf,stage})
 }).catch(()=>{});
})();
