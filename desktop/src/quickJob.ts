/** Deliver the result before the consumer clears the active job (and unmounts
 * this observer). Late results from a replaced selection are discarded. */
export function observeQuickJob<T>(loadJob:()=>Promise<{status:string;error?:unknown;message?:string}>,loadResult:()=>Promise<T>,done:(result:T)=>void,failed:(error:unknown)=>void,delay=450){
 let disposed=false,timer:ReturnType<typeof setTimeout>|undefined;
 const poll=async()=>{try{
   const job=await loadJob();if(disposed)return;
   if(['queued','running','cancelling'].includes(job.status)){timer=setTimeout(()=>void poll(),delay);return;}
   if(job.status!=='completed')throw job.error||job.message||'处理未完成';
   const result=await loadResult();if(!disposed)done(result);
 }catch(error){if(!disposed)failed(error);}};
 void poll();return()=>{disposed=true;clearTimeout(timer);};
}
