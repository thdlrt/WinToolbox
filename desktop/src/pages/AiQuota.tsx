import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowUpRight, Check, CircleAlert, Clock3, KeyRound, Plus, RefreshCw, Search, Settings2, Trash2, Wallet } from 'lucide-react';
import { errorText, native, rpc, subscribe, type Job } from '../api';
import { useApp } from '../context';
import { activeJob, Button, cx, dateText, Field, IconButton, Modal, Notice } from '../ui';
import { visiblePolling } from '../visiblePolling';
import '../aiQuota.css';
import AiProxyPanel from '../AiProxyPanel';
import AiSyncConflicts from '../AiSyncConflicts';

interface Platform { id: string; name: string; kind: 'balance' | 'plan'; auth: 'key' | 'cookie'; console_url: string; hint: string; sources: { id: string; name: string }[] }
interface Meter { label: string; unit: string; used?: number; total?: number; remaining?: number; used_percent: number; remaining_percent: number; resets_at?: string }
interface Money { label?: string; value: number; currency: string; unit?: string }
interface Snapshot { fetched_at: number; balances: Money[]; meters: Meter[]; note?: string; plan?: string; expires_at?: string; spend?: Money; available?: boolean }
interface Account { id: string; provider: string; name: string; source_provider_id: string; status: string; error?: string; snapshot?: Snapshot; checked_at?: number }
interface Route { id: string; default_model: string; enabled: boolean; ready: boolean; error?: string; usage: { requests: number; total_tokens: number; token_reported_requests: number } }
interface Draft { id?: string; provider: string; name: string; credential: string; source_provider_id: string; api_key: string; model: string; enabled: boolean }
const defaults: Record<string, string> = { kimi_code: 'kimi-for-coding', qianwen: 'qwen3.8-flash', opencode_go: 'deepseek-v4.1-flash', glm: 'glm-5.3' };
const styles: Record<string, { mark: string; color: string }> = {
  deepseek: { mark: 'D', color: '#4c70ed' }, opencode_go: { mark: 'oc', color: '#8a6be5' },
  kimi_code: { mark: 'K', color: '#368776' },
  qianwen: { mark: 'Q', color: '#7661dc' }, aliyun_coding: { mark: '阿', color: '#ed8d46' },
  siliconflow: { mark: 'S', color: '#31a7a3' }, moonshot: { mark: 'M', color: '#56868c' }, openrouter: { mark: '↗', color: '#657fbe' },
  glm: { mark: 'Z', color: '#2f6bdd' },
};
const money = (m: Money) => m.unit && m.unit !== m.currency ? `${count(m.value)} ${m.unit}` : new Intl.NumberFormat('zh-CN', { style: 'currency', currency: m.currency, maximumFractionDigits: 2 }).format(m.value);
const count = (n: number) => new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(n);
function expired(account: Account) { const end = account.snapshot?.expires_at; return !!end && new Date(end).getTime() <= Date.now(); }
function low(account: Account) { return expired(account) || account.snapshot?.available === false || account.snapshot?.meters.some(m => m.remaining_percent <= 20) || account.snapshot?.balances.some(m => ['可用余额', 'Key 剩余额度'].includes(m.label || '') && m.value <= 0); }

function QuotaMeter({ item, stale }: { item: Meter; stale: boolean }) {
  const percent = Math.min(100, Math.max(0, item.remaining_percent));
  return <div className={cx('quota-meter', percent <= 20 && 'low')}>
    <div className="quota-meter-label"><span>{item.label}</span><span><b>{count(item.remaining_percent)}%</b> 剩余</span></div>
    <div className="quota-track" role="progressbar" aria-label={`${item.label}${stale ? '上次查询' : ''}剩余额度`} aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${percent}%` }} /></div>
    <div className="quota-meter-detail"><span>{item.used !== undefined && item.total !== undefined ? `已用 ${count(item.used)} / ${count(item.total)} ${item.unit}` : `已用 ${count(item.used_percent)}%`}</span><span>{item.resets_at ? `${dateText(item.resets_at)} 重置` : '重置时间未提供'}</span></div>
  </div>;
}

export default function AiQuotaPage() {
  const { connected, jobs, track, settings, refreshSettings, error: showError } = useApp();
  const [platforms, setPlatforms] = useState<Platform[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [routes, setRoutes] = useState<Route[]>([]);
  const [models, setModels] = useState<Record<string, string[]>>({});
  const [modelQuery, setModelQuery] = useState('');
  const [modelLoading, setModelLoading] = useState('');
  const [modelFeedback, setModelFeedback] = useState<Record<string, string>>({});
  const modelRequests = useRef(new Set<string>());
  const [draft, setDraft] = useState<Draft>();
  const [deleting, setDeleting] = useState<Account>();
  const [failure, setFailure] = useState('');
  const [saving, setSaving] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [filter, setFilter] = useState('all');
  const [query, setQuery] = useState('');
  const [interval, setIntervalValue] = useState(() => Number(localStorage.getItem('quota-refresh-minutes') || 0));
  const alive = useRef(true);
  const handled = useRef(new Set<string>());
  const running = submitting || jobs.some(j => j.tool === 'ai_quota.refresh' && activeJob(j.status));
  const runningRef = useRef(running); runningRef.current = running;
  const load = useCallback(async () => {
    const [catalogue, listing, calls] = await Promise.all([rpc<{ providers: Platform[] }>('ai_quota.catalogue'), rpc<{ accounts: Account[] }>('ai_quota.list'), rpc<{ routes: Route[] }>('ai_calls.list')]);
    if (alive.current) { setPlatforms(catalogue.providers); setAccounts(listing.accounts); setRoutes(calls.routes); }
  }, []);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { if (connected) void load().catch(e => setFailure(errorText(e))); }, [connected, load]);
  useEffect(() => {
    let disposed = false, off: (() => void) | undefined;
    void subscribe(event => { if (event.type === 'ai.config.changed') void load().catch(e => { if (!disposed) setFailure(errorText(e)); }); }).then(unlisten => { if (disposed) unlisten(); else off = unlisten; }).catch(e => { if (!disposed) setFailure(errorText(e)); });
    return () => { disposed = true; off?.(); };
  }, [load]);
  useEffect(() => {
    const saved = settings?.preferences.ai_quota_refresh_minutes;
    if (typeof saved === 'number') setIntervalValue([1, 5, 15].includes(saved) ? saved : 0);
  }, [settings]);
  const saveInterval = async (value: number) => {
    const previous = interval;
    setIntervalValue(value);
    try {
      await rpc('settings.update', { preferences: { ai_quota_refresh_minutes: value } });
      localStorage.setItem('quota-refresh-minutes', String(value));
      await refreshSettings();
    } catch (reason) { setIntervalValue(previous); setFailure(errorText(reason)); }
  };
  useEffect(() => {
    for (const job of jobs) if (job.tool === 'ai_quota.refresh' && !activeJob(job.status) && !handled.current.has(job.id)) {
      handled.current.add(job.id);
      void load().catch(e => setFailure(errorText(e)));
      if (job.status === 'failed') setFailure(errorText(job.error || job.message));
    }
  }, [jobs, load]);
  useEffect(() => {
    for (const job of jobs) if (job.tool === 'ai_calls.models' && modelRequests.current.has(job.id) && !activeJob(job.status) && !handled.current.has(job.id)) {
      handled.current.add(job.id);
      modelRequests.current.delete(job.id);
      const accountId = String(job.params?.account_id || '');
      setModelLoading(old => old === accountId ? '' : old);
      if (job.status === 'completed') {
        const data = (job as Job & { result?: { account_id: string; models: { id: string }[] } }).result;
        if (data) {
          const ids = data.models.map(m => m.id).filter(id => !id.startsWith('wan') && !['image', 'tts', 'asr', 'realtime', 'video'].some(t => id.includes(t)));
          setModels(old => ({ ...old, [data.account_id]: ids }));
          setModelFeedback(old => ({ ...old, [data.account_id]: ids.length ? `已读取 ${ids.length} 个对话模型，请在下方选择。` : '平台未返回可用的对话模型。' }));
        } else setModelFeedback(old => ({ ...old, [accountId]: '模型读取结果不完整，请重试。' }));
      } else setModelFeedback(old => ({ ...old, [accountId]: errorText(job.error || job.message) }));
    }
  }, [jobs]);
  const refresh = useCallback(async (ids?: string[], silent = false) => {
    if (runningRef.current) return;
    runningRef.current = true; setSubmitting(true); setFailure('');
    try { const job = await rpc<Job>('ai_quota.refresh', ids ? { ids } : {}); if (!silent) track(job, '正在查询账户额度。'); }
    catch (e) { setFailure(errorText(e)); }
    finally { if (alive.current) setSubmitting(false); }
  }, [track]);
  useEffect(() => {
    if (!connected || ![1, 5, 15].includes(interval) || !accounts.length) return;
    let first = true;
    return visiblePolling(async () => { if (first) { first = false; return; } await refresh(undefined, true); }, interval * 60000);
  }, [connected, interval, accounts.length, refresh]);
  const add = (p: Platform, account?: Account) => {
    const route = routes.find(r => r.id === account?.id);
    setFailure(''); setModelQuery(''); setDraft({ id: account?.id, provider: p.id, name: account?.name || p.name, credential: '', source_provider_id: account?.source_provider_id || (!account ? p.sources[0]?.id || '' : ''), api_key: '', model: route?.default_model || defaults[p.id] || '', enabled: route?.enabled ?? true });
  };
  const readModels = async () => {
    if (!draft?.id) return;
    const accountId = draft.id;
    setModelLoading(accountId); setModelFeedback(old => ({ ...old, [accountId]: '' }));
    try {
      const job = await rpc<Job>('ai_calls.models', { account_id: accountId });
      modelRequests.current.add(job.id);
      track(job);
    } catch (e) { setModelLoading(''); setModelFeedback(old => ({ ...old, [accountId]: errorText(e) })); }
  };
  const save = async () => {
    if (!draft) return;
    setSaving(true); setFailure('');
    let guard: ReturnType<typeof setTimeout> | undefined;
    try {
      const { api_key, model, enabled, ...quota } = draft;
      const timedOut = () => new Promise<never>((_, reject) => { guard = setTimeout(() => reject(new Error('保存迟迟未获本机服务确认，可稍后在账户列表查看是否已保存。')), 90000); });
      const account = await Promise.race([rpc<Account>('ai_quota.save', { ...quota, api_only: draft.provider === 'qianwen' && !!api_key }), timedOut()]);
      clearTimeout(guard); setDraft(old => old ? { ...old, id: account.id } : old);
      const followUp = () => { void load().catch(e => setFailure(errorText(e))); void refresh([account.id]); };
      if (defaults[draft.provider]) {
        const key = draft.provider === 'qianwen' ? api_key : draft.credential;
        // 额度账户已保存：立即收起弹窗，模型 API 配置在后台继续，失败用全局提示。
        setDraft(undefined);
        void rpc('ai_calls.save', { account_id: account.id, model, enabled, ...(key ? { api_key: key, source_provider_id: '' } : {}) })
          .then(() => followUp()).catch(e => { showError(e); followUp(); });
      } else { setDraft(undefined); followUp(); }
    } catch (e) { setFailure(errorText(e)); }
    finally { setSaving(false); if (guard) clearTimeout(guard); }
  };
  const remove = async () => {
    if (!deleting) return;
    setSaving(true);
    try { await rpc('ai_quota.remove', { id: deleting.id }); setDeleting(undefined); await load(); }
    catch (e) { setFailure(errorText(e)); }
    finally { setSaving(false); }
  };
  const open = (p: Platform) => { void native.openExternal(p.console_url).catch(showError); };
  const selected = platforms.find(p => p.id === draft?.provider);
  const shown = accounts.filter(a => (filter === 'all' || platforms.find(p => p.id === a.provider)?.kind === filter) && `${a.name} ${platforms.find(p => p.id === a.provider)?.name || ''}`.toLowerCase().includes(query.toLowerCase()));
  const counts = useMemo(() => ({ healthy: accounts.filter(a => a.status === 'ok' && !low(a)).length, attention: accounts.filter(a => (a.status !== 'ok' && a.status !== 'not_checked') || low(a)).length }), [accounts]);
  const last = Math.max(0, ...accounts.map(a => a.snapshot?.fetched_at || 0));

  return <div className="quota-page">
    <AiSyncConflicts />
    <div className="quota-toolbar"><div className="quota-sync"><span className={cx('quota-sync-dot', running && 'working')} />{running ? '正在更新额度…' : last ? `更新于 ${dateText(last)}` : '尚未查询'}<span className="quota-unit-hint">各平台独立计量</span></div><div className="button-row"><select className="quota-refresh-select" aria-label="自动刷新间隔" value={[1, 5, 15].includes(interval) ? interval : 0} onChange={e => void saveInterval(Number(e.target.value))}><option value={0}>手动刷新</option><option value={1}>每分钟刷新</option><option value={5}>每 5 分钟刷新</option><option value={15}>每 15 分钟刷新</option></select><Button disabled={!connected || running || !accounts.length} onClick={() => void refresh()}><RefreshCw size={15} className={running ? 'quota-spin' : ''} />刷新</Button><Button variant="primary" disabled={!connected || !platforms.length} onClick={() => add(platforms[0])}><Plus size={16} />添加账户</Button></div></div>
    {!draft && !deleting && failure && <Notice tone="warning">{failure}</Notice>}
    <div className="quota-overview"><div><span className="quota-overview-icon"><Wallet size={20} /></span><span>已添加账户<strong>{accounts.length}<small> 个</small></strong></span></div><div><span className="quota-overview-icon good"><Check size={20} /></span><span>额度正常<strong>{counts.healthy}<small> 个</small></strong></span></div><div><span className="quota-overview-icon warn"><CircleAlert size={20} /></span><span>需要关注<strong>{counts.attention}<small> 个</small></strong></span></div></div>
    {accounts.length > 0 && <><div className="quota-list-toolbar"><div className="quota-tabs" role="group" aria-label="账户类型">{[['all', '全部'], ['plan', '订阅套餐'], ['balance', 'API 余额']].map(([id, label]) => <button key={id} className={filter === id ? 'selected' : ''} onClick={() => setFilter(id)}>{label}</button>)}</div><div className="search-input"><Search size={15} /><input aria-label="搜索额度账户" placeholder="搜索账户" value={query} onChange={e => setQuery(e.target.value)} /></div></div>
    <div className="quota-grid">{shown.map(account => {
      const p = platforms.find(item => item.id === account.provider); if (!p) return null;
      const snapshot = account.snapshot, stale = account.status !== 'ok', style = styles[p.id];
      const route = routes.find(r => r.id === account.id);
      const mainBalance = snapshot?.balances[0];
      const mainMeter = snapshot?.meters.find(m => m.label === '每月') || snapshot?.meters[0];
      const state = stale ? account.status === 'not_checked' ? '待查询' : account.status === 'auth_required' ? '需重新连接' : '查询失败' : expired(account) ? '套餐已到期' : low(account) ? '额度偏低' : '正常';
      return <article className={cx('quota-card', stale && 'stale')} key={account.id} style={{ '--quota-color': style.color } as React.CSSProperties}>
        <div className="quota-card-heading"><span className="quota-provider-mark">{style.mark}</span><div className="quota-account-name"><h2>{account.name}</h2><small>{p.name}{snapshot?.plan ? ` · ${snapshot.plan}` : ''}</small></div><span className={cx('quota-status', stale || low(account) ? 'warning' : 'good')}><i />{state}</span></div>
        <div className="quota-main-value"><span>{stale && snapshot ? '上次查询 · ' : ''}{mainBalance?.label || (mainMeter ? `${mainMeter.label}剩余额度` : snapshot?.spend ? 'Key 累计消耗' : '可用额度')}</span><strong>{mainBalance ? money(mainBalance) : mainMeter ? <>{count(mainMeter.remaining_percent)}<em>%</em></> : snapshot?.spend ? money(snapshot.spend) : '—'}</strong>{snapshot?.expires_at && <small>套餐到期 {dateText(snapshot.expires_at)}</small>}</div>
        {snapshot?.balances.length ? <div className="quota-money-detail">{snapshot.balances.slice(1).map((b, i) => <div key={i}><span>{b.label}</span><b>{money(b)}</b></div>)}{snapshot.spend && <div><span>累计消耗</span><b>{money(snapshot.spend)}</b></div>}</div> : null}
        {snapshot?.meters.map((m, i) => <QuotaMeter key={i} item={m} stale={stale} />)}
        {account.error && <div className="quota-card-error" role="status"><CircleAlert size={14} /><span>{account.error}{snapshot ? ` 上次成功：${dateText(snapshot.fetched_at)}` : ''}</span></div>}
        {!snapshot && !account.error && <p className="quota-no-data">连接后查询真实额度</p>}
        {snapshot?.note && <p className="quota-card-note">{snapshot.note}</p>}
        {route && <div className="quota-api-detail"><div><b>模型 API</b><span>{route.enabled ? route.ready ? '已接入代理' : '待配置 Key' : '已停用'}</span></div><code>{route.default_model}</code><small>{route.usage.requests} 次调用 · 已知 {count(route.usage.total_tokens)} Tokens · {route.usage.token_reported_requests} 次有用量回执</small>{route.error && <small>{route.error}</small>}</div>}
        <div className="quota-card-footer"><button onClick={() => open(p)}>控制台 <ArrowUpRight size={13} /></button>{snapshot && <small className="quota-card-time">{dateText(snapshot.fetched_at)}</small>}<div><IconButton label={`刷新 ${account.name}`} disabled={running || !connected} onClick={() => void refresh([account.id])}><RefreshCw size={14} /></IconButton><IconButton label={`设置 ${account.name}`} disabled={!connected} onClick={() => add(p, account)}><Settings2 size={14} /></IconButton><IconButton label={`移除 ${account.name}`} disabled={!connected} onClick={() => { setFailure(''); setDeleting(account); }}><Trash2 size={14} /></IconButton></div></div>
      </article>;
    })}</div>{shown.length === 0 && <div className="quota-empty">没有匹配的账户</div>}</>}
    <AiProxyPanel />
    {connected && !platforms.length && !failure && <div className="quota-empty" role="status">正在读取平台列表…</div>}
    <div className="quota-footnote"><Clock3 size={13} /><span>查询间隔至少 1 分钟；失败时保留上次成功结果并标明状态。自动刷新仅在此页面可见时运行。</span></div>
    {draft && selected && <Modal wide title={draft.id ? '账户与 API 设置' : '添加聚合账户'} onClose={() => { if (!saving) { setDraft(undefined); setFailure(''); } }}><form className="quota-form modal-body quota-settings-form" onSubmit={e => { e.preventDefault(); void save(); }}>
      <div className="quota-settings-identity">{!draft.id && <Field label="平台"><select aria-label="平台" value={draft.provider} disabled={saving} onChange={e => { const p = platforms.find(item => item.id === e.target.value)!; add(p); }} >{platforms.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>}<Field label="账户名称"><input aria-label="账户名称" value={draft.name} maxLength={80} disabled={saving} onChange={e => setDraft({ ...draft, name: e.target.value })} /></Field></div>
      <div className={cx('quota-settings-grid', !defaults[draft.provider] && 'single')}>
        <section className="quota-settings-section"><h3>额度查询</h3>
          {selected.sources.length > 0 && <Field label="凭据来源"><select aria-label="凭据来源" disabled={saving} value={draft.source_provider_id} onChange={e => setDraft({ ...draft, credential: '', source_provider_id: e.target.value })}><option value="">单独填写凭据</option>{selected.sources.map(s => <option key={s.id} value={s.id}>模型设置 · {s.name}</option>)}</select></Field>}
          {!draft.source_provider_id && <Field label={selected.auth === 'cookie' ? '控制台 Cookie' : 'API Key'} hint={draft.id ? '留空保留已保存的凭据。' : '仅在本机加密保存，不回传到界面或任务日志。'}><input type="password" autoComplete="off" spellCheck={false} disabled={saving} value={draft.credential} onChange={e => setDraft({ ...draft, credential: e.target.value })} placeholder={draft.id ? '已保存，填写可替换' : selected.auth === 'cookie' ? '粘贴登录控制台的 Cookie' : '粘贴 API Key'} /></Field>}
          <div className="quota-connect-hint"><KeyRound size={16} /><span>{selected.hint}</span></div>
          {selected.auth === 'cookie' && <details className="quota-cookie-help"><summary>如何获取控制台 Cookie</summary><p>在浏览器登录此平台，按 F12 打开「网络」，刷新页面，选择控制台的 info.json 或额度请求，复制「请求标头」中的 Cookie 值。Cookie 过期后在此替换。</p><button type="button" className="button" onClick={() => open(selected)}>打开平台控制台 <ArrowUpRight size={14} /></button></details>}
        </section>
        {!!defaults[draft.provider] && <section className="quota-settings-section"><div className="quota-settings-section-heading"><h3>模型 API</h3><label className="ai-proxy-enable"><input type="checkbox" checked={draft.enabled} disabled={saving} onChange={e => setDraft({ ...draft, enabled: e.target.checked })} />启用代理</label></div>
          {draft.provider === 'qianwen' && <Field label="Token Plan API Key" hint="用于调用模型，留空保留已保存的 Key。"><input aria-label="Token Plan API Key" type="password" autoComplete="off" value={draft.api_key} disabled={saving} onChange={e => setDraft({ ...draft, api_key: e.target.value })} /></Field>}
          <Field label="默认模型"><input aria-label="默认模型" value={draft.model} disabled={saving} onChange={e => setDraft({ ...draft, model: e.target.value })} /></Field>
          <div className="quota-model-actions"><Button type="button" disabled={saving || !draft.id || modelLoading === draft.id || !!draft.api_key || !!draft.credential} onClick={() => void readModels()}><RefreshCw size={14} className={modelLoading === draft.id ? 'quota-spin' : ''} />{modelLoading === draft.id ? '读取中…' : '读取可用模型'}</Button>{draft.id && modelLoading === draft.id && <span role="status">正在获取模型目录…</span>}</div>
          {(!draft.id || !!draft.api_key || !!draft.credential) && <small className="quota-model-feedback">请先保存账户或新 Key，再读取模型。</small>}
          {draft.id && modelFeedback[draft.id] && <p className="quota-model-feedback" role="status">{modelFeedback[draft.id]}</p>}
          {draft.id && (models[draft.id]?.length || 0) > 0 && <div className="quota-model-picker"><input aria-label="搜索可用模型" placeholder="搜索模型 ID" value={modelQuery} onChange={e => setModelQuery(e.target.value)} /><select aria-label="可用模型列表" size={6} value={models[draft.id].includes(draft.model) ? draft.model : ''} disabled={saving} onChange={e => setDraft({ ...draft, model: e.target.value })}>{!models[draft.id].includes(draft.model) && <option value="" disabled>选择模型以填入上方默认模型</option>}{models[draft.id].filter(m => m.toLowerCase().includes(modelQuery.toLowerCase())).map(m => <option key={m} value={m}>{m}</option>)}</select>{!models[draft.id].some(m => m.toLowerCase().includes(modelQuery.toLowerCase())) && <small>没有匹配的模型</small>}</div>}
        </section>}
      </div>
      {failure && <Notice tone="warning">{failure}</Notice>}
      <div className="quota-form-actions"><Button type="button" disabled={saving} onClick={() => { setDraft(undefined); setFailure(''); }}>取消</Button><Button type="submit" variant="primary" disabled={saving || running || !connected || (!draft.id && !draft.credential.trim() && !draft.source_provider_id && !(draft.provider === 'qianwen' && draft.api_key.trim()))}>{saving ? '保存中…' : '保存'}</Button></div>
    </form></Modal>}
    {deleting && <Modal title="移除额度账户" onClose={() => { if (!saving) setDeleting(undefined); }}><p>移除「{deleting.name}」及其本地额度记录？模型设置中的密钥会保留。</p>{failure && <Notice tone="warning">{failure}</Notice>}<div className="quota-form-actions"><Button disabled={saving} onClick={() => setDeleting(undefined)}>取消</Button><Button variant="danger" disabled={saving} onClick={() => void remove()}>移除账户</Button></div></Modal>}
  </div>;
}
