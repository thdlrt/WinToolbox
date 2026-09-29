import { useEffect,useState } from 'react';
import { Plus,Save,Trash2 } from 'lucide-react';
import { useApp } from '../context';
import { errorText,native,rpc } from '../api';
import { Button,CheckField,Field,Notice,Section } from '../ui';
import { getQuickSettings,saveQuickSettings,triggerNames,type QuickAction,type QuickSettingsValue,type QuickSnapshot } from '../quickMenuApi';
import { QuickActions } from '../QuickMenu';
import { quickActionIcon } from '../quickIcons';

export default function QuickScripts(){
 const {connected}=useApp();const [config,setConfig]=useState<QuickSettingsValue>(),[actions,setActions]=useState<QuickAction[]>([]),[failure,setFailure]=useState(''),[busy,setBusy]=useState(false),[saved,setSaved]=useState(false),[text,setText]=useState(''),[paths,setPaths]=useState<string[]>([]),[snapshot,setSnapshot]=useState<QuickSnapshot>();
 const load=async()=>{const [c,a]=await Promise.all([getQuickSettings(),rpc<{actions:QuickAction[]}>('quick.actions')]);setConfig(c);setActions(a.actions);};
 useEffect(()=>{if(connected)void load().catch(e=>setFailure(errorText(e)));},[connected]);
 const update=(id:string,patch:Partial<QuickAction>)=>{setActions(old=>old.map(a=>a.id===id?{...a,...patch}:a));setSaved(false);};
 const save=async()=>{if(!config)return;setFailure('');setBusy(true);try{const value=await saveQuickSettings({...config,actions:Object.fromEntries(actions.filter(a=>a.builtin).map(a=>[a.id,{enabled:a.enabled,trigger:a.trigger}])),custom:actions.filter(a=>!a.builtin)});setConfig(value);setSaved(true);}catch(e){setFailure(errorText(e));}finally{setBusy(false);}};
 const add=async()=>{try{const files=await native.files(false,[{name:'脚本',extensions:['py','ps1','exe']}]);if(!files.length)return;setActions(old=>[...old,{id:'custom-'+crypto.randomUUID(),name:files[0].split(/[\\/]/).pop()||'自定义脚本',path:files[0],trigger:'selection',enabled:true,builtin:false}]);setSaved(false);}catch(e){setFailure(errorText(e));}};
 const capture=async()=>{try{setFailure('');setSnapshot(await rpc<QuickSnapshot>('quick.capture',{text,paths}));}catch(e){setFailure(errorText(e));}};
 return <div className="form-stack">{failure&&<Notice tone="warning">{failure}</Notice>}
  <Section title="功能与触发条件" action={<Button onClick={()=>void add()} disabled={!connected}><Plus size={16}/>添加本地脚本</Button>}>
   <div className="quick-script-list">{actions.map(a=>{const Icon=quickActionIcon(a.id);return <div className="quick-script-row" key={a.id}><Icon size={20} className="quick-script-icon"/><div><CheckField label={a.name} checked={a.enabled} onChange={enabled=>update(a.id,{enabled})}/>{!a.builtin&&<input aria-label="脚本名称" value={a.name} onChange={e=>update(a.id,{name:e.target.value})}/>}<p className="small-note">{a.description||a.path}</p></div><select aria-label={`${a.name}触发条件`} value={a.trigger} onChange={e=>update(a.id,{trigger:e.target.value})}>{Object.entries(triggerNames).map(([id,name])=><option value={id} key={id}>{name}</option>)}</select>{!a.builtin&&<Button title="移除脚本入口" onClick={()=>{setActions(old=>old.filter(x=>x.id!==a.id));setSaved(false);}}><Trash2 size={15}/></Button>}</div>;})}</div>
   <Button variant="primary" busy={busy} disabled={!config} onClick={()=>void save()}><Save size={16}/>{saved?'已保存':'保存脚本设置'}</Button>
  </Section>
  <Section title="手动使用"><Field label="文本"><textarea rows={4} value={text} onChange={e=>{setText(e.target.value);setPaths([]);}} placeholder="输入或粘贴文本，也可选择文件或文件夹"/></Field>
   {paths.length>0&&<p className="small-note break-word">{paths.join('\n')}</p>}<div className="button-row"><Button onClick={()=>void native.files(true).then(v=>{setPaths(v);setText('');}).catch(e=>setFailure(errorText(e)))}>选择文件</Button><Button onClick={()=>void native.directory().then(v=>{if(v){setPaths([v]);setText('');}}).catch(e=>setFailure(errorText(e)))}>选择文件夹</Button><Button variant="primary" disabled={!connected} onClick={()=>void capture()}>显示可用功能</Button></div>
  </Section>{snapshot&&<Section title="可用功能"><QuickActions key={snapshot.id} snapshot={snapshot} onChange={setSnapshot}/></Section>}
 </div>;
}
