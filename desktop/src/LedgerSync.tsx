import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { errorText, rpc, type Job } from './api';
import { useApp } from './context';
import { Button, Modal, Notice, Field } from './ui';
import { ledgerMigrationMessage, type LedgerMigrationStatus } from './ledgerSyncState';

interface SyncStatus extends LedgerMigrationStatus { configured: boolean; syncing: boolean; pending: number; last_sync?: string | number | null; error?: string; conflicts: number; incomplete?: number }
interface Conflict { entity_type: string; id: string; title?: string; name?: string; heads: string[]; fields: Record<string, { op_id: string; value: unknown }[]> }
const labels: Record<string, string> = { name: '名称', title: '名称', date: '日期', amount: '金额', currency: '币种', entry_type: '收支类型', status: '报销状态', notes: '备注', category: '分类', project_id: '项目', settlement_mode: '结算方式', archived: '归档状态', target: '目标', port: '端口', timeout_ms: '超时', attempts: '次数' };
const show = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value);
const valueNames: Record<string, string> = { general: '收支记账', half: '报销后两人均摊', income: '收入', expense: '支出', waiting: '等待报销', reimbursed: '已报销', non_reimbursable: '不可报销', not_applicable: '不适用' };
export function syncTime(value?: string | number | null) { if (!value) return ''; const date = new Date(typeof value === 'number' ? value * 1000 : value); return Number.isNaN(date.getTime()) ? '' : date.toLocaleString(); }

export default function LedgerSync({ onSynced }: { onSynced: () => void }) {
  const { connected, navigate, track } = useApp();
  const [status, setStatus] = useState<SyncStatus>();
  const [failure, setFailure] = useState('');
  const [busy, setBusy] = useState(false);
  const [conflicts, setConflicts] = useState<Conflict[]>();
  const [choices, setChoices] = useState<Record<string, unknown>>({});
  const [projectNames, setProjectNames] = useState<Record<string, string>>({});
  const callback = useRef(onSynced); callback.current = onSynced;
  const last = useRef<string | number | null | undefined>(undefined);
  const receivedStatus = useRef(false);
  const refresh = useCallback(async () => {
    const value = await rpc<SyncStatus>('expenses.sync.status'); setStatus(value); setFailure('');
    if (receivedStatus.current && last.current !== value.last_sync) callback.current();
    last.current = value.last_sync; receivedStatus.current = true;
  }, []);
  useEffect(() => {
    if (!connected) return;
    let alive = true;
    const read = () => void refresh().catch(reason => { if (alive) setFailure(errorText(reason)); });
    read(); const timer = setInterval(read, 5000);
    return () => { alive = false; clearInterval(timer); };
  }, [connected, refresh]);
  const sync = async () => {
    setBusy(true); setFailure('');
    try { track(await rpc<Job>('expenses.sync'), '正在合并同步记账与网络诊断配置。'); await refresh(); }
    catch (reason) { setFailure(errorText(reason)); }
    finally { setBusy(false); }
  };
  const review = async () => {
    setBusy(true); setFailure('');
    try { const result = await rpc<{ items: Conflict[] }>('expenses.conflicts'); setConflicts(result.items); setChoices({}); const projects = await rpc<{ items: { id: string; name: string }[] }>('expenses.projects.list'); setProjectNames(Object.fromEntries(projects.items.map(project => [project.id, project.name]))); }
    catch (reason) { setFailure(errorText(reason)); }
    finally { setBusy(false); }
  };
  const conflict = conflicts?.[0];
  const migrationMessage = ledgerMigrationMessage(status);
  const candidateLabel = (field: string, value: unknown) => {
    if (field === 'project_id' && typeof value === 'string') return projectNames[value] || value;
    if (field === 'archived') return value ? '已归档' : '未归档';
    if (field.startsWith('attachment:')) return value && typeof value === 'object' && 'original_name' in value ? String(value.original_name) : value === null ? '移除附件' : show(value);
    return ['status', 'entry_type', 'settlement_mode'].includes(field) && typeof value === 'string' ? valueNames[value] || value : show(value);
  };
  const resolve = async () => {
    if (!conflict) return;
    setBusy(true); setFailure('');
    try {
      await rpc('expenses.resolve', { entity_type: conflict.entity_type, id: conflict.id, heads: conflict.heads, changes: choices });
      const next = await rpc<{ items: Conflict[] }>('expenses.conflicts'); setConflicts(next.items.length ? next.items : undefined); setChoices({}); await refresh(); callback.current();
    } catch (reason) { setFailure(errorText(reason)); }
    finally { setBusy(false); }
  };
  return <>
    <div className="ledger-sync" aria-live="polite"><span>{!status ? '正在读取同步状态…' : !status.configured ? '记账尚未连接 WebDAV' : status.syncing ? '正在合并同步…' : status.error ? '同步失败，本机记录已保留' : migrationMessage ? migrationMessage : status.incomplete ? `等待补齐 ${status.incomplete} 项同步历史` : status.pending ? `待同步 ${status.pending} 项` : status.last_sync ? `已同步 · ${syncTime(status.last_sync)}` : '等待首次同步'}</span><span className="flex-spacer" />{!!status?.conflicts && <Button disabled={busy} onClick={review}>处理冲突 · {status.conflicts}</Button>}{status?.configured ? <Button variant="ghost" disabled={!connected || busy || status.syncing} onClick={sync}><RefreshCw size={14} />立即同步</Button> : <Button variant="ghost" onClick={() => { sessionStorage.setItem('wintoolbox-settings-tab', 'data'); navigate('settings'); }}>配置 WebDAV</Button>}</div>
    {migrationMessage && <Notice tone="warning">{migrationMessage}。旧目录中尚未同步到本机的记录可能暂未显示，连接恢复后会重试。{status?.migration_error && <div>{status.migration_error}</div>}</Notice>}
    {(failure || status?.error) && !conflicts && <Notice tone="warning">{failure || status?.error}</Notice>}
    {conflicts && <Modal title={`同步冲突${conflicts.length ? ` · 剩余 ${conflicts.length} 项` : ''}`} onClose={() => { if (!busy) setConflicts(undefined); }}><div className="modal-body"><p className="small-note">不同字段会自动合并。同一字段有不同修改时，选择需要保留的内容；其他候选仍留在同步历史中。</p>{conflict ? <><p>{conflict.entity_type === 'project' ? '项目' : conflict.entity_type === 'network_profile' ? '网络诊断配置' : '记账条目'} · {conflict.title || conflict.name || conflict.id.slice(0, 8)}</p>{Object.entries(conflict.fields).map(([field, candidates]) => <Field key={field} label={labels[field] || (field.startsWith('attachment:') ? '附件' : field)}><select aria-label={`解决${labels[field] || field}冲突`} value={Object.hasOwn(choices, field) ? candidates.findIndex(candidate => JSON.stringify(candidate.value) === JSON.stringify(choices[field])) : ''} onChange={event => setChoices(value => ({ ...value, [field]: candidates[Number(event.target.value)].value }))}><option value="" disabled>选择保留的值</option>{candidates.map((candidate, index) => <option key={candidate.op_id} value={index}>{candidateLabel(field, candidate.value)}</option>)}</select></Field>)}</> : <p>没有待处理冲突。</p>}{failure && <Notice tone="warning">{failure}</Notice>}<div className="modal-footer"><Button disabled={busy} onClick={() => setConflicts(undefined)}>稍后处理</Button>{conflict && <Button variant="primary" busy={busy} disabled={Object.keys(choices).length !== Object.keys(conflict.fields).length} onClick={resolve}>保存选择</Button>}</div></div></Modal>}
  </>;
}
