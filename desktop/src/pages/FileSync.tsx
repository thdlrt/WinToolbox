import { useEffect, useState } from 'react';
import { FolderSync, Plus, RefreshCw, Download } from 'lucide-react';
import { native, rpc, type Job } from '../api';
import { useApp } from '../context';
import { Button, CheckField, dateText, Empty, Field, Notice, Section, activeJob, statusLabel } from '../ui';

interface Result { time: number; ok: boolean; name: string; message: string; changed: number; backups: string[] }
interface Rule { id?: string; name: string; kind: 'file' | 'folder'; source: string; target: string; bidirectional: boolean; auto: boolean; deletes: boolean; interval: number; retention: number; include: string[]; exclude: string[]; last?: Result }
interface State { rules: Rule[]; history: Result[]; legacy_paths: string[]; legacy_logs?: string[] }
interface Plan { id: string; token: string; changes: number; operations: { path: string; action: string; reason: string }[] }
interface SyncJob extends Job { result?: Plan | { imported?: Rule[]; errors?: { name: string; message: string }[] } }
const blank = (): Rule => ({ name: '', kind: 'file', source: '', target: '', bidirectional: false, auto: false, deletes: false, interval: 30, retention: 30, include: [], exclude: [] });
const actions: Record<string, string> = { skip: '不变', to_target: '源 → 目标', to_source: '目标 → 源', delete_target: '删除目标（有备份）', delete_source: '删除源（有备份）', conflict: '需要选择' };

export default function FileSyncPage() {
  const { connected, run, track, error } = useApp();
  const [state, setState] = useState<State>({ rules: [], history: [], legacy_paths: [] });
  const [draft, setDraft] = useState<Rule>();
  const [selected, setSelected] = useState('');
  const [plan, setPlan] = useState<Plan>();
  const [resolutions, setResolutions] = useState<Record<string, string>>({});
  const [job, setJob] = useState<SyncJob>();
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState('');
  const refresh = async () => { const value = await rpc<State>('filesync.status'); setState(value); return value; };
  useEffect(() => { if (connected) void run(refresh); }, [connected]);
  useEffect(() => {
    if (!job || !activeJob(job.status)) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void rpc<SyncJob>('jobs.get', { id: job.id }).then(async current => {
        if (cancelled) return;
        setJob(current);
        if (!activeJob(current.status)) {
          if (current.status === 'completed') {
            if (current.tool === 'filesync.preview') { setPlan(current.result as Plan); setResolutions({}); }
            if (current.tool === 'filesync.import') {
              const result = current.result as { imported: Rule[]; errors: { name: string; message: string }[]; recovered?: boolean };
              setReport(`已导入 ${result.imported.length} 条规则（自动同步已关闭）。${result.recovered ? '日志未记录同步方向和过滤条件，已恢复为双向、无删除的手动规则，请检查预览。' : ''}${result.errors.map(item => `${item.name}：${item.message}`).join('；')}`);
            }
          } else if (current.status === 'failed') error(current.error || current.message);
          await refresh();
        }
      }).catch(error);
    }, 700);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [job?.id, job?.status]);
  const working = busy || !!job && activeJob(job.status);
  const start = async (method: string, params: Record<string, unknown>) => {
    setBusy(true); setReport('');
    const result = await run(() => rpc<SyncJob>(method, params));
    if (result) { setJob(result); track(result); }
    setBusy(false);
  };
  const save = async () => {
    if (!draft) return;
    setBusy(true);
    const result = await run(() => rpc<Rule>('filesync.save', { ...draft }));
    if (result) { setDraft(undefined); setSelected(result.id!); setPlan(undefined); await run(refresh); }
    setBusy(false);
  };
  const pick = async (side: 'source' | 'target') => {
    if (!draft) return;
    const path = await run(async () => draft.kind === 'folder' ? native.directory() : side === 'source' ? (await native.files(false))[0] : native.save(draft.source.split(/[\\/]/).pop() || 'README.md'));
    if (path) setDraft({ ...draft, [side]: path });
  };
  const importOld = async (path?: string) => {
    const value = path || (await run(() => native.files(false, [{ name: 'FileSync 配置或日志', extensions: ['json', 'jsonl'] }])))?.[0];
    if (value) await start('filesync.import', { path: value });
  };
  const rule = state.rules.find(r => r.id === selected);
  const conflicts = plan?.operations.filter(o => o.action === 'conflict') || [];
  return <>
    <div className="section-footer"><span className="muted">文件与文件夹 · 单向 / 双向同步</span><div className="button-row"><Button disabled={!connected || working} onClick={() => void run(refresh)} aria-label="刷新"><RefreshCw size={16} /></Button><Button disabled={!connected || working} onClick={() => void importOld()}><Download size={16} />导入旧配置</Button><Button variant="primary" disabled={!connected || working} onClick={() => { setDraft(blank()); setPlan(undefined); }}><Plus size={16} />新建规则</Button></div></div>
    {!!state.legacy_paths.length && !state.rules.length && <Notice>检测到旧 FileSync 配置。<button className="inline-link" disabled={working} onClick={() => void importOld(state.legacy_paths[0])}>导入现有规则</button></Notice>}
    {!!state.legacy_logs?.length && <Notice>旧配置损坏时，可从日志找回路径。<button className="inline-link" disabled={working} onClick={() => void importOld(state.legacy_logs![0])}>从日志恢复规则</button></Notice>}
    {report && <Notice>{report}</Notice>}
    {draft && <Section title={draft.id ? '编辑规则' : '新建规则'}>
      <div className="form-grid"><Field label="名称"><input value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} /></Field><Field label="同步类型"><select value={draft.kind} onChange={e => setDraft({ ...draft, kind: e.target.value as Rule['kind'] })}><option value="file">文件 → 文件</option><option value="folder">文件夹 → 文件夹</option></select></Field>
      {(['source', 'target'] as const).map(side => <Field key={side} label={side === 'source' ? '源路径' : '目标路径'} hint={draft.kind === 'file' && side === 'target' ? '完整文件路径，可选择尚不存在的文件。' : undefined}><div className="button-row"><input value={draft[side]} onChange={e => setDraft({ ...draft, [side]: e.target.value })} /><Button onClick={() => void pick(side)}>选择</Button></div></Field>)}
      <Field label="自动检查间隔（秒）"><input type="number" min={5} max={86400} value={draft.interval} onChange={e => setDraft({ ...draft, interval: Number(e.target.value) })} /></Field><Field label="备份保留天数" hint="0 表示永久；只清理此规则在新版中生成的过期备份。"><input type="number" min={0} max={3650} value={draft.retention} onChange={e => setDraft({ ...draft, retention: Number(e.target.value) })} /></Field>
      {draft.kind === 'folder' && <><Field label="只包含（每行一个 glob）" hint="留空包含所有文件，例如 **/*.md。"><textarea value={draft.include.join('\n')} onChange={e => setDraft({ ...draft, include: e.target.value.split('\n') })} /></Field><Field label="排除（每行一个 glob）" hint=".git、.back 和同步临时文件始终排除。"><textarea value={draft.exclude.join('\n')} onChange={e => setDraft({ ...draft, exclude: e.target.value.split('\n') })} /></Field></>}
      </div><div className="inline-checks"><CheckField checked={draft.bidirectional} onChange={value => setDraft({ ...draft, bidirectional: value })} label="双向同步" /><CheckField checked={draft.auto} onChange={value => setDraft({ ...draft, auto: value })} label="自动同步" /><CheckField checked={draft.deletes} onChange={value => setDraft({ ...draft, deletes: value })} label="传播删除（先备份）" /></div>
      {draft.deletes && <Notice tone="warning">单向模式会移除目标独有文件；双向模式仅传播已建立基线的删除。删除前保存备份。</Notice>}
      {draft.auto && <Notice>工具箱运行期间按间隔校验并同步。双向冲突会停止该次同步，等待手动选择。</Notice>}
      <div className="section-footer"><Button disabled={working} onClick={() => setDraft(undefined)}>取消</Button><Button variant="primary" disabled={working || !draft.source || !draft.target} onClick={() => void save()}>保存规则</Button></div>
    </Section>}
    <Section title="同步规则">
      {!state.rules.length ? <Empty title="还没有同步规则">新建规则或导入旧 FileSync 的 store.json。</Empty> : <div className="filesync-rules">{state.rules.map(item => <button key={item.id} className={`filesync-rule ${selected === item.id ? 'selected' : ''}`} disabled={working} onClick={() => { setSelected(item.id!); setPlan(undefined); setResolutions({}); }}><strong>{item.name}</strong><span>{item.source} {item.bidirectional ? '↔' : '→'} {item.target}</span><small>{item.auto ? `自动 · ${item.interval} 秒` : '手动'} · {item.last ? `${dateText(item.last.time)} · ${item.last.message}` : '尚未同步'}</small></button>)}</div>}
      {rule && <div className="section-footer"><div className="button-row"><Button disabled={working} onClick={() => { setDraft({ ...rule }); setPlan(undefined); }}>编辑</Button><Button disabled={working} variant="danger" onClick={() => { if (window.confirm(`删除规则“${rule.name}”？同步文件和备份会保留。`)) void run(async () => { await rpc('filesync.remove', { id: rule.id }); setSelected(''); setPlan(undefined); await refresh(); }); }}>删除规则</Button></div><Button variant="primary" disabled={working} onClick={() => { setPlan(undefined); void start('filesync.preview', { id: rule.id }); }}><FolderSync size={16} />预览同步</Button></div>}
    </Section>
    {job && <Notice tone={job.status === 'failed' ? 'warning' : 'info'}>{statusLabel(job.status)} · {job.message}{activeJob(job.status) && <button className="inline-link" onClick={() => void run(() => rpc('jobs.cancel', { id: job.id }))}>取消任务</button>}</Notice>}
    {plan && rule && <Section title="同步预览" description={`${plan.changes} 项变更 · ${conflicts.length} 项冲突`}>
      <div className="filesync-plan">{plan.operations.map(o => <div className="filesync-operation" key={o.path}><div><strong>{o.path || rule.name}</strong><small>{o.reason}</small></div><span>{actions[o.action]}</span>{o.action === 'conflict' && <select aria-label={`保留哪一侧：${o.path || rule.name}`} value={resolutions[o.path] || ''} onChange={e => setResolutions({ ...resolutions, [o.path]: e.target.value })}><option value="">请选择</option><option value="source">保留源端（包括删除）</option><option value="target">保留目标端（包括删除）</option></select>}</div>)}</div>
      {!plan.operations.length && <Empty title="没有匹配的文件" />}
      <div className="section-footer"><span className="muted">覆盖和删除前备份；执行前重新校验。</span><Button variant="primary" disabled={working || conflicts.some(o => !resolutions[o.path])} onClick={() => { void start('filesync.sync', { id: plan.id, token: plan.token, resolutions }); setPlan(undefined); }}>确认同步</Button></div>
    </Section>}
    {!!state.history.length && <Section title="最近记录"><div className="filesync-plan">{state.history.map((item, index) => <div className="filesync-operation" key={index}><div><strong>{item.name} · {item.ok ? '完成' : '未完成'}</strong><small>{dateText(item.time)} · {item.message}</small></div>{!!item.backups.length && <Button onClick={() => void run(() => native.open(item.backups[0]))}>查看备份（{item.backups.length}）</Button>}</div>)}</div></Section>}
  </>;
}
