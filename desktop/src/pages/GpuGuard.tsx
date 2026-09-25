import { useCallback, useEffect, useRef, useState } from 'react';
import { Battery, Plus, RefreshCw, Trash2 } from 'lucide-react';
import { errorText, native, rpc, type Job } from '../api';
import { useApp } from '../context';
import { activeJob, basename, Button, CheckField, Field, Notice, Section } from '../ui';
import '../gpuGuard.css';

interface Power { source: 'battery' | 'ac' | 'unknown'; has_battery: boolean; percent: number | null; discharge_w?: number | null; remaining_seconds?: number | null; remaining_wh?: number | null; runtime_source?: string }
interface Process { pid: number; gpu: string; luid?: string; name: string; path: string; usage: number; memory_mb: number }
interface Snapshot { sampled_at: number; processes: Process[]; adapters: { name: string; vendor: number }[] }
interface GuardState { enabled: boolean; active: boolean; pending_restore: number; apps: string[]; power: Power; sample?: Snapshot; error: string; notice: string; software_rendering: boolean }
const number = (value: number | null | undefined, unit: string) => value == null ? '未提供' : `${value.toFixed(1)} ${unit}`;
const duration = (value: number | null | undefined) => value == null ? '未提供' : `${Math.floor(value / 3600)} 小时 ${Math.floor(value % 3600 / 60)} 分`;
const time = (value?: number) => value ? new Date(value * 1000).toLocaleTimeString('zh-CN') : '尚未采样';

export default function GpuGuardPage() {
  const { connected, jobs, track } = useApp();
  const [state, setState] = useState<GuardState>();
  const [failure, setFailure] = useState('');
  const [busy, setBusy] = useState('');
  const [path, setPath] = useState('');
  const [renderNotice, setRenderNotice] = useState('');
  const alive = useRef(true);
  const reading = useRef(false);
  const mutation = useRef(false);
  const revision = useRef(0);
  const handled = useRef(new Set<string>());
  const submitted = useRef(new Set<string>());
  const relevant = jobs.filter(job => job.tool.startsWith('gpu_guard.')) as (Job & { result?: unknown })[];
  const running = relevant.find(job => activeJob(job.status));
  const read = useCallback(async () => {
    if (reading.current || mutation.current) return;
    reading.current = true;
    const version = revision.current;
    try {
      const result = await rpc<GuardState>('gpu_guard.status');
      if (alive.current && version === revision.current) { setState(result); setFailure(''); }
    } catch (reason) { if (alive.current && version === revision.current) setFailure(errorText(reason)); }
    finally { reading.current = false; }
  }, []);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (!connected) return;
    void read();
    const timer = setInterval(() => { if (!document.hidden) void read(); }, 15000);
    return () => clearInterval(timer);
  }, [connected, read]);
  useEffect(() => {
    for (const job of relevant) {
      if (!submitted.current.has(job.id) || handled.current.has(job.id) || activeJob(job.status)) continue;
      handled.current.add(job.id);
      if (job.status === 'completed') {
        void read();
      } else setFailure(errorText(job.error || job.message || '读取未完成'));
    }
  }, [relevant, read]);
  const change = async (method: string, params: Record<string, unknown>) => {
    if (mutation.current) return;
    mutation.current = true; revision.current++; setBusy(method); setFailure('');
    try {
      const result = await rpc<GuardState>(method, params);
      if (alive.current) { setState(result); if (method === 'gpu_guard.apps') setPath(''); }
    } catch (reason) { if (alive.current) setFailure(errorText(reason)); }
    finally { mutation.current = false; if (alive.current) setBusy(''); }
  };
  const start = async (method: string) => {
    setBusy(method); setFailure('');
    try { const job = await rpc<Job>(method); submitted.current.add(job.id); track(job, '正在读取本机数据。'); }
    catch (reason) { if (alive.current) setFailure(errorText(reason)); }
    finally { if (alive.current) setBusy(''); }
  };
  const pick = async () => {
    try { const chosen = (await native.files(false, [{ name: '应用程序', extensions: ['exe'] }]))[0]; if (chosen) setPath(chosen); }
    catch (reason) { setFailure(errorText(reason)); }
  };
  const canEdit = connected && !!state && !state.enabled && !state.pending_restore && !busy;
  const source = state?.power.source === 'battery' ? '电池供电' : state?.power.source === 'ac' ? '接通电源' : '电源状态未知';
  return <div className="gpu-guard-page">
    {!connected && <Notice>请在 WinToolbox 桌面程序中使用本机独显守卫。</Notice>}
    {failure && <Notice tone="warning">{failure}</Notice>}
    {state?.error && <Notice tone="warning">{state.error}</Notice>}
    {state?.notice && <Notice>{state.notice}</Notice>}
    <Section>
      <div className="gpu-power-grid">
        <div><span><Battery size={15} />{source}</span><strong>{state?.power.percent == null ? '—' : `${state.power.percent}%`}</strong></div>
        <div><span>放电功率</span><strong>{number(state?.power.discharge_w, 'W')}</strong></div>
        <div title={state?.power.runtime_source === 'current_load' ? '按当前负载推算' : 'Windows 系统估计'}><span>预计续航</span><strong>{duration(state?.power.remaining_seconds)}</strong></div>
      </div>
    </Section>
    <Section title="独显占用" action={<Button disabled={!connected || !!busy || !!running} busy={!!running && running.tool === 'gpu_guard.scan'} onClick={() => void start('gpu_guard.scan')}><RefreshCw size={15} />检查独显</Button>}>
      {state?.sample ? <>
        {state.sample.processes.length ? <div className="gpu-table-wrap"><table className="gpu-table"><thead><tr><th>应用</th><th>利用率</th><th>显存</th><th /></tr></thead><tbody>{state.sample.processes.map((process, i) => <tr key={`${process.pid}-${process.luid}-${i}`}><td title={process.path || process.gpu}>{process.name}<small>PID {process.pid}</small></td><td>{process.usage.toFixed(2)}%</td><td>{number(process.memory_mb, 'MB')}</td><td><Button disabled={!canEdit || !process.path || state.apps.some(app => app.toLowerCase() === process.path.toLowerCase())} onClick={() => void change('gpu_guard.apps', { path: process.path })}>加入名单</Button></td></tr>)}</tbody></table></div> : <p className="small-note">未检测到占用</p>}
        <p className="small-note gpu-sample-time">更新于 {time(state.sample.sampled_at)}</p>
      </> : <p className="small-note">尚未检查</p>}
    </Section>
    <Section title="电池守卫" action={<Button variant={state?.enabled ? 'secondary' : 'primary'} disabled={!connected || !state || !!busy || (!state.enabled && !state.pending_restore && !state.power.has_battery)} busy={busy === 'gpu_guard.enable'} onClick={() => void change('gpu_guard.enable', { enabled: !(state?.enabled || state?.pending_restore) })}>{state?.enabled || state?.pending_restore ? '停用并恢复' : '启用守卫'}</Button>}>
      <p className="small-note gpu-guard-state">{state?.enabled ? state.active ? '已应用核显偏好' : '已启用 · 等待电池供电' : state && !state.power.has_battery ? '未检测到电池' : '拔电时优先核显，接电后恢复。'}</p>
      <Field label="应用名单" hint={state?.enabled ? '停用守卫后可编辑' : undefined}>
        <div className="input-action"><input aria-label="应用程序路径" value={path} disabled={!canEdit} onChange={e => setPath(e.target.value)} placeholder="选择应用（.exe）" /><Button disabled={!canEdit} onClick={() => void pick()}>浏览</Button><Button disabled={!canEdit || !path.trim()} onClick={() => void change('gpu_guard.apps', { path })}><Plus size={15} />添加</Button></div>
      </Field>
      <div className="gpu-app-list">{state?.apps.map(app => <div key={app}><span title={app}>{basename(app)}</span><Button variant="ghost" aria-label={`移除 ${basename(app)}`} disabled={!canEdit} onClick={() => void change('gpu_guard.apps', { path: app, remove: true })}><Trash2 size={15} /></Button></div>)}</div>
    </Section>
    <details className="gpu-help"><summary>设置与说明</summary>
      <CheckField checked={state?.software_rendering || false} disabled={!connected || !state || !!busy} label="工具箱使用软件渲染" hint="重启生效，可能增加 CPU 开销。" onChange={value => { void change('gpu_guard.rendering', { enabled: value }); setRenderNotice('设置成功后，从托盘退出并重新启动工具箱。'); }} />
      {renderNotice && <p className="small-note">{renderNotice}</p>}
      <p className="small-note">核显偏好需重启相关应用生效，不保证独显断电。停用或从托盘退出后恢复原设置。</p>
      <p className="small-note">电池模式每 30 秒检查占用，不查询独显功率。利用率与显存分配不能代表电源状态。</p>
      <p className="small-note">放电功率为整机电池读数；缺失数据显示“未提供”。续航随负载变化，CPU 独立功率暂不支持。</p>
    </details>
  </div>;
}
