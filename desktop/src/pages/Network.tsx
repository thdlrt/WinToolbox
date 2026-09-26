import { useCallback, useEffect, useRef, useState } from 'react';
import { Play, Plus, Save, Trash2 } from 'lucide-react';
import { errorText, native, rpc, type Job } from '../api';
import { useApp } from '../context';
import { activeJob, Button, Empty, Field, Modal, Notice, Section } from '../ui';
import { networkDraft, networkParams, networkValidation, newNetworkDraft, type NetworkDraft, type NetworkProfile, type NetworkReport } from '../networkState';
import LedgerSync from '../LedgerSync';
import ToolJobs from '../ToolJobs';
import '../expenses.css';
import '../network.css';

const stepNames = { dns: 'DNS 解析', tcp: 'TCP 连接', tls: 'TLS 握手', http: 'HTTP 请求' };
const stepStatus = { ok: '通过', error: '失败', skipped: '跳过' };
export default function NetworkPage() {
  const { connected, jobs, track } = useApp();
  const [profiles, setProfiles] = useState<NetworkProfile[]>([]);
  const [selected, setSelected] = useState('');
  const [editBase, setEditBase] = useState<NetworkProfile>();
  const [draft, setDraft] = useState<NetworkDraft>(newNetworkDraft);
  const [reports, setReports] = useState<NetworkReport[]>([]);
  const [reportId, setReportId] = useState('');
  const [busy, setBusy] = useState('');
  const [failure, setFailure] = useState('');
  const [deleting, setDeleting] = useState(false);
  const handled = useRef(new Set<string>());
  const alive = useRef(true);
  const report = reports.find(item => item.id === reportId) || reports[0];
  const profile = editBase;
  const running = jobs.some(job => job.tool === 'network.run' && activeJob(job.status));
  const load = useCallback(async () => {
    const results = await Promise.allSettled([rpc<{ profiles: NetworkProfile[] }>('network.profiles.list'), rpc<{ reports: NetworkReport[] }>('network.history')]);
    if (!alive.current) return;
    if (results[0].status === 'fulfilled') setProfiles(results[0].value.profiles);
    if (results[1].status === 'fulfilled') setReports(results[1].value.reports);
    const failed = results.find(value => value.status === 'rejected'); if (failed?.status === 'rejected') setFailure(errorText(failed.reason));
  }, []);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { if (connected) void load().catch(reason => setFailure(errorText(reason))); }, [connected, load]);
  useEffect(() => {
    for (const job of jobs) {
      if (job.tool !== 'network.run' || activeJob(job.status) || handled.current.has(job.id)) continue;
      handled.current.add(job.id); void load().catch(reason => setFailure(errorText(reason)));
    }
  }, [jobs, load]);
  const change = (patch: Partial<NetworkDraft>) => setDraft(value => ({ ...value, ...patch }));
  const choose = (id: string) => { setSelected(id); const item = profiles.find(value => value.id === id); setEditBase(item); setDraft(item ? networkDraft(item) : newNetworkDraft()); setFailure(''); };
  const execute = async () => {
    const invalid = networkValidation(draft); if (invalid) { setFailure(invalid); return; }
    setBusy('run'); setFailure(''); setReportId('');
    try { track(await rpc<Job>('network.run', networkParams(draft)), '正在分步诊断网络连接。'); }
    catch (reason) { setFailure(errorText(reason)); } finally { setBusy(''); }
  };
  const save = async () => {
    const invalid = networkValidation(draft); if (invalid || !draft.name.trim()) { setFailure(invalid || '请为配置填写名称。'); return; }
    setBusy('save'); setFailure('');
    try { const result = await rpc<NetworkProfile>('network.profiles.save', { ...networkParams(draft), name: draft.name.trim(), ...(profile ? { id: profile.id, revision: profile.revision, heads: profile.sync_heads } : {}) }); await load(); setSelected(result.id); setEditBase(result); }
    catch (reason) { setFailure(errorText(reason)); } finally { setBusy(''); }
  };
  const remove = async () => {
    if (!profile) return;
    setBusy('delete'); setFailure('');
    try { await rpc('network.profiles.delete', { id: profile.id, revision: profile.revision }); await load(); choose(''); setDeleting(false); }
    catch (reason) { setFailure(errorText(reason)); } finally { setBusy(''); }
  };
  const exportReport = async () => {
    if (!report) return;
    setBusy('export'); setFailure('');
    try { const path = await native.save(`网络诊断-${report.id.slice(0, 8)}.json`, [{ name: 'JSON', extensions: ['json'] }]); if (path) { const result = await rpc<{ path: string }>('network.export', { id: report.id, path }); await native.open(result.path); } }
    catch (reason) { setFailure(errorText(reason)); } finally { setBusy(''); }
  };
  return <div className="network-page">
    <LedgerSync onSynced={() => void load()} />
    <Section><div className="network-toolbar"><Field label="已保存配置"><select aria-label="已保存配置" value={selected} onChange={event => choose(event.target.value)}><option value="">临时诊断</option>{profiles.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field><Button onClick={() => choose('')} disabled={!!busy}><Plus size={14} />新建</Button></div>
      <fieldset className="plain-fieldset" disabled={!connected || !!busy}><div className="form-grid"><Field label="目标"><input value={draft.target} placeholder="example.com 或 https://example.com" onChange={event => change({ target: event.target.value })} /></Field><Field label="端口（留空自动选择）"><input inputMode="numeric" value={draft.port} placeholder="HTTP 80，其余 443" onChange={event => change({ port: event.target.value })} /></Field></div><div className="network-options"><Field label="配置名称"><input value={draft.name} maxLength={100} placeholder="保存配置时填写" onChange={event => change({ name: event.target.value })} /></Field><Field label="单次超时（毫秒）"><input type="number" min={1000} max={15000} step={500} value={draft.timeout_ms} onChange={event => change({ timeout_ms: event.target.value })} /></Field><Field label="TCP 测试次数"><input type="number" min={1} max={5} value={draft.attempts} onChange={event => change({ attempts: event.target.value })} /></Field></div></fieldset>
      <div className="network-actions"><Button variant="primary" busy={busy === 'run'} disabled={!connected || !!busy || running} onClick={execute}><Play size={14} />开始诊断</Button><Button busy={busy === 'save'} disabled={!connected || !!busy} onClick={save}><Save size={14} />保存配置</Button>{profile && <Button variant="ghost" disabled={!connected || !!busy} onClick={() => setDeleting(true)}><Trash2 size={14} />删除配置</Button>}</div><p className="small-note">配置通过统一 WebDAV 合并同步。诊断记录仅保留在本机；HTTP(S) 地址会额外检查 HTTP 和 TLS。</p>
    </Section>
    {failure && !deleting && <Notice tone="warning">{failure}</Notice>}
    <ToolJobs scope="network" title="诊断任务" />
    <Section><div className="network-toolbar"><Field label="诊断记录"><select aria-label="诊断记录" value={report?.id || ''} onChange={event => setReportId(event.target.value)}>{!reports.length && <option value="">暂无记录</option>}{reports.map(item => <option key={item.id} value={item.id}>{new Date(item.started_at).toLocaleString()} · {item.target}</option>)}</select></Field><Button disabled={!report || !!busy} busy={busy === 'export'} onClick={exportReport}>导出报告</Button></div>{report ? <><p className="network-summary">{report.summary}</p>{report.route && <p className="small-note">{report.route}</p>}<div className="network-steps">{report.steps.map(step => <article key={step.name} className={`network-step ${step.status}`}><div><strong>{stepNames[step.name]}</strong><span>{stepStatus[step.status]}</span><small>{step.duration_ms.toFixed(1)} ms</small></div><p>{step.detail}</p>{step.addresses?.length ? <p className="small-note">{step.addresses.join(' · ')}</p> : null}{step.samples_ms?.length ? <p className="small-note">连接耗时：{step.samples_ms.map(value => `${value.toFixed(1)} ms`).join(' / ')} · 成功 {step.success_count}/{step.attempt_count}</p> : null}</article>)}</div></> : <Empty title="运行诊断后查看各环节结果" />}</Section>
    {deleting && <Modal title="删除诊断配置？" onClose={() => { if (!busy) setDeleting(false); }}><div className="modal-body"><p>删除“{profile?.name}”后，此删除会同步到其他设备。已有诊断记录保留。</p>{failure && <Notice tone="warning">{failure}</Notice>}<div className="modal-footer"><Button disabled={!!busy} onClick={() => setDeleting(false)}>取消</Button><Button variant="danger" busy={busy === 'delete'} onClick={remove}>确认删除</Button></div></div></Modal>}
  </div>;
}
