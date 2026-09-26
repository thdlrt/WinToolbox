import { useEffect, useState } from 'react';
import { ExternalLink, Sparkles } from 'lucide-react';
import { rpc, type Job } from '../api';
import { useApp } from '../context';
import { activeJob, Button, Notice, Section, dateText, statusLabel } from '../ui';
import '../systemMemory.css';
import MemoryRocket from '../MemoryRocket';

interface Memory { percent: number; total: number; available: number; used: number; commit_total: number; commit_used: number }
interface Preferences { mode: 'default' | 'full' }
interface MemoryJob extends Job { result?: { available_change?: number; opened?: boolean } }
const gb = (n = 0) => `${(n / 1024 ** 3).toFixed(1)} GB`;

export default function SystemMemoryPage() {
  const { connected, jobs, run, track, error } = useApp();
  const [memory, setMemory] = useState<Memory>();
  const [config, setConfig] = useState<Preferences>({ mode: 'default' });
  const [saved, setSaved] = useState<Preferences>();
  const [busy, setBusy] = useState(false);
  const [helper, setHelper] = useState<{ installed: boolean; present?: boolean }>();
  const recent = jobs.filter(j => j.tool === 'memory.clean' || j.tool === 'memory.configure' || j.tool.startsWith('memory.helper.')) as MemoryJob[];
  const running = recent.some(j => activeJob(j.status));
  useEffect(() => { if (connected && !running) void rpc<{ installed: boolean }>('memory.helper.status').then(setHelper).catch(error); }, [connected, running, error]);
  useEffect(() => {
    if (!connected) return;
    let disposed = false;
    const read = () => void rpc<Memory>('memory.status').then(v => { if (!disposed) setMemory(v); }).catch(error);
    read();
    void rpc<Preferences>('memory.settings.get').then(v => { if (!disposed) { setConfig(v); setSaved(v); } }).catch(error);
    const timer = setInterval(read, 2000);
    return () => { disposed = true; clearInterval(timer); };
  }, [connected, error]);
  const save = async () => {
    setBusy(true);
    try { const value = await run(() => rpc<Preferences>('memory.settings.save', { ...config }), '清理设置已保存'); if (value) setSaved(value); }
    finally { setBusy(false); }
  };
  const launch = async (method: string) => {
    setBusy(true);
    try { const value = await run(() => rpc<MemoryJob>(method)); if (value) track(value, method === 'memory.clean' ? '正在请求内存清理' : method.startsWith('memory.helper.') ? '正在配置清理组件' : '正在打开 Mem Reduct'); }
    finally { setBusy(false); }
  };
  const dirty = config.mode !== saved?.mode;
  return <>
    <Section title="内存状态" action={<Button variant="primary" disabled={!saved || dirty || busy || running} onClick={() => void launch('memory.clean')}><Sparkles size={16}/>立即清理</Button>}>
      <div className="ram-stats">
        <div className="ram-primary"><strong>{memory?.percent ?? '—'}<small>%</small></strong><span>物理内存使用率</span><div className="ram-meter"><i style={{ width: `${memory?.percent || 0}%` }}/></div></div>
        <div><span>已使用</span><strong>{memory ? gb(memory.used) : '—'}</strong><small>总计 {memory ? gb(memory.total) : '—'}</small></div>
        <div><span>可用内存</span><strong>{memory ? gb(memory.available) : '—'}</strong><small>每 2 秒更新</small></div>
        <div><span>系统提交量</span><strong>{memory ? gb(memory.commit_used) : '—'}</strong><small>上限 {memory ? gb(memory.commit_total) : '—'}</small></div>
      </div>
    </Section>
    <Section title="一键清理设置">
      <div className="ram-modes">
        <label className={config.mode === 'default' ? 'selected' : ''}><input type="radio" name="ram-mode" checked={config.mode === 'default'} onChange={() => setConfig({ mode: 'default' })}/><span><strong>常规清理</strong><small>清理进程工作集与低优先级备用页，适合日常使用。</small></span></label>
        <label className={config.mode === 'full' ? 'selected' : ''}><input type="radio" name="ram-mode" checked={config.mode === 'full'} onChange={() => setConfig({ mode: 'full' })}/><span><strong>深度清理</strong><small>清理进程工作集、修改页及全部备用页，可能短暂卡顿。</small></span></label>
      </div>
      <div className="ram-setting-footer"><span className="muted">保存后同时应用到悬浮球的一键清理。</span><Button disabled={!saved || !dirty || busy || running} onClick={() => void save()}>保存设置</Button></div>
    </Section>
    <Section title="免重复授权">
      <p className="muted">{helper?.installed ? '已启用。工具箱退出、更新或电脑重启后仍可直接清理，不再反复弹出管理员授权。' : helper?.present ? '组件已安装但不可用，请修复；不会自动重复申请权限。' : '首次安装固定用途清理组件需要一次 Windows 授权，之后日常清理无需重复授权。点击立即清理也会自动完成首次安装。'}</p>
      <div className="button-row"><Button disabled={busy || running || !connected} onClick={() => void launch('memory.helper.install')}>{helper?.present ? '修复清理组件' : '启用免重复授权'}</Button>{helper?.present && <Button disabled={busy || running} onClick={() => { if (window.confirm('移除免重复授权组件？以后首次清理需要重新授权安装。')) void launch('memory.helper.remove'); }}>移除组件</Button>}</div>
    </Section>
    <Section title="自动清理与高级设置" action={<Button disabled={busy || running} onClick={() => void launch('memory.settings.open')}><ExternalLink size={16}/>打开 Mem Reduct 设置</Button>}>
      <p className="muted">在原版窗口的“文件 → 设置”中配置内存区域、占用阈值、定时间隔、快捷键和结果通知。启用自动清理后，需保持 Mem Reduct 在托盘运行。</p>
      <p className="muted">原版的区域与自动清理设置由 Mem Reduct 管理；上方模式用于工具箱及悬浮球的手动清理。打开原版设置窗口仍可能需要单独授权。</p>
    </Section>
    {recent[0]?.tool === 'memory.clean' && activeJob(recent[0].status) && <MemoryRocket/>}
    {recent[0] && activeJob(recent[0].status) && <Notice>{recent[0].message || '正在处理…'}</Notice>}
    <Section title="最近操作"><div className="ram-history">{recent.length ? recent.slice(0, 8).map(j => <div key={j.id}><span>{dateText(j.created_at)}</span><span>{j.tool === 'memory.configure' ? '打开设置' : j.tool === 'memory.helper.install' ? '启用或修复组件' : j.tool === 'memory.helper.remove' ? '移除组件' : '内存清理'}</span><span>{j.status === 'completed' && j.result?.available_change !== undefined ? `可用内存变化 ${j.result.available_change >= 0 ? '+' : ''}${(j.result.available_change / 1024 ** 2).toFixed(0)} MB` : j.status === 'failed' ? String(j.error || j.message) : statusLabel(j.status)}</span></div>) : <span className="muted">暂无操作记录</span>}</div></Section>
  </>;
}
