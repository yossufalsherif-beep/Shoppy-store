"""Exercise the actual JS request builder with a named-action form control."""
import shutil
import subprocess
import unittest
from pathlib import Path


class PromoFormTargetTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required for browser request regression')
    def test_named_action_control_does_not_replace_request_url(self):
        root = Path(__file__).resolve().parents[1]
        script = r"""
const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const source=fs.readFileSync('static/promo.js','utf8');
const start=source.indexOf('async function readJSON(');
const end=source.indexOf('async function updateAdminList(',start);
const calls=[];
const context={
 rup:'application/json',
 FormData:class { constructor(){this.values={};} set(k,v){this.values[k]=v;} },
 fetch:async (url,options)=>{
  calls.push({url,options});
  if(url==='/api/csrf-token')return {ok:true,json:async()=>({csrf:'fresh-token-for-session',authenticated:true})};
  assert.equal(typeof url,'string');
  return {ok:true,headers:{get:()=> 'application/json'},json:async()=>({ok:true})};
 }
};
vm.createContext(context);
vm.runInContext(source.slice(start,end),context);
(async()=>{
 for(const [kind,action,target] of [
  ['customer','apply','/promo-code/apply'],
  ['customer','remove','/promo-code/apply'],
  ['admin','create','/admin/promo-codes/create']
 ]){
  const form={action:{name:'action',value:action},dataset:{asyncPromo:kind},
   getAttribute:name=>name==='action'?target:null,
   querySelector:()=>({value:''})};
  await context.readJSON(form,{name:'action',value:action});
  const sent=calls.at(-1);
  assert.equal(sent.url,target);
  assert.equal(sent.options.method,'POST');
  assert.equal(sent.options.body.values.action,action);
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script], cwd=root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
