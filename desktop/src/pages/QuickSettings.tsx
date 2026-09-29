import { useEffect,useState } from 'react';
import { Save } from 'lucide-react';
import { getQuickSettings,saveQuickSettings,quickTools,type QuickSettingsValue } from '../quickMenuApi';
import { useApp } from '../context';
import { Button,CheckField,Field,Notice,Section } from '../ui';
import { errorText,subscribe } from '../api';

export default function QuickSettings(){
  const {connected,navigate}=useApp();const [config,setConfig]=useState<QuickSettingsValue>(),[failure,setFailure]=useState(''),[busy,setBusy]=useState(false),[saved,setSaved]=useState(false);
  useEffect(()=>{if(!connected)return;let disposed=false;void getQuickSettings().then(v=>{if(!disposed)setConfig(v);}).catch(e=>setFailure(errorText(e)));return()=>{disposed=true;};},[connected]);
  useEffect(()=>{let disposed=false,off:(()=>void)|undefined;void subscribe(e=>{if(e.type==='quick.settings.changed')void getQuickSettings().then(v=>{if(!disposed)setConfig(v);});}).then(v=>{if(disposed)v();else off=v;});return()=>{disposed=true;off?.();};},[]);
  const update=(patch:Partial<QuickSettingsValue>)=>{setConfig(old=>old?{...old,...patch}:old);setSaved(false);};
  const save=async()=>{if(!config)return;setBusy(true);setFailure('');try{setConfig(await saveQuickSettings(config));setSaved(true);}catch(e){setFailure(errorText(e));}finally{setBusy(false);}};
  if(!config)return <Notice>{failure||'正在读取快捷菜单设置…'}</Notice>;
  return <div className="form-stack">{failure&&<Notice tone="warning">{failure}</Notice>}{config.runtime_error&&<Notice tone="warning">{config.runtime_error}</Notice>}
    <Section title="触发方式"><CheckField label="启用快捷菜单" checked={config.enabled} onChange={enabled=>update({enabled})}/>
      <div className="form-grid"><Field label="长按按键"><select value={config.trigger} onChange={e=>update({trigger:e.target.value})}><option value="middle">鼠标中键</option><option value="x1">鼠标侧键 1（后退）</option><option value="x2">鼠标侧键 2（前进）</option><option value="keyboard">键盘快捷键</option></select></Field><Field label="长按时长（毫秒）"><input type="number" min={200} max={1500} step={50} value={config.hold_ms} onChange={e=>update({hold_ms:Number(e.target.value)})}/></Field></div>
      {config.trigger==='keyboard'&&<Field label="快捷键" hint="例如 Ctrl+Shift+Space、Alt+Q 或 F8。"><input value={config.shortcut} onChange={e=>update({shortcut:e.target.value})}/></Field>}
    </Section>
    <Section title="自动翻译"><CheckField label="自动翻译外语选区" checked={config.auto_translate} onChange={auto_translate=>update({auto_translate})}/><Field label="目标语言"><input value={config.target_language} onChange={e=>update({target_language:e.target.value})}/></Field></Section>
    <Section title="始终显示的工具入口"><div className="quick-tool-options">{quickTools.map(([id,label])=><CheckField key={id} label={label} checked={config.tools.includes(id)} onChange={checked=>update({tools:checked?[...config.tools,id]:config.tools.filter(v=>v!==id)})}/>)}</div></Section>
    <div className="button-row"><Button variant="primary" busy={busy} onClick={()=>void save()}><Save size={16}/>{saved?'已保存':'保存设置'}</Button><Button onClick={()=>navigate('scripts')}>管理快捷脚本与触发条件</Button></div>
  </div>;
}
