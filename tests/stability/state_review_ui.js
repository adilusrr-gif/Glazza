'use strict';
// Independent async-state verification. This is a Node VM test, not browser QA.
// Fake timers execute both intervals and timeouts; the unmodified product script
// must be able to recover if it has a bounded request/response-body timeout.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');
const source=fs.readFileSync(path.join(root,'app/static/staff.js'),'utf8');

function create(mode) {
  let clock=1800000000000,nextTimer=1,release;
  const elements=new Map(),timers=new Map(),calls=[];
  const button={dataset:{restaurantAction:'hold',restaurantTable:'independent-table'},disabled:false};
  const element=selector=>{
    if(!elements.has(selector)) {
      const classes=new Set();
      elements.set(selector,{hidden:false,textContent:'',innerHTML:'',classList:{
        add:v=>classes.add(v),remove:v=>classes.delete(v),toggle:(v,b)=>b?classes.add(v):classes.delete(v)
      },setAttribute(){}});
    }
    return elements.get(selector);
  };
  const payload=()=>({current_staff:{name:'Independent waiter',role:'waiter'},server_time:clock/1000,
    tables:[{id:'independent-table',visit_id:'independent-visit',revision:3}],alerts:[]});
  const schedule=(fn,delay,interval)=>{const id=nextTimer++;timers.set(id,{fn,at:clock+delay,interval});return id;};
  const settle=()=>new Promise(resolve=>setImmediate(resolve));
  const context={
    URLSearchParams,Promise,queueMicrotask,console,
    Date:class extends Date {constructor(...args){super(...(args.length?args:[clock]));}static now(){return clock;}},
    location:{hash:'#token=independent_test_capability_123456',pathname:'/staff',search:''},
    history:{replaceState(){}},sessionStorage:{setItem(){},getItem(){return null;},removeItem(){}},
    document:{hidden:false,querySelector:element,querySelectorAll:q=>q.includes('data-restaurant-action')?[button]:[],addEventListener(){}},
    window:{},navigator:{},
    setTimeout:(fn,ms)=>schedule(fn,ms,0),clearTimeout:id=>timers.delete(id),
    setInterval:(fn,ms)=>schedule(fn,ms,ms),clearInterval:id=>timers.delete(id),
    RestaurantUI:{escape:String,roles:{waiter:'Официант'},rememberFocus(){},restoreFocus(){},setClock(){},timers(){},alerts(){return '';},cards(){return '';}},
    fetch:(url,options)=>{
      calls.push({method:options.method||'GET',signal:Boolean(options.signal)});
      const shouldBlock=mode==='response-body'&&calls.length===2||mode==='action'&&options.method==='POST';
      if(shouldBlock) {
        const pending=new Promise((resolve,reject)=>{release={resolve,reject};});
        if(mode==='response-body')return Promise.resolve({status:200,ok:true,json:()=>pending});
        return pending;
      }
      return Promise.resolve({status:200,ok:true,json:async()=>payload()});
    }
  };
  vm.runInNewContext(source,context,{filename:'app/static/staff.js'});
  async function advance(seconds) {
    for(let i=0;i<seconds;i++) {
      clock+=1000;
      for(const [id,timer] of [...timers.entries()])if(timer.at<=clock) {
        if(timer.interval)timer.at+=timer.interval;else timers.delete(id);
        timer.fn();
      }
      await settle();
    }
  }
  return {calls,elements,button,advance,settle,payload,reject:()=>release.reject(new Error('Controlled independent failure'))};
}

(async()=>{
  const result=[];
  const read=create('response-body');await read.settle();await read.advance(2);
  await read.advance(180);
  read.elements.get('#staff-refresh').onclick();await read.settle();
  const blocked=read.calls.length;
  assert.equal(blocked,2);
  assert.equal(read.elements.get('#staff-offline').hidden,false);
  assert.equal(read.button.disabled,true);
  read.reject();await read.settle();await read.advance(2);
  assert.equal(read.calls.length,3);
  result.push({variant:'fetch resolved but response.json never settles',simulated_stall_seconds:180,
    requests_before_release:blocked,requests_after_rejection_and_next_poll:read.calls.length,
    stale_state_displayed:true,manual_refresh_while_pending:'no new request',abort_signal_present:false});

  const write=create('action');await write.settle();
  const operation=write.button.onclick();await write.settle();await write.advance(180);
  write.elements.get('#staff-refresh').onclick();await write.settle();
  assert.equal(write.calls.length,2);
  assert.equal(write.button.disabled,true);
  write.reject();await operation;await write.settle();
  assert.equal(write.calls.length,3);
  result.push({variant:'POST request never settles',simulated_stall_seconds:180,
    requests_before_release:2,requests_after_rejection:write.calls.length,
    manual_refresh_while_pending:'no new request',abort_signal_present:false});
  const report={finding_id:'ROOT-UI-001',state:'verified',environment:'Independent Node VM synthetic DOM/fetch/clock; all scheduled timeouts and intervals are executed; no physical browser claim',
    source:['app/static/staff.js:27','app/static/staff.js:29','app/static/staff.js:75-90','app/static/staff.js:96-101'],cases:result,
    expected:'Bound request and response-body time, clear busy guards, then refresh actual state after uncertainty.',
    observed:'A pending read body or POST blocks all new polling and manual refresh for the entire180s virtual window; explicit rejection releases the guard.'};
  const output=path.join(root,'docs/audits/restaurant-stability-2026-11-17/evidence/verification/state-review-ui.json');
  fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report,null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
