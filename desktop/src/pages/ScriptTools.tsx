import { useCallback, useEffect, useState } from 'react';
import { ArrowUpRight, FileCode2, FolderOpen, Pencil, Play, Plus, RefreshCw, RotateCcw, Square, Trash2 } from 'lucide-react';
import { native, rpc, type Job } from '../api';
import { useApp } from '../context';
import { basename, Button, dateText, Empty, Field, IconButton, Loading, Modal, Notice, PageActions, Progress, Section, Status } from '../ui';
import '../scriptTools.css';

interface Entry { id: string; name: string; note: string; path: string; folder: string; runner: string; exists: boolean; builtin: boolean; update_available: boolean; openable: boolean; runnable: boolean; last_job_id?: string }
interface Console { job_id: string; status: string; progress: number; message: string; error?: string | null; created_at?: string; name: string; runner: string; path: string; output: string }
interface Session { jobId: string; status: string; progress: number; output: string; error?: string | null; createdAt?: string; runner: string }
const ACTIVE = ['queued', 'running', 'cancelling'];

export default function ScriptToolsPage() {
  const { connected, run } = useApp();
  const [entries, setEntries] = useState<Entry[]>([]);
  const [sessions, setSessions] = useState<Record<string, Session>>({});
  const [busy, setBusy] = useState('');
  const [editing, setEditing] = useState<Entry>();
  const [removing, setRemoving] = useState<Entry>();
  const [form, setForm] = useState({ name: '', note: '' });

  // Terminals are keyed by entry, so a rerun replaces that entry's output only.
  const watch = (entry: Entry, data: Console) => setSessions(old => ({ ...old, [entry.id]: { jobId: data.job_id, status: data.status, progress: data.progress, output: data.output, error: data.error, createdAt: data.created_at, runner: data.runner } }));
  const show = useCallback(async (entry: Entry, jobId: string) => {
    try { watch(entry, await rpc<Console>('script_tools.console', { job_id: jobId })); } catch { /* the job may be gone after a data reset */ }
  }, []);
  const load = useCallback(async () => {
    const result = await run(() => rpc<{ entries: Entry[] }>('script_tools.list'));
    if (!result) return;
    setEntries(result.entries);
    await Promise.all(result.entries.filter(entry => entry.last_job_id).map(entry => show(entry, entry.last_job_id!)));
  }, [run, show]);
  useEffect(() => { if (connected) void load(); }, [connected, load]);

  const watching = [...new Set(Object.values(sessions).filter(item => ACTIVE.includes(item.status)).map(item => item.jobId))];
  useEffect(() => {
    if (!watching.length) return;
    const timer = setInterval(() => { for (const jobId of watching) void rpc<Console>('script_tools.console', { job_id: jobId }).then(data => { setSessions(old => { const entry = Object.entries(old).find(([, item]) => item.jobId === jobId); return entry ? { ...old, [entry[0]]: { ...entry[1], status: data.status, progress: data.progress, output: data.output, error: data.error } } : old; }); }).catch(() => {}); }, 900);
    return () => clearInterval(timer);
  }, [watching.join(',')]);

  const start = async (entry: Entry) => {
    setBusy(entry.id);
    const job = await run(() => rpc<Job>('script_tools.run', { id: entry.id }));
    if (job) setSessions(old => ({ ...old, [entry.id]: { jobId: job.id, status: job.status, progress: 0, output: '', runner: entry.runner } }));
    setBusy('');
  };
  const stop = (session: Session) => void run(() => rpc('jobs.cancel', { id: session.jobId }));
  const add = async () => {
    const files = await run(() => native.files(false, [{ name: '脚本', extensions: ['py', 'ps1', 'cmd', 'bat', 'exe', 'js', 'mjs'] }]));
    if (!files?.length) return;
    setBusy('add');
    const result = await run(() => rpc('script_tools.add', { path: files[0] }), '脚本已加入列表。');
    if (result) await load();
    setBusy('');
  };
  const save = async () => { if (!editing) return; setBusy('edit'); const result = await run(() => rpc('script_tools.update', { id: editing.id, ...form }), '已保存。'); if (result) { setEditing(undefined); await load(); } setBusy(''); };
  const confirmRemove = async () => { if (!removing) return; setBusy('remove'); const result = await run(() => rpc('script_tools.remove', { id: removing.id }), '已从列表移除，脚本文件保留。'); if (result) { setSessions(old => { const next = { ...old }; delete next[removing.id]; return next; }); setRemoving(undefined); await load(); } setBusy(''); };
  const reset = async (entry: Entry) => { setBusy('reset'); const result = await run(() => rpc('script_tools.reset', { id: entry.id }), '已恢复内置版本。'); if (result) { setEditing(undefined); await load(); } setBusy(''); };

  return <>
    <PageActions action={<div className="button-row"><Button disabled={!connected} onClick={() => void add()} busy={busy === 'add'}><Plus size={16} />新增脚本</Button><Button variant="ghost" disabled={!connected} onClick={() => void load()}><RefreshCw size={15} />刷新</Button></div>} />
    {entries.length ? entries.map(entry => {
      const session = sessions[entry.id];
      return <Section key={entry.id} className="script-section">
        <div className="script-row">
          <Button variant="primary" className="script-run" disabled={!connected || !entry.runnable} busy={busy === entry.id} title={entry.runnable ? `运行 ${entry.name}` : '该文件不存在或类型不受支持'} onClick={() => void start(entry)}><Play size={15} /></Button>
          <div className="script-body">
            <strong>{entry.name}</strong>
            {entry.note && <p>{entry.note}</p>}
            <span className="script-path" title={entry.path}>{entry.path}{!entry.exists && ' · 文件不存在'}</span>
            {entry.update_available && <span className="script-update">内置脚本有更新：你改过这份副本，未自动覆盖，可在编辑里恢复内置版本。</span>}
          </div>
          <div className="script-actions">
            {entry.openable && <IconButton label="打开脚本文件" disabled={!connected} onClick={() => void run(() => native.open(entry.path))}><ArrowUpRight size={16} /></IconButton>}
            <IconButton label="打开所在文件夹" disabled={!connected} onClick={() => void run(() => native.open(entry.folder))}><FolderOpen size={16} /></IconButton>
            <IconButton label={`编辑 ${entry.name}`} onClick={() => { setEditing(entry); setForm({ name: entry.name, note: entry.note }); }}><Pencil size={16} /></IconButton>
            <IconButton label={`删除 ${entry.name}`} onClick={() => setRemoving(entry)}><Trash2 size={16} /></IconButton>
          </div>
        </div>
        {session && <div className="terminal">
          <div className="terminal-head"><FileCode2 size={14} /><code>$ {session.runner || entry.runner} {basename(entry.path)}</code><Status value={session.status} /><span className="flex-spacer" />{session.createdAt && <time>{dateText(session.createdAt)}</time>}{ACTIVE.includes(session.status) && <Button variant="ghost" onClick={() => stop(session)}><Square size={13} />停止</Button>}</div>
          {ACTIVE.includes(session.status) && <Progress value={session.progress} />}
          <pre className="terminal-body">{session.output || (ACTIVE.includes(session.status) ? '正在运行…' : '（没有输出）')}</pre>
          {session.error && !ACTIVE.includes(session.status) && <p className="terminal-error">{session.error}</p>}
        </div>}
      </Section>;
    }) : connected ? <Empty title="还没有脚本">点「新增脚本」选择 .py、.ps1、.cmd、.bat、.exe、.js 文件。</Empty> : <Loading label="等待本地服务连接…" />}
    {editing && <Modal title="编辑脚本" onClose={() => setEditing(undefined)}><div className="modal-body">
      <div className="form-stack">
        <Field label="名称"><input value={form.name} onChange={event => setForm(old => ({ ...old, name: event.target.value }))} /></Field>
        <Field label="说明" hint="留空则不显示"><textarea rows={3} value={form.note} onChange={event => setForm(old => ({ ...old, note: event.target.value }))} /></Field>
        <Field label="路径" hint="脚本文件位置不可在工具箱内修改"><input value={editing.path} readOnly /></Field>
      </div>
      {editing.builtin && <Notice>这是随工具箱提供的内置脚本。{editing.update_available ? '你修改过它，工具箱没有覆盖你的改动。' : ''}「恢复内置版本」会用当前版本的脚本覆盖该文件。</Notice>}
      <div className="modal-footer">
        {editing.builtin && <Button variant="ghost" busy={busy === 'reset'} onClick={() => void reset(editing)}><RotateCcw size={15} />恢复内置版本</Button>}
        <span className="flex-spacer" />
        <Button onClick={() => setEditing(undefined)}>关闭</Button>
        <Button variant="primary" busy={busy === 'edit'} onClick={() => void save()}>保存</Button>
      </div>
    </div></Modal>}
    {removing && <Modal title="移除脚本" onClose={() => setRemoving(undefined)}><div className="modal-body">
      <p>将从列表移除「{removing.name}」。脚本文件保留在原处，需要时可以再用「新增脚本」加回来。</p>
      <div className="modal-footer"><Button onClick={() => setRemoving(undefined)}>取消</Button><Button variant="danger" busy={busy === 'remove'} onClick={() => void confirmRemove()}>移除</Button></div>
    </div></Modal>}
  </>;
}
