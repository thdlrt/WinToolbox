import { visiblePolling } from '../visiblePolling';
import { useEffect, useState } from 'react';
import { RefreshCw, Sparkles } from 'lucide-react';
import { errorText, rpc, type Job } from '../api';
import { useApp } from '../context';
import { activeJob, Button, CheckField, Field, Notice, Section, dateText, statusLabel } from '../ui';
import { memoryDraft, memorySettingsDirty, memorySettingsParams, memorySettingsValidation, type MemoryAutomation, type MemoryDraft, type MemoryMode, type MemoryPreferences, type MemorySettings } from '../systemMemoryState';
import '../systemMemory.css';
import MemoryRocket from '../MemoryRocket';

interface Memory { percent: number; total: number; available: number; used: number; commit_total: number; commit_used: number }
interface Helper { installed: boolean; present?: boolean }
interface MemoryJob extends Job { result?: { available_change?: number } }
const gb = (n = 0) => `${(n / 1024 ** 3).toFixed(1)} GB`;
const triggerName = (value?: unknown) => value === 'threshold' ? '占用阈值' : value === 'interval' ? '定时' : '手动';

export default function SystemMemoryPage() {
  const { connected, jobs, run, track } = useApp();
  const [memory, setMemory] = useState<Memory>();
  const [config, setConfig] = useState<MemoryDraft>(() => memoryDraft());
  const [saved, setSaved] = useState<MemoryPreferences>();
  const [automation, setAutomation] = useState<MemoryAutomation>();
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState('');
  const [readFailure, setReadFailure] = useState('');
  const [helperFailure, setHelperFailure] = useState('');
  const [helper, setHelper] = useState<Helper>();
  const recent = jobs.filter(j => j.tool === 'memory.clean' || j.tool.startsWith('memory.helper.')) as MemoryJob[];
  const running = recent.some(j => activeJob(j.status));
  const refreshHelper = async () => {
    setHelperFailure('');
    try { setHelper(await rpc<Helper>('memory.helper.status')); }
    catch (reason) { setHelperFailure(errorText(reason)); }
  };
  useEffect(() => { if (connected && !running) void refreshHelper(); }, [connected, running]);
  useEffect(() => {
    if (!connected) return;
    let disposed = false;
    let first = true;
    const read = async () => {
      const results = await Promise.allSettled([rpc<Memory>('memory.status'), rpc<MemorySettings>('memory.settings.get')]);
      if (disposed) return;
      if (results[0].status === 'fulfilled') setMemory(results[0].value);
      if (results[1].status === 'fulfilled') {
        const value = results[1].value; setAutomation(value.automation);
        if (first) { setConfig(memoryDraft(value)); setSaved(value); first = false; }
      }
      const failed = results.find(value => value.status === 'rejected');
      setReadFailure(failed?.status === 'rejected' ? errorText(failed.reason) : '');
    };
    const stopPolling = visiblePolling(read, 2000);
    return () => { disposed = true; stopPolling(); };
  }, [connected]);
  const save = async () => {
    const invalid = memorySettingsValidation(config); if (invalid) { setFailure(invalid); return; }
    setBusy(true); setFailure('');
    try {
      const value = await rpc<MemorySettings>('memory.settings.save', { ...memorySettingsParams(config) });
      setSaved(value); setConfig(memoryDraft(value)); setAutomation(value.automation);
    } catch (reason) { setFailure(errorText(reason)); }
    finally { setBusy(false); }
  };
  const launch = async (method: 'memory.clean' | 'memory.helper.install' | 'memory.helper.remove') => {
    setBusy(true);
    try { const value = await run(() => rpc<MemoryJob>(method)); if (value) track(value, method === 'memory.clean' ? '正在请求内存清理' : '正在配置内存清理授权组件'); }
    finally { setBusy(false); }
  };
  const change = (patch: Partial<MemoryDraft>) => { setConfig(value => ({ ...value, ...patch })); setFailure(''); };
  const dirty = memorySettingsDirty(config, saved);
  const autoState = !saved?.auto_enabled ? '自动清理未开启' : automation?.running ? '正在清理…' : automation?.paused_reason ? `已暂停：${automation.paused_reason}` : helperFailure ? '等待确认授权组件状态' : helper && !helper.installed ? '等待启用免重复授权' : '自动清理已开启';
  return <>
    <Section title="内存状态" action={<Button variant="primary" disabled={!connected || !saved || dirty || busy || running} onClick={() => void launch('memory.clean')}><Sparkles size={16}/>立即清理</Button>}>
      <div className="ram-stats">
        <div className="ram-primary"><strong>{memory?.percent ?? '—'}<small>%</small></strong><span>物理内存使用率</span><div className="ram-meter"><i style={{ width: `${memory?.percent || 0}%` }}/></div></div>
        <div><span>已使用</span><strong>{memory ? gb(memory.used) : '—'}</strong><small>总计 {memory ? gb(memory.total) : '—'}</small></div>
        <div><span>可用内存</span><strong>{memory ? gb(memory.available) : '—'}</strong><small>每 2 秒更新</small></div>
        <div><span>系统提交量</span><strong>{memory ? gb(memory.commit_used) : '—'}</strong><small>上限 {memory ? gb(memory.commit_total) : '—'}</small></div>
      </div>{readFailure && <Notice tone="warning">读取内存状态或清理设置失败：{readFailure}</Notice>}
    </Section>
    <Section title="清理设置">
      <fieldset className="plain-fieldset" disabled={!connected || !saved || busy}>
        <div className="ram-modes">
          <label className={config.mode === 'default' ? 'selected' : ''}><input type="radio" name="ram-mode" checked={config.mode === 'default'} onChange={() => change({ mode: 'default' })}/><span><strong>常规清理</strong><small>清理进程工作集与低优先级备用页，适合日常使用。</small></span></label>
          <label className={config.mode === 'full' ? 'selected' : ''}><input type="radio" name="ram-mode" checked={config.mode === 'full'} onChange={() => change({ mode: 'full' })}/><span><strong>深度清理</strong><small>清理进程工作集、修改页及全部备用页，可能短暂卡顿。</small></span></label>
        </div>
        <p className="small-note">此模式用于立即清理及悬浮球的一键清理。</p>
        <div className="section-divider" />
        <CheckField checked={config.auto_enabled} onChange={value => change({ auto_enabled: value })} label="自动清理" hint="仅在工具箱运行期间生效。首次需启用下方免重复授权组件，自动清理不会弹出授权窗口。" />
        <div className="ram-auto-triggers">
          <div><CheckField checked={config.threshold_enabled} disabled={!config.auto_enabled} onChange={value => change({ threshold_enabled: value })} label="按内存使用率" /><Field label="使用率达到（%）"><input type="number" min={50} max={99} step={1} disabled={!config.auto_enabled || !config.threshold_enabled} value={config.threshold_percent} onChange={event => change({ threshold_percent: event.target.value })}/></Field></div>
          <div><CheckField checked={config.interval_enabled} disabled={!config.auto_enabled} onChange={value => change({ interval_enabled: value })} label="定时清理" /><Field label="每隔（分钟）"><input type="number" min={1} max={1440} step={1} disabled={!config.auto_enabled || !config.interval_enabled} value={config.interval_minutes} onChange={event => change({ interval_minutes: event.target.value })}/></Field></div>
        </div>
        <details className="ram-advanced"><summary>高级设置</summary><div className="form-grid"><Field label="自动清理模式"><select disabled={!config.auto_enabled} value={config.auto_mode} onChange={event => change({ auto_mode: event.target.value as MemoryMode })}><option value="default">常规清理</option><option value="full">深度清理</option></select></Field><Field label="两次自动清理至少间隔（分钟）"><input type="number" min={1} max={1440} step={1} disabled={!config.auto_enabled} value={config.cooldown_minutes} onChange={event => change({ cooldown_minutes: event.target.value })}/></Field></div><p className="small-note">占用阈值触发后，使用率需要先降低 5 个百分点才会再次触发，避免持续重复清理。定时清理也遵守最短间隔。</p></details>
      </fieldset>
      {failure && <Notice tone="warning">{failure}</Notice>}
      <div className="ram-setting-footer"><span className="muted">{dirty ? '设置尚未保存' : '设置已保存'}</span><Button disabled={!connected || !saved || !dirty} busy={busy} onClick={() => void save()}>保存设置</Button></div>
      <div className="ram-auto-status" aria-live="polite"><strong>{autoState}</strong>{saved?.auto_enabled && !!automation?.next_interval_at && <span>下次定时：{dateText(automation.next_interval_at)}</span>}{!!automation?.last_run && <span>最近清理：{dateText(automation.last_run)} · {triggerName(automation.last_trigger)}</span>}</div>
      {automation?.last_error && <Notice tone="warning">最近清理未完成：{automation.last_error}</Notice>}
    </Section>
    <Section title="免重复授权" action={<Button disabled={!connected || busy || running} onClick={() => void refreshHelper()}><RefreshCw size={14}/>重新检测</Button>}>
      <p className="muted">{helperFailure ? '暂时无法确认授权组件状态，详细原因见下方。' : !helper ? '正在读取组件状态…' : helper.installed ? '已启用，手动和自动清理无需重复请求管理员授权。' : helper.present ? '组件已安装但未通过检查，可重新配置后重试。' : '首次启用需要一次 Windows 授权；也可通过立即清理完成首次安装。'}</p>
      {helperFailure && <Notice tone="warning">{helperFailure}</Notice>}
      <div className="button-row"><Button disabled={busy || running || !connected} onClick={() => void launch('memory.helper.install')}>{helper?.present ? '重新配置组件' : '启用免重复授权'}</Button>{helper?.present && <Button disabled={!connected || busy || running} onClick={() => { if (window.confirm('移除免重复授权组件？自动清理将暂停；以后首次手动清理需要重新授权安装。')) void launch('memory.helper.remove'); }}>移除组件</Button>}</div>
    </Section>
    {recent[0]?.tool === 'memory.clean' && activeJob(recent[0].status) && <MemoryRocket/>}
    {recent[0] && activeJob(recent[0].status) && <Notice>{recent[0].message || '正在处理…'}</Notice>}
    <Section title="最近操作"><div className="ram-history">{recent.length ? recent.slice(0, 8).map(j => <div key={j.id}><span>{dateText(j.created_at)}</span><span>{j.tool === 'memory.helper.install' ? '配置授权组件' : j.tool === 'memory.helper.remove' ? '移除组件' : j.params.trigger ? `自动清理 · ${triggerName(j.params.trigger)}` : '手动清理'}</span><span>{j.status === 'completed' && j.result?.available_change !== undefined ? `可用内存变化 ${j.result.available_change >= 0 ? '+' : ''}${(j.result.available_change / 1024 ** 2).toFixed(0)} MB` : j.status === 'failed' ? errorText(j.error || j.message) : statusLabel(j.status)}</span></div>) : <span className="muted">暂无操作记录</span>}</div></Section>
  </>;
}
