import { useState } from 'react';
import { errorText, rpc } from './api';
import { Button, Field, Modal, Notice } from './ui';
import type { ExpenseProject } from './expensesState';

export default function ExpenseProjects({ projects, onChanged, onClose }: { projects: ExpenseProject[]; onChanged: () => Promise<void>; onClose: () => void }) {
  const [selected, setSelected] = useState<ExpenseProject>();
  const [name, setName] = useState('');
  const [mode, setMode] = useState<'general' | 'half'>('general');
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState('');
  const edit = (project?: ExpenseProject) => { setSelected(project); setName(project?.name || ''); setMode(project?.settlement_mode || 'general'); setFailure(''); };
  const save = async (archived = selected?.archived || false) => {
    setBusy(true); setFailure('');
    try { await rpc('expenses.projects.save', { ...(selected ? { id: selected.id, heads: selected.heads } : {}), name: name.trim(), settlement_mode: mode, archived }); await onChanged(); edit(); }
    catch (reason) { setFailure(errorText(reason)); }
    finally { setBusy(false); }
  };
  return <Modal title="项目管理" onClose={() => { if (!busy) onClose(); }}><div className="modal-body"><div className="expense-project-list">{projects.map(project => <Button key={project.id} disabled={busy} variant={selected?.id === project.id ? 'primary' : 'ghost'} onClick={() => edit(project)}>{project.name}{project.archived ? ' · 已归档' : ''}</Button>)}<Button disabled={busy} onClick={() => edit()}>新建项目</Button></div><Field label={selected ? '项目名称' : '新项目名称'}><input value={name} maxLength={100} disabled={busy} onChange={event => setName(event.target.value)} /></Field><Field label="结算方式"><select value={mode} disabled={busy} onChange={event => setMode(event.target.value as 'general' | 'half')}><option value="general">收支记账</option><option value="half">报销后两人均摊</option></select></Field><p className="small-note">归档项目会保留条目和附件，仍可查询与导出。</p>{failure && <Notice tone="warning">{failure}</Notice>}<div className="modal-footer">{selected && <Button disabled={busy || !name.trim()} onClick={() => void save(!selected.archived)}>{selected.archived ? '恢复项目' : '归档项目'}</Button>}<span className="flex-spacer" /><Button disabled={busy} onClick={onClose}>关闭</Button><Button variant="primary" disabled={!name.trim()} busy={busy} onClick={() => void save()}>{selected ? '保存' : '创建项目'}</Button></div></div></Modal>;
}
