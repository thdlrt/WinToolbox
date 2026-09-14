import { useCallback, useEffect, useState } from 'react';
import { BarChart3, Check, Download, RefreshCw } from 'lucide-react';
import { rpc, type Job, type Settings } from '../api';
import { useApp } from '../context';
import { activeJob, Button, cx, Field, IconButton, Section } from '../ui';
import ToolJobs from '../ToolJobs';
import { presetNames } from '../modelConfig';

interface Preset {
  id: string; name: string; description: string; hardware_hint: string;
  asr_engine: string; asr_model: string; llm_model: string; model_ids: string[]; installed: boolean;
}
interface Setup {
  mode: 'bailian' | 'local'; preset: string; region: string; has_key: boolean; configured: boolean;
  presets: Preset[]; recommended_preset: string; provider_id: string;
}
interface UsagePeriod { requests: number; successful: number; failed: number; cancelled: number; input_tokens: number; output_tokens: number; total_tokens: number; audio_seconds: number; token_reported_requests: number; average_latency_ms: number }
interface UsageSummary { source: 'local'; started_at?: number; periods: { today: UsagePeriod; month: UsagePeriod; all: UsagePeriod }; by_model: { model: string; operation: string; requests: number; total_tokens: number; audio_seconds: number }[] }

const count = (value: number) => value >= 1_000_000 ? `${(value / 1_000_000).toFixed(1)}M` : value >= 1_000 ? `${(value / 1_000).toFixed(1)}K` : String(value);
const periodText = (value: UsagePeriod) => {
  const parts = [`${value.requests} 次`];
  if (value.token_reported_requests) parts.push(`${count(value.total_tokens)} Token`);
  if (value.audio_seconds) parts.push(`${(value.audio_seconds / 60).toFixed(value.audio_seconds < 600 ? 1 : 0)} 分钟语音`);
  if (value.failed) parts.push(`${value.failed} 次失败`);
  return parts.join(' · ');
};

export default function ModelSetup() {
  const { connected, settings, jobs, run, refreshSettings, track } = useApp();
  const [setup, setSetup] = useState<Setup>();
  const [mode, setMode] = useState<'bailian' | 'local'>('bailian');
  const [preset, setPreset] = useState('light');
  const [region, setRegion] = useState('cn');
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState('');
  const [usage, setUsage] = useState<UsageSummary>();
  const load = useCallback(async () => {
    const result = await run(() => rpc<Setup>('setup.get'));
    if (result) setSetup(result);
    return result;
  }, [run]);
  useEffect(() => {
    if (!connected) return;
    let alive = true;
    void load().then(result => {
      if (result && alive) { setMode(result.mode); setPreset(result.preset || result.recommended_preset || 'light'); setRegion(result.region || 'cn'); }
    });
    return () => { alive = false; };
  }, [connected, settings, load]);
  const pendingInstalls = jobs.filter(job => activeJob(job.status) && (job.tool === 'setup.install' || job.tool === 'models.install' || job.tool === 'model-install'));
  const installationState = pendingInstalls.map(job => `${job.id}:${job.status}`).join(',');
  useEffect(() => { if (connected) void load(); }, [connected, installationState, load]);
  const selected = setup?.presets.find(item => item.id === preset);
  const loadUsage = useCallback(async () => {
    if (!setup?.provider_id) return;
    setBusy(old => old || 'usage');
    const result = await run(() => rpc<UsageSummary>('providers.usage', { provider_id: setup.provider_id }));
    if (result) setUsage(result);
    setBusy(old => old === 'usage' ? '' : old);
  }, [run, setup?.provider_id]);
  useEffect(() => { if (connected && mode === 'bailian' && setup?.provider_id) void loadUsage(); }, [connected, mode, setup?.provider_id, loadUsage]);
  const installing = pendingInstalls.some(job => job.params?.preset === preset || selected?.model_ids.includes(String(job.params?.model_id || '')));
  const applied = !!setup?.configured && mode === setup.mode && (mode !== 'local' || preset === setup.preset);
  const apply = async () => {
    setBusy('apply');
    const result = await run(() => rpc<Settings>('setup.apply', { mode, ...(mode === 'local' ? { preset } : { region, ...(key.trim() ? { api_key: key.trim() } : {}) }) }), '模型设置已应用。');
    if (result) { setKey(''); await run(refreshSettings); }
    setBusy('');
  };
  const install = async () => {
    setBusy('install');
    const job = await run(() => rpc<Job>('setup.install', { preset }));
    if (job) { track(job, '正在安装本地预设。'); }
    setBusy('');
  };

  return <Section className="model-setup">
    <div className="setup-mode" role="tablist" aria-label="模型模式">
      <button role="tab" aria-selected={mode === 'bailian'} className={cx(mode === 'bailian' && 'selected')} onClick={() => setMode('bailian')}>阿里云百炼 API</button>
      <button role="tab" aria-selected={mode === 'local'} className={cx(mode === 'local' && 'selected')} onClick={() => setMode('local')}>本地模式</button>
    </div>
    {mode === 'bailian' ? <div className="setup-api">
      <Field label="API Key"><input type="password" autoComplete="new-password" spellCheck={false} value={key} onChange={e => setKey(e.target.value)} placeholder={setup?.has_key ? '已保存密钥，留空保留' : '粘贴百炼 API Key'} /></Field>
      <details className="details setup-region"><summary>地域 · {region === 'intl' ? '国际站' : '中国内地'}</summary><Field label="服务地域"><select value={region} onChange={e => setRegion(e.target.value)}><option value="cn">中国内地</option><option value="intl">国际站</option></select></Field></details>
      <div className="usage-card"><div className="usage-heading"><span><BarChart3 size={15} />本机 API 用量</span><IconButton label="刷新用量" disabled={!connected || !setup?.provider_id || busy === 'usage'} onClick={() => void loadUsage()}><RefreshCw size={14} className={cx(busy === 'usage' && 'spin')} /></IconButton></div><div className="usage-periods"><div><span>今日</span><strong>{usage ? periodText(usage.periods.today) : '正在读取…'}</strong></div><div><span>本月</span><strong>{usage ? periodText(usage.periods.month) : '正在读取…'}</strong></div><div><span>累计</span><strong>{usage ? periodText(usage.periods.all) : '正在读取…'}</strong></div></div>{usage?.by_model.length ? <details className="details usage-models"><summary>本月按模型</summary>{usage.by_model.map(item => <div key={`${item.model}-${item.operation}`}><span>{item.model}</span><small>{item.requests} 次{item.total_tokens ? ` · ${count(item.total_tokens)} Token` : ''}{item.audio_seconds ? ` · ${(item.audio_seconds / 60).toFixed(1)} 分钟` : ''}</small></div>)}</details> : null}<p>从本版本开始记录本机请求；Token 以接口返回为准，实时语音按发送时长统计。此处不是阿里云账户账单或余额。</p></div>
      <div className="setup-footer"><span className="muted">转写、翻译与问答自动配置</span><Button variant="primary" busy={busy === 'apply'} disabled={!connected || !setup || (!key.trim() && !setup.has_key)} onClick={apply}>{applied && setup?.has_key && !key && region === setup.region ? '重新应用' : '应用'}</Button></div>
    </div> : <div>
      <div className="preset-list" role="radiogroup" aria-label="本地预设">{setup?.presets.map(item => <button key={item.id} role="radio" aria-checked={preset === item.id} className={cx('preset-option', preset === item.id && 'selected')} onClick={() => setPreset(item.id)}><span className="preset-radio">{preset === item.id && <Check size={12} />}</span><span className="preset-copy"><strong>{presetNames[item.id] || item.name}{setup.recommended_preset === item.id && <small>推荐</small>}</strong><span>{item.hardware_hint || item.description}</span></span><span className={cx('preset-status', item.installed && 'ready')}>{item.installed ? '已安装' : '待安装'}</span></button>)}</div>
      {!setup && <p className="small-note">{connected ? '正在读取本地预设…' : '等待服务连接'}</p>}
      <div className="setup-footer"><div className="button-row"><IconButton label="刷新安装状态" disabled={!connected} onClick={() => void load()}><RefreshCw size={15} /></IconButton><span className="muted">{selected?.description}</span></div>{selected?.installed ? <Button variant="primary" busy={busy === 'apply'} disabled={!connected || applied || installing} onClick={apply}>{applied ? '使用中' : '使用预设'}</Button> : <Button variant="primary" busy={busy === 'install'} disabled={!connected || !selected || installing} onClick={install}><Download size={15} />{installing ? '安装中' : '安装预设'}</Button>}</div>
    </div>}
    <ToolJobs scope="presets" title="安装进度" />
  </Section>;
}
