import { useEffect, useState } from 'react';
import { ArrowDown, ArrowUp, Plus, Trash2 } from 'lucide-react';
import { rpc } from '../api';
import { useApp } from '../context';
import { Button, Section } from '../ui';
import { defaultOrbActions, orbActions, type OrbActionId, type OrbPreferences } from '../orbActions';
import '../orbSettings.css';

export default function OrbSettingsPage() {
  const { connected, run } = useApp();
  const [actions, setActions] = useState<OrbActionId[]>(defaultOrbActions);
  const [saved, setSaved] = useState<OrbActionId[]>();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let disposed = false;
    if (connected) void run(() => rpc<OrbPreferences>('orb.settings.get')).then(value => { if (value && !disposed) { setActions(value.actions); setSaved(value.actions); } });
    return () => { disposed = true; };
  }, [connected, run]);
  const move = (index: number, offset: number) => setActions(current => { const next = [...current]; [next[index], next[index + offset]] = [next[index + offset], next[index]]; return next; });
  const save = async () => {
    setBusy(true);
    try { const value = await run(() => rpc<OrbPreferences>('orb.settings.save', { actions }), '快捷菜单已更新'); if (value) { setSaved(value.actions); setActions(value.actions); } }
    finally { setBusy(false); }
  };
  return <Section title="悬浮球快捷菜单" description="选择 1–6 项，从顶部开始按顺时针排列。保存后立即生效。" action={<Button disabled={!saved || busy || JSON.stringify(actions) === JSON.stringify(saved)} variant="primary" onClick={() => void save()}>保存快捷菜单</Button>}>
    <div className="orb-settings-list">{actions.map((id, index) => {
      const item = orbActions.find(a => a.id === id)!;
      return <div key={id} className="orb-settings-row"><span className="orb-settings-number">{index + 1}</span><item.icon size={19}/><span className="orb-settings-name"><strong>{item.label}</strong><small>{item.description}</small></span><Button disabled={!saved || busy || index === 0} aria-label={`上移${item.label}`} onClick={() => move(index, -1)}><ArrowUp size={15}/></Button><Button disabled={!saved || busy || index === actions.length - 1} aria-label={`下移${item.label}`} onClick={() => move(index, 1)}><ArrowDown size={15}/></Button><Button disabled={!saved || busy || actions.length === 1} aria-label={`移除${item.label}`} onClick={() => setActions(actions.filter(a => a !== id))}><Trash2 size={15}/></Button></div>;
    })}</div>
    <div className="orb-settings-heading"><strong>添加功能 · {actions.length}/6</strong><Button disabled={!saved || busy} onClick={() => setActions([...defaultOrbActions])}>恢复默认</Button></div>
    <div className="orb-settings-options">{orbActions.filter(item => !actions.includes(item.id)).map(item => <Button key={item.id} disabled={!saved || busy || actions.length >= 6} title={item.description} onClick={() => setActions([...actions, item.id])}><Plus size={14}/><item.icon size={16}/>{item.label}</Button>)}</div>
  </Section>;
}
