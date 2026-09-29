import { strict as assert } from 'node:assert';
import { mock } from 'node:test';
import { observeQuickJob } from './quickJob.ts';
const flush=async()=>{for(let n=0;n<5;n++)await Promise.resolve();};
mock.timers.enable({apis:['setTimeout']});
try{
 let reads=0,resolve,result,errors=[];
 const stop=observeQuickJob(async()=>({status:++reads===1?'running':'completed'}),()=>new Promise(r=>resolve=r),v=>result=v,e=>errors.push(e),100);
 await flush();mock.timers.tick(100);await flush();
 assert.equal(reads,2);assert.equal(result,undefined,'completed jobs wait for their result snapshot');
 resolve({translation:'性能'});await flush();assert.deepEqual(result,{translation:'性能'});assert.deepEqual(errors,[]);stop();
 let late=false;const stopLate=observeQuickJob(async()=>({status:'completed'}),()=>new Promise(r=>resolve=r),()=>late=true,()=>{});
 await flush();stopLate();resolve({translation:'stale'});await flush();assert.equal(late,false,'changed selections ignore stale results');
 let fetches=0;observeQuickJob(async()=>({status:'failed',error:'API 未配置'}),async()=>{fetches++;},()=>{},e=>errors.push(e));await flush();
 assert.equal(fetches,0);assert.deepEqual(errors,['API 未配置']);
}finally{mock.timers.reset();}
console.log('PASS async result delivery, stale selection disposal, and failed jobs');
