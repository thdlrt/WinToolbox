import { useEffect, useRef, useState } from 'react';
import { rpc } from '../api';
import { useApp } from '../context';
import { Button, Field, Notice, Section, Switch } from '../ui';
import { receiveSettings, savedSettings, settingsPayload, type SettingsDraft } from '../stackchanSettings';
import '../stackchan.css';

const pages = { face: '表情', tasks: '多会话任务台', weather: '时间天气', pc: 'PC 状态', printer: '拓竹打印机', quota: 'AI 额度' };
const states: Record<string, string> = { running: '运行中', waiting: '需要处理', error: '异常', idle: '空闲', preparing: '准备中', paused: '已暂停', done: '已完成', unknown: '等待状态' };
type Config = { enabled: boolean; host: string; port: number; city: string; latitude: number | null; longitude: number | null; gpu: string; thread_id: string; default_page: string; has_token: boolean;
  dsh_enabled: boolean; dsh_port: number;
  rotation_enabled: boolean; rotation_speed: string; rotation_pages: string[];
  interaction_enabled: boolean; pet_enabled: boolean; search_enabled: boolean; welcome_enabled: boolean; summary_ai: boolean; away_minutes: number; motion_seconds: number; search_cooldown_seconds: number; camera_mirror: boolean; companion_dir: string;
  presence_enabled: boolean; presence_absence_seconds: number; auto_screen_off: boolean; screen_mode: 'auto' | 'on' | 'off';
  printer_enabled: boolean; printer_connection: 'lan' | 'cloud'; printer_host: string; printer_serial: string; printer_name: string; has_printer_access_code: boolean };
type City = { name: string; latitude: number; longitude: number };
type State = { settings: Config; connected: boolean; running: boolean; page: string; error?: string; city_search?: string; cities: City[];
  health?: { supported: boolean; uptime_seconds?: number; observed_restarts: number };
  metrics?: { cpu?: { percent?: number }; ram?: { percent?: number }; gpu?: { name?: string; percent?: number; adapters?: { name: string; luid: string }[] } };
  interaction?: { busy?: boolean; status?: string; summary?: string; summary_source?: string; orientation?: string; error?: string; photos?: number; searches?: number; capture_seconds?: number;
    presence_error?: string; presence_photo_seconds?: number; current_action?: string; search_remaining_seconds?: number | null; camera_rpc_ms?: number; detection_ms?: number;
    presence?: { state: string; checked_at?: number; last_seen_at?: number; interval_seconds: number; misses: number; facing?: boolean };
    display?: { off: boolean; requested_off: boolean; reason: string; touch_wake_ms: number } };
  sessions?: { running?: number; waiting?: number; total?: number; error?: string; sources?: Record<string, { available: boolean; error?: string }> }; printer_import?: string;
  displayed_sessions?: { running?: number; waiting?: number; done?: number; total?: number };
  printer?: { available?: boolean; connected?: boolean; state?: string; progress?: number; remaining_minutes?: number; error?: string };
  weather?: { temperature?: number; error?: string }; quota?: { windows?: { remaining: number; minutes?: number }[]; error?: string } };

function Toggle({ label, checked, onChange, disabled = false }: { label: string; checked: boolean; onChange: (value: boolean) => void; disabled?: boolean }) {
  return <div className={`sc-toggle${disabled ? ' sc-disabled' : ''}`}><span>{label}</span><Switch label={label} checked={checked} onChange={onChange} disabled={disabled} /></div>;
}

export default function StackChanPage() {
  const { error, success } = useApp();
  const [state, setState] = useState<State>();
  const [form, setForm] = useState<SettingsDraft<Config>>();
  const [token, setToken] = useState('');
  const [printerCode, setPrinterCode] = useState('');
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [searching, setSearching] = useState(false);
  const revision = useRef(0);
  const mutating = useRef(false);
  const config = form?.draft;
  const payload = form ? settingsPayload(form, token, printerCode) : {};
  const dirty = Object.keys(payload).length > 0;
  const accept = (value: State) => { setState(value); setForm(old => receiveSettings(old, value.settings)); };

  useEffect(() => {
    let disposed = false;
    let polling = false;
    const update = async () => {
      if (polling || mutating.current) return;
      polling = true;
      const started = revision.current;
      try {
        const value = await rpc<State>('stackchan.status');
        if (!disposed && started === revision.current && !mutating.current) {
          setState(value); setForm(old => receiveSettings(old, value.settings));
        }
      } catch (reason) { if (!disposed) error(reason); }
      finally { polling = false; }
    };
    void update();
    const timer = setInterval(() => { if (!document.hidden) void update(); }, 2500);
    return () => { disposed = true; clearInterval(timer); };
  }, [error]);

  const patch = (value: Partial<Config>) => {
    setForm(old => old ? { ...old, draft: { ...old.draft, ...value } } : old);
    setSaveError('');
  };
  const save = async () => {
    if (!form || !dirty || mutating.current) return;
    const submitted = form.draft;
    mutating.current = true; revision.current++; setBusy(true); setSaveError('');
    try {
      const value = await rpc<State>('stackchan.settings.save', payload);
      setState(value); setForm(old => old ? savedSettings(old, submitted, value.settings) : receiveSettings(undefined, value.settings));
      setToken(''); setPrinterCode(''); success('设置已保存并生效');
    } catch (reason) { setSaveError('保存失败，修改已保留。请检查设置后重试。'); error(reason); }
    finally { revision.current++; mutating.current = false; setBusy(false); }
  };
  const screenPower = async (mode: 'auto' | 'on' | 'off') => {
    if (mutating.current) return;
    mutating.current = true; revision.current++; setBusy(true);
    try { accept(await rpc<State>('stackchan.settings.save', { screen_mode: mode })); }
    catch (reason) { error(reason); }
    finally { revision.current++; mutating.current = false; setBusy(false); }
  };
  const search = async () => {
    if (searching) return;
    setSearching(true);
    try {
      const value = await rpc<State>('stackchan.cities.search', { query });
      setState(old => old ? { ...old, cities: value.cities, city_search: value.city_search } : value);
    } catch (reason) { error(reason); }
    finally { setSearching(false); }
  };
  const importPrinter = async () => {
    if (mutating.current) return;
    mutating.current = true; revision.current++; setBusy(true);
    try { accept(await rpc<State>('stackchan.printer.import', { connection: config?.printer_connection ?? 'cloud' })); }
    catch (reason) { error(reason); }
    finally { revision.current++; mutating.current = false; setBusy(false); }
  };
  const presence = state?.interaction?.presence;
  const presenceLabel = ({ unknown: '等待检测', present: '人在', 'checking-face': '正在找人', 'checking-away': '离席计时中', away: '已离席' } as Record<string, string>)[presence?.state ?? 'unknown'] ?? '等待检测';
  const mode = state?.settings.screen_mode ?? 'auto';
  const sourceErrors = Object.entries(state?.sessions?.sources ?? {}).filter(([, value]) => value.error);

  return <div className="sc-settings">
    <fieldset className="sc-form" disabled={busy || !config}>
      <Section className="sc-connection">
        <div className="sc-connection-line">
          <div className="sc-connection-state"><span className={`sc-dot${state?.connected ? ' sc-online' : ''}`} /><strong>{state?.connected ? '已连接' : !config ? '正在读取设置' : state?.settings.enabled ? '等待连接' : '同步已关闭'}</strong>{state?.connected && <span>当前：{(pages as Record<string, string>)[state.page] || state.page}</span>}</div>
          <Toggle label="数据同步" checked={config?.enabled ?? false} onChange={enabled => patch({ enabled })} />
        </div>
        {state?.error && <Notice tone="warning">{state.error}</Notice>}
      </Section>

      <Section title="页面与轮播">
        <Toggle label="自动轮播" checked={config?.rotation_enabled ?? true} onChange={rotation_enabled => patch({ rotation_enabled })} />
        <div className="sc-fields">
          <Field label="轮播速度"><select disabled={!config?.rotation_enabled} value={config?.rotation_speed ?? 'normal'} onChange={e => patch({ rotation_speed: e.target.value })}><option value="slow">慢 · 20 秒</option><option value="normal">标准 · 12 秒</option><option value="fast">快 · 8 秒</option><option value="rapid">很快 · 5 秒</option></select></Field>
          <Field label="开机默认页"><select value={config?.default_page ?? 'face'} onChange={e => patch({ default_page: e.target.value })}>{Object.entries(pages).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></Field>
        </div>
        <fieldset className="sc-page-picker" disabled={!config?.rotation_enabled}>
          <legend>参与轮播的页面</legend>
          <div className="sc-page-options">{Object.entries(pages).map(([key, label]) => {
            const selected = config?.rotation_pages ?? [];
            const checked = selected.includes(key);
            return <label key={key} className={`sc-page-option${checked ? ' sc-selected' : ''}`}><input type="checkbox" checked={checked} disabled={checked && selected.length === 1} onChange={e => patch({ rotation_pages: e.target.checked ? [...selected, key] : selected.filter(page => page !== key) })} /><span>{label}</span></label>;
          })}</div>
        </fieldset>
        <p className="sc-hint">至少保留一页。手动触摸后暂停轮播 20 秒，左右滑动仍可切换全部页面。</p>
        <p className="sc-hint">任务台仅显示执行中、待处理和完成一小时内的任务。AI 额度页复用“AI 额度”中的账户，每 5 分钟刷新，支持轻触换组。</p>
        <Toggle label="任务台显示 DSH 会话" checked={config?.dsh_enabled ?? true} onChange={dsh_enabled => patch({ dsh_enabled })} />
        <div className="sc-status-line"><span>{state?.displayed_sessions?.running ?? '—'} 个运行</span><span>{state?.displayed_sessions?.waiting ?? '—'} 个待处理</span><span>{state?.displayed_sessions?.done ?? '—'} 个近期完成</span></div>
        {sourceErrors.map(([name, value]) => <Notice key={name} tone="warning">{name === 'dsh' ? 'DSH' : 'Codex'}：{value.error}</Notice>)}
        {state?.sessions?.error && <Notice tone="warning">{state.sessions.error}</Notice>}
      </Section>

      <Section title="屏幕与互动">
        <div className="sc-power-row"><div><span className="sc-label">屏幕控制</span><p className="sc-hint">点击立即生效</p></div><div className="sc-power" role="group" aria-label="屏幕控制">{([['auto', '自动'], ['on', '亮屏'], ['off', '熄屏']] as const).map(([value, label]) => <button type="button" key={value} aria-pressed={mode === value} className={mode === value ? 'sc-active' : ''} disabled={!state?.settings.enabled} onClick={() => void screenPower(value)}>{label}</button>)}</div></div>
        <Toggle label="摄像头检测在场" checked={config?.presence_enabled ?? false} onChange={presence_enabled => patch({ presence_enabled })} />
        {config?.presence_enabled && <div className="sc-nested">
          <Toggle label="离席后自动熄屏" checked={config.auto_screen_off} onChange={auto_screen_off => patch({ auto_screen_off })} />
          <Field label="无人后寻找多久确认离席"><select value={config.presence_absence_seconds} onChange={e => patch({ presence_absence_seconds: Number(e.target.value) })}><option value={30}>30 秒</option><option value={60}>1 分钟</option><option value={120}>2 分钟</option><option value={300}>5 分钟</option></select></Field>
          <p className="sc-hint">每 30 秒检测。找到后停止，未找到再熄屏；照片仅在本机识别。</p>
        </div>}
        <div className="sc-status-line"><span>{presenceLabel}</span><span>{state?.interaction?.display ? state.interaction.display.off ? '屏幕已熄灭' : '屏幕已点亮' : '等待屏幕回执'}</span>{mode !== 'auto' && <span>手动模式优先</span>}</div>
        <Toggle label="灯光与转头互动" checked={config?.interaction_enabled ?? true} onChange={interaction_enabled => patch({ interaction_enabled })} />
        {config?.interaction_enabled && <div className="sc-nested">
          <Toggle label="摸头回应并找人" checked={config.pet_enabled} onChange={pet_enabled => patch({ pet_enabled })} />
          <Toggle label="没看到人脸时小范围寻找" checked={config.search_enabled} onChange={search_enabled => patch({ search_enabled })} />
          <Toggle label="回来时欢迎并显示进展" checked={config.welcome_enabled} onChange={welcome_enabled => patch({ welcome_enabled })} />
          <details className="sc-details"><summary>动作与摘要设置</summary><div className="sc-details-body">
            <Field label="转头用时"><select value={config.motion_seconds} onChange={e => patch({ motion_seconds: Number(e.target.value) })}><option value={2}>2 秒</option><option value={3}>3 秒</option><option value={3.5}>3.5 秒</option><option value={5}>5 秒</option></select></Field>
            <Field label="自动找人最短间隔"><select value={config.search_cooldown_seconds ?? 300} onChange={e => patch({ search_cooldown_seconds: Number(e.target.value) })}><option value={180}>3 分钟</option><option value={300}>5 分钟</option><option value={600}>10 分钟</option></select></Field>
            <p className="sc-hint">连续两次没识别人脸才自动寻找；找到后保持朝向。摸头主动寻找不受间隔限制。</p>
            <Toggle label="反转相机左右方向" checked={config.camera_mirror} onChange={camera_mirror => patch({ camera_mirror })} />
            <Toggle label="使用 AI 整理进展摘要" checked={config.summary_ai} onChange={summary_ai => patch({ summary_ai })} />
            <Button disabled={dirty || !state?.settings.enabled || !state?.settings.interaction_enabled} busy={state?.interaction?.busy} onClick={() => { void rpc('stackchan.welcome.test').then(() => success('欢迎测试已排队')).catch(error); }}>测试欢迎与摘要</Button>
            <p className="sc-hint">全程静音；测试使用已保存的设置。</p>
          </div></details>
        </div>}
        {state?.interaction?.summary && <Notice>{state.interaction.summary}</Notice>}
        {state?.interaction?.presence_error && <Notice tone="warning">检测状态未知：{state.interaction.presence_error}</Notice>}
        {state?.interaction?.error && <Notice tone="warning">{state.interaction.error}</Notice>}
      </Section>

      <Section title="数据来源">
        <details className="sc-details"><summary>天气城市<span>{config?.city || '未设置'}</span></summary><div className="sc-details-body">
          <div className="sc-search"><input aria-label="搜索城市" value={query} onChange={e => setQuery(e.target.value)} placeholder="输入城市名称" onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); if (query.trim()) void search(); } }} /><Button busy={searching || state?.city_search === 'searching'} disabled={!query.trim()} onClick={() => void search()}>搜索</Button></div>
          {state?.cities?.length ? <Field label="选择城市"><select value="" onChange={e => { if (!e.target.value) return; const city = state.cities[Number(e.target.value)]; if (city) patch({ city: city.name, latitude: city.latitude, longitude: city.longitude }); }}><option value="">请选择搜索结果</option>{state.cities.map((city, index) => <option key={`${city.latitude},${city.longitude}`} value={index}>{city.name}</option>)}</select></Field> : state?.city_search === 'done' ? <p className="sc-hint">没有找到城市，试试拼音或附近的大城市。</p> : null}
          <p className="sc-hint">天气由 Open-Meteo 提供。</p>
        </div></details>
        {state?.weather?.error && <Notice tone="warning">{state.weather.error}</Notice>}
        {state?.city_search && !['done', 'searching'].includes(state.city_search) && <Notice tone="warning">{state.city_search}</Notice>}
        <Toggle label="读取拓竹打印机状态" checked={config?.printer_enabled ?? false} onChange={printer_enabled => patch({ printer_enabled })} />
        <details className="sc-details"><summary>打印机连接<span>{config?.printer_name || '尚未配置'} · {config?.printer_connection === 'cloud' ? '云端' : '局域网'}</span></summary><div className="sc-details-body">
          <div className="sc-fields">
            <Field label="连接方式"><select value={config?.printer_connection ?? 'cloud'} onChange={e => patch({ printer_connection: e.target.value as 'lan' | 'cloud' })}><option value="cloud">云端 · 保留 Bambu Handy</option><option value="lan">局域网 / VPN</option></select></Field>
            <Field label="显示名称"><input value={config?.printer_name ?? ''} onChange={e => patch({ printer_name: e.target.value })} /></Field>
            {config?.printer_connection !== 'cloud' && <Field label="打印机 IP"><input value={config?.printer_host ?? ''} onChange={e => patch({ printer_host: e.target.value })} /></Field>}
            <Field label="设备序列号"><input value={config?.printer_serial ?? ''} onChange={e => patch({ printer_serial: e.target.value })} /></Field>
            {config?.printer_connection !== 'cloud' && <Field label="访问码"><input type="password" autoComplete="new-password" maxLength={8} value={printerCode} placeholder={config?.has_printer_access_code ? '已保存，留空保留' : '8 位访问码'} onChange={e => { setPrinterCode(e.target.value); setSaveError(''); }} /></Field>}
          </div>
          <Button busy={state?.printer_import === 'running'} disabled={dirty} onClick={() => void importPrinter()}>从 Bambu Studio 导入</Button>
          <p className="sc-hint">{dirty ? '先保存或撤销修改，再导入。' : config?.printer_connection === 'cloud' ? '导入会保存本机 Studio 登录的设备，只读取状态，保留 Handy。' : '导入会保存设备信息；电脑需能访问打印机 8883 端口。'}</p>
        </div></details>
        {state?.printer_import === 'need_address' && config?.printer_connection !== 'cloud' && !config?.printer_host && <Notice>设备信息已导入，请补充打印机 IP 后保存。</Notice>}
        {state?.printer_import && !['running', 'done', 'need_address'].includes(state.printer_import) && <Notice tone="warning">{state.printer_import}</Notice>}
        {state?.settings.printer_enabled && <div className="sc-status-line"><span>{state.printer?.connected ? `${states[state.printer.state ?? 'unknown']} · ${state.printer.progress ?? '—'}% · 剩余 ${state.printer.remaining_minutes ?? '—'} 分钟` : '等待打印机连接'}</span></div>}
        {state?.settings.printer_enabled && state.printer?.error && <Notice tone="warning">{state.printer.error}</Notice>}
        <details className="sc-details"><summary>PC 与 Codex<span>CPU {state?.metrics?.cpu?.percent ?? '—'}% · 内存 {state?.metrics?.ram?.percent ?? '—'}%</span></summary><div className="sc-details-body">
          <Field label="显示的 GPU"><select value={config?.gpu ?? ''} onChange={e => patch({ gpu: e.target.value })}><option value="">自动选择独立显卡</option>{state?.metrics?.gpu?.adapters?.map(gpu => <option key={gpu.luid} value={gpu.luid}>{gpu.name}</option>)}</select></Field>
          <p className="sc-hint">GPU {state?.metrics?.gpu?.percent ?? '—'}%。任务台左下角显示当前 Codex 账号的额度。</p>
          <Button disabled={!state?.settings.enabled} onClick={() => { void rpc<State>('stackchan.refresh').then(accept).catch(error); }}>刷新数据</Button>
        </div></details>
        {state?.quota?.error && <Notice tone="warning">{state.quota.error}</Notice>}
      </Section>

      <Section className="sc-advanced">
        <details className="sc-details"><summary>连接设置<span>{config?.host || '未填写 IP'}:{config?.port ?? 8080}</span></summary><div className="sc-details-body sc-fields">
          <Field label="机器人 IP"><input value={config?.host ?? ''} onChange={e => patch({ host: e.target.value.trim() })} placeholder="192.168.100.156" /></Field>
          <Field label="机器人端口"><input type="number" min={1} max={65535} value={config?.port ?? 8080} onChange={e => patch({ port: Number(e.target.value) })} /></Field>
          <Field label="设备令牌"><input type="password" autoComplete="new-password" value={token} placeholder={config?.has_token ? '已保存，留空保留' : '填写设备令牌'} onChange={e => { setToken(e.target.value); setSaveError(''); }} /></Field>
          <Field label="DSH Pocket 端口"><input type="number" min={1} max={65535} value={config?.dsh_port ?? 3081} onChange={e => patch({ dsh_port: Number(e.target.value) })} /></Field>
        </div></details>
        <details className="sc-details"><summary>使用说明与诊断</summary><div className="sc-details-body">
          <p>左右滑动换页，上滑打开应用面板。红灯表示需要处理，绿灯表示完成；轻触只关闭提醒，回答与审批仍在 PC。</p>
          {state?.health?.supported && <p>连续运行 {Math.floor((state.health.uptime_seconds ?? 0) / 60)} 分钟 · 累计记录到 {state.health.observed_restarts} 次重启（包含人为重启，连接超时不计入）。</p>}
          <p>当前检测：{presenceLabel}{presence?.checked_at ? ` · ${new Date(presence.checked_at * 1000).toLocaleTimeString()}` : ''}。熄屏后触摸可临时点亮 2 分钟。</p>
          <p>默认抬头 15°，自动转头限位左右 40°、抬头 5–35°。照片只在本机内存识别，不保存或上传；关闭工具箱后停止检测。</p>
          {state?.interaction?.orientation && <p>定位：{state.interaction.orientation} · {state.interaction.photos ?? 0} 张照片 · 取图 {state.interaction.capture_seconds ?? '—'} 秒</p>}
        </div></details>
      </Section>
    </fieldset>
    <div className="sc-savebar">
      <span role="status" aria-live="polite" className={saveError ? 'sc-save-error' : dirty ? 'sc-unsaved' : ''}>{saveError || (busy ? '正在应用设置…' : dirty ? '有未保存的修改' : config ? '所有设置已保存' : '正在读取设置…')}</span>
      <div className="button-row"><Button disabled={!dirty || busy} onClick={() => { setForm(old => old ? { saved: old.saved, draft: old.saved } : old); setToken(''); setPrinterCode(''); setSaveError(''); }}>撤销修改</Button><Button variant="primary" busy={busy} disabled={!config || !dirty || state?.printer_import === 'running'} onClick={() => void save()}>保存修改</Button></div>
    </div>
  </div>;
}
