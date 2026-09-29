import { useEffect, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { Copy, Languages, LoaderCircle, Settings2, X } from 'lucide-react';
import { errorText, native, rpc, type Job } from './api';
import { copyQuickText, quickSnapshot, quickTools, startQuickAction, type DissolvePlan, type QuickSnapshot } from './quickMenuApi';
import { Button, Notice } from './ui';
import { quickActionIcon } from './quickIcons';
import { toolIcons } from './toolIcons';
import { observeQuickJob } from './quickJob';
import './quickMenu.css';

export function QuickActions({ snapshot, onChange }: {snapshot:QuickSnapshot;onChange:(value:QuickSnapshot)=>void}) {
  const [failure,setFailure]=useState(''), [job,setJob]=useState<Job>(), [busy,setBusy]=useState(false), [plan,setPlan]=useState<DissolvePlan>(), [copied,setCopied]=useState(false);
  const current=useRef(snapshot.id);current.current=snapshot.id;
  const translate=async()=>{
    setFailure('');setBusy(true);
    try { const value=await rpc<Job>('quick.translate',{id:snapshot.id});if(current.current===snapshot.id)setJob(value); }
    catch(e){if(current.current===snapshot.id)setFailure(errorText(e));}finally{setBusy(false);}
  };
  useEffect(()=>{setFailure('');setJob(undefined);setPlan(undefined);setCopied(false);if(snapshot.foreign&&snapshot.settings.auto_translate&&!snapshot.translation)void translate();},[snapshot.id]);
  useEffect(()=>{
    if(!job)return;
    return observeQuickJob(()=>rpc<Job>('jobs.get',{id:job.id}),()=>quickSnapshot(snapshot.id),next=>{onChange(next);setJob(undefined);},e=>{setFailure(errorText(e));setJob(undefined);});
  },[job?.id,snapshot.id]);
  const run=async(action:string,token?:string)=>{
    setFailure('');setBusy(true);setCopied(false);
    try {if(action==='dissolve'&&!token){setPlan(await rpc<DissolvePlan>('quick.preview',{id:snapshot.id}));return;}
      const value=await startQuickAction(snapshot.id,action,token);if(current.current===snapshot.id){setPlan(undefined);setJob(value);}
    }catch(e){setFailure(errorText(e));}finally{setBusy(false);}
  };
  const copy=async(text:string)=>{try{await copyQuickText(text);setCopied(true);}catch(e){setFailure(errorText(e));}};
  const working=busy||!!job;
  return <>
    {snapshot.message&&<Notice>{snapshot.message}</Notice>}
    {snapshot.translation&&<section className="quick-translation" aria-label="选区翻译">
      {snapshot.translation.phonetic&&<p className="quick-phonetic">{snapshot.text} <span>{snapshot.translation.phonetic}</span></p>}
      <p>{snapshot.translation.translation}</p>{snapshot.translation.meanings.map((text,i)=><p key={i}>{text}</p>)}
      <button className="quick-copy" aria-label="复制译文" onClick={()=>void copy(snapshot.translation!.translation)}><Copy size={14}/>{copied?'已复制':'复制'}</button>
    </section>}
    {working&&<div className="quick-working" role="status"><LoaderCircle size={16} className="spin"/>{job?.tool==='quick.translate'?'正在翻译…':'正在处理…'}</div>}
    {failure&&<Notice tone="warning">{failure}</Notice>}
    {snapshot.output&&<section className="quick-output"><pre>{snapshot.output}</pre><Button onClick={()=>void copy(snapshot.output)}><Copy size={14}/>{copied?'已复制':'复制结果'}</Button></section>}
    {plan&&<section className="quick-confirm"><strong>解散 {plan.roots.length} 个文件夹</strong><p>将 {plan.operations.length} 个直接子项移至各自上一级，然后移除空文件夹。</p>
      <div className="quick-plan">{plan.operations.map((op,i)=><div key={i}>{op.source}<br/>→ {op.target}</div>)}{!plan.operations.length&&plan.roots.map(root=><div key={root}>{root}（空）</div>)}</div>
      <div className="button-row"><Button onClick={()=>setPlan(undefined)}>取消</Button><Button variant="primary" disabled={working} onClick={()=>void run('dissolve',plan.token)}>确认解散</Button></div>
    </section>}
    <div className="quick-action-list">
      {snapshot.foreign&&<button disabled={working} onClick={()=>void translate()}><Languages size={19}/><span>{snapshot.translation?'重新翻译':'翻译'}</span></button>}
      {snapshot.actions.map(action=>{const Icon=quickActionIcon(action.id);return <button key={action.id} disabled={working} title={action.description||action.path} onClick={()=>void run(action.id)}><span className={'quick-action-icon icon-'+action.id}><Icon size={18}/></span><span>{action.name}</span></button>;})}
    </div>
  </>;
}

export default function QuickMenu(){
  const [snapshot,setSnapshot]=useState<QuickSnapshot>(),[error,setError]=useState('');
  useEffect(()=>{
    let disposed=false,off:(()=>void)|undefined;
    const theme=localStorage.getItem('wintoolbox-theme')||'system';
    document.documentElement.dataset.theme=theme==='system'?(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'):theme;
    const receive=(value:QuickSnapshot)=>{if(!disposed&&value?.id){setSnapshot(value);setError('');requestAnimationFrame(()=>document.querySelector('.quick-scroll')?.scrollTo(0,0));}};
    void listen<QuickSnapshot>('quick-context',e=>receive(e.payload)).then(value=>{if(disposed)value();else off=value;}).catch(e=>setError(errorText(e)));
    void invoke<QuickSnapshot>('quick_snapshot').then(receive).catch(e=>setError(errorText(e)));
    const key=(event:KeyboardEvent)=>{if(event.key==='Escape')void invoke('quick_hide');};window.addEventListener('keydown',key);
    return()=>{disposed=true;off?.();window.removeEventListener('keydown',key);};
  },[]);
  const open=async(page:string)=>{try{await native.orbAction(page);await invoke('quick_hide');}catch(e){setError(errorText(e));}};
  return <main className="quick-menu"><header className="quick-header"><strong>{snapshot?.paths.length?`选中 ${snapshot.paths.length} 个项目`:snapshot?.text?`选中文本 · ${snapshot.text.length} 字符`:'快捷菜单'}</strong><div><button aria-label="快捷菜单设置" onClick={()=>void open('quick-settings')}><Settings2 size={17}/></button><button aria-label="关闭快捷菜单" onClick={()=>void invoke('quick_hide')}><X size={18}/></button></div></header>
    <div className="quick-scroll">{error&&<Notice tone="warning">{error}</Notice>}{snapshot&&<><QuickActions key={snapshot.id} snapshot={snapshot} onChange={setSnapshot}/>
      <div className="quick-tools">{snapshot.settings.tools.map(id=>{const Icon=toolIcons[id as keyof typeof toolIcons]||Settings2;return <button key={id} onClick={()=>void open(id)}><Icon size={18}/>{quickTools.find(tool=>tool[0]===id)?.[1]||id}</button>;})}</div></>}
    </div>
  </main>;
}
