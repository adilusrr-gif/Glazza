'use strict';

// Execute the unmodified staff script with an explicit synthetic DOM/clock/fetch.
// This tests async recovery logic; it is not a real-browser rendering test.
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../..');
const script = fs.readFileSync(path.join(root, 'app/static/staff.js'), 'utf8');
const output = process.argv[2];

function fixture(mode) {
  let now = 1_800_000_000_000;
  const nodes = new Map(), intervals = [], calls = [], deferred = [];
  let available = true;
  const action = {dataset:{restaurantAction:'greeted',restaurantTable:'table-fixture'},disabled:false};
  function node(id) {
    if (!nodes.has(id)) {
      const classes = new Set();
      nodes.set(id,{hidden:false,textContent:'',innerHTML:'',dataset:{},classList:{
        add:value=>classes.add(value), remove:value=>classes.delete(value),
        toggle:(value,on)=>on?classes.add(value):classes.delete(value),contains:value=>classes.has(value)
      },setAttribute(){},querySelectorAll(){return [];},contains(){return false;}});
    }
    return nodes.get(id);
  }
  const data = () => ({server_time:now/1000,current_staff:{name:'Synthetic waiter',role:'waiter'},
    tables:[{id:'table-fixture',visit_id:'visit-fixture',revision:1,state:'waiting',allowed_actions:['greeted']}],alerts:[],diagnostics:[]});
  const response = (status=200) => ({status,ok:status===200,json:async()=>status===200?data():{detail:'Synthetic rejection'}});
  const context = vm.createContext({
    console, URLSearchParams, Promise, queueMicrotask,
    Date:class extends Date { constructor(...args){super(...(args.length?args:[now]));}static now(){return now;} },
    location:{hash:'#token=synthetic_staff_token_000000000000',pathname:'/staff',search:''},
    history:{replaceState(){}},sessionStorage:{setItem(){},removeItem(){},getItem(){return null;}},
    document:{hidden:false,querySelector:node,querySelectorAll:selector=>selector.includes('data-restaurant-action')?[action]:[],addEventListener(){}},
    window:{},navigator:{},setInterval:(fn,ms)=>{intervals.push({fn,ms});return intervals.length;},
    setTimeout:()=>1,clearTimeout(){},
    RestaurantUI:{escape:String,roles:{waiter:'Официант'},rememberFocus(){return null;},restoreFocus(){},setClock(){},timers(){},alerts(){return '';},cards(){return '';},},
    fetch:(url,options)=>{
      calls.push({url,method:options.method||'GET',has_abort_signal:!!options.signal});
      const shouldHang=(mode==='poll-hang'&&calls.length===2)||(mode==='action-hang'&&options.method==='POST');
      if(shouldHang)return new Promise((resolve,reject)=>deferred.push({resolve,reject}));
      if(!available)return Promise.reject(new Error('Synthetic network unavailable'));
      return Promise.resolve(response());
    }
  });
  vm.runInContext(script,context,{filename:'app/static/staff.js'});
  const settle = () => new Promise(resolve=>setImmediate(resolve));
  async function advance(milliseconds) {
    now+=milliseconds;
    for(const timer of intervals)timer.fn();
    await settle();
  }
  return {nodes,calls,deferred,action,response,settle,advance,setAvailable(value){available=value;}};
}

async function run() {
  const cases=[];
  const polling=fixture('poll-hang');await polling.settle();await polling.advance(2000);
  assert.equal(polling.calls.length,2);
  for(let i=0;i<150;i++)await polling.advance(2000);
  polling.nodes.get('#staff-refresh').onclick();await polling.settle();
  const blockedCalls=polling.calls.length;
  assert.equal(blockedCalls,2,'Reproduction changed: polling now recovers while a request is pending');
  assert.equal(polling.nodes.get('#staff-offline').hidden,false);
  assert.equal(polling.action.disabled,true);
  polling.deferred[0].resolve(polling.response());await polling.settle();await polling.advance(2000);
  assert.equal(polling.calls.length,3,'Control must recover once pending request settles');
  cases.push({id:'pending-poll',simulated_seconds:302,observed:{requests_before_release:blockedCalls,manual_refresh_started_request:false,offline_visible:true,actions_disabled:true,abort_signal:false,requests_after_release:polling.calls.length},expected:'A bounded request timeout releases the polling guard and permits a fresh request without waiting indefinitely for an old response.',finding:true});

  const mutation=fixture('action-hang');await mutation.settle();
  const actionPromise=mutation.action.onclick();await mutation.settle();
  for(let i=0;i<150;i++)await mutation.advance(2000);
  mutation.nodes.get('#staff-refresh').onclick();await mutation.settle();
  assert.equal(mutation.calls.length,2,'Reproduction changed: an indefinite mutation no longer blocks polling');
  mutation.deferred[0].resolve(mutation.response());await actionPromise;await mutation.settle();
  cases.push({id:'pending-action',simulated_seconds:300,observed:{requests_before_release:2,manual_refresh_started_request:false,abort_signal:false,requests_after_release:mutation.calls.length},expected:'A timed-out mutation should release the UI, refresh actual server state, and permit safe recovery.',finding:true});

  const rejected=fixture('rejection');await rejected.settle();rejected.setAvailable(false);await rejected.advance(2000);
  rejected.setAvailable(true);await rejected.advance(2000);
  assert.equal(rejected.calls.length,3);assert.equal(rejected.nodes.get('#staff-offline').hidden,true);
  cases.push({id:'rejected-request-recovers',observed:{requests:3,offline_visible:false},expected:'An explicitly rejected fetch is retried on the next poll.',finding:false});
  const result={target:'app/static/staff.js at tested commit',environment:'Node VM with synthetic DOM, fetch, and accelerated clock; no browser/network claims',cases};
  if(output)fs.writeFileSync(output,JSON.stringify(result,null,2)+'\n');
  console.log(JSON.stringify(result,null,2));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
