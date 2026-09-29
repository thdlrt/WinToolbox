import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Boxes, CheckCircle2, FileStack, House, Moon, PanelLeftClose, PanelLeftOpen, Settings2, Sun, X, AlertCircle } from 'lucide-react';
import { errorText, isDesktop, rpc, subscribe, type Job, type Settings } from './api';
import { AppContext, type AppInfo, type Page } from './context';
import { cx, IconButton } from './ui';
import { migratedUiPreferences, validFavorites, validTheme } from './uiPreferences';
import { moveSidebarItem } from './sidebarOrder';
import DataSyncIndicator from './DataSyncIndicator';
import { visiblePolling } from './visiblePolling';
const HomePage = lazy(() => import('./pages/Home'));
const OrbSettingsPage = lazy(() => import('./pages/OrbSettings'));
const SystemMemoryPage = lazy(() => import('./pages/SystemMemory'));
import { listen } from '@tauri-apps/api/event';
const MediaPage = lazy(() => import('./pages/Media'));
const LivePage = lazy(() => import('./pages/Live'));
const CodexPage = lazy(() => import('./pages/Codex'));
const QuickScriptsPage = lazy(() => import('./pages/QuickScripts'));
const FileSyncPage = lazy(() => import('./pages/FileSync'));
const RelayPage = lazy(() => import('./pages/Relay'));
import './filesync.css';
const PluginsPage = lazy(() => import('./pages/Plugins'));
const SettingsPage = lazy(() => import('./pages/Settings'));
import { toolIcons } from './toolIcons';
const CaptionsPage = lazy(() => import('./pages/Captions'));
const CaptionOverlay = lazy(() => import('./pages/Captions').then(module => ({ default: module.CaptionOverlay })));
const PracticePage = lazy(() => import('./pages/Practice'));
const PhoneticsPage = lazy(() => import('./pages/Phonetics'));
const ExpensesPage = lazy(() => import('./pages/Expenses'));
const NetworkPage = lazy(() => import('./pages/Network'));
const ShizukuPage = lazy(() => import('./pages/Shizuku'));
const FnConnectPage = lazy(() => import('./pages/FnConnect'));
const GpuGuardPage = lazy(() => import('./pages/GpuGuard'));
const ProjectMemoryPage = lazy(() => import('./pages/ProjectMemory'));

const nav: { id: Page; name: string; icon: typeof House }[] = [
  { id: 'orb-settings', name: '悬浮球设置', icon: toolIcons['orb-settings'] },
  { id: 'ram', name: '内存清理', icon: toolIcons['ram'] },
  { id: 'relay', name: '文件中转站', icon: toolIcons['relay'] },
  { id: 'filesync', name: '文件同步', icon: toolIcons['filesync'] },
  { id: 'home', name: '工具首页', icon: toolIcons['home'] },
  { id: 'memory', name: '项目记忆', icon: toolIcons['memory'] },
  { id: 'media', name: '音视频工作台', icon: toolIcons['media'] }, { id: 'live', name: '实时助手', icon: toolIcons['live'] },
  { id: 'captions', name: '实时字幕', icon: toolIcons['captions'] },
  { id: 'practice', name: '口语练习', icon: toolIcons['practice'] },
  { id: 'expenses', name: '记账', icon: toolIcons['expenses'] },
  { id: 'network', name: '网络诊断', icon: toolIcons['network'] },
  { id: 'shizuku', name: 'Shizuku', icon: toolIcons['shizuku'] },
  { id: 'fnconnect', name: 'FN 远程访问', icon: toolIcons['fnconnect'] },
  { id: 'gpu', name: '独显省电守卫', icon: toolIcons['gpu'] },
  { id: 'codex', name: 'Codex 配置', icon: toolIcons['codex'] },
  { id: 'scripts', name: '快捷脚本', icon: toolIcons['scripts'] }, { id: 'plugins', name: '扩展工具', icon: toolIcons['plugins'] },
  { id: 'settings', name: '设置', icon: toolIcons['settings'] },
];

function localPinnedTools(): string[] {
  try { const value = JSON.parse(localStorage.getItem('wintoolbox-favorites') || '["media","live"]'); return validFavorites(value) ? value.map(id => id === 'files' ? 'scripts' : id) : ['media', 'live']; }
  catch { return ['media', 'live']; }
}

export default function App() {
  const overlay = new URLSearchParams(location.search).has('overlay');
  const captions = new URLSearchParams(location.search).has('captions');
  const [page, setPage] = useState<Page>('home');
  const pageRef = useRef<Page>('home'); pageRef.current = page;
  const beforeNavigate = useRef<(() => boolean | Promise<boolean>) | undefined>(undefined);
  const navigating = useRef(false);
  const setBeforeNavigate = useCallback((guard: (() => boolean | Promise<boolean>) | undefined) => { beforeNavigate.current = guard; }, []);
  const [collapsed, setCollapsed] = useState(false);
  const [connected, setConnected] = useState(false);
  const [info, setInfo] = useState<AppInfo>();
  const [settings, setSettings] = useState<Settings>();
  const [jobs, setJobs] = useState<Job[]>([]);
  const [theme, updateTheme] = useState(localStorage.getItem('wintoolbox-theme') || 'system');
  const [pinnedTools, setPinnedTools] = useState<string[]>(localPinnedTools);
  const pinnedToolsRef = useRef(pinnedTools); pinnedToolsRef.current = pinnedTools;
  const themeWrite = useRef(0);
  const pinnedWrite = useRef(0);
  const pinnedSaving = useRef(false);
  const uiMigration = useRef(false);
  const uiWriteQueue = useRef<Promise<unknown>>(Promise.resolve());
  const [toast, setToast] = useState<{ message: string; kind: 'success' | 'error' }>();
  const error = useCallback((value: unknown) => setToast({ message: errorText(value), kind: 'error' }), []);
  const success = useCallback((message: string) => setToast({ message, kind: 'success' }), []);
  const setTheme = useCallback((value: string) => {
    updateTheme(value); localStorage.setItem('wintoolbox-theme', value);
    if (!isDesktop()) return;
    const revision = ++themeWrite.current;
    const request = uiWriteQueue.current.catch(() => {}).then(() => rpc<Settings>('settings.update', { settings: { preferences: { theme: value } } }));
    uiWriteQueue.current = request;
    void request.then(result => { if (revision === themeWrite.current) setSettings(result); }).catch(error);
  }, [error]);
  const navigate = useCallback((value: Page) => {
    if (value === pageRef.current || navigating.current) return;
    void (async () => {
      navigating.current = true;
      try { if (beforeNavigate.current && !await beforeNavigate.current()) return; setPage(value); document.querySelector('.content-scroll')?.scrollTo(0, 0); }
      finally { navigating.current = false; }
    })();
  }, []);
  const refreshSettings = useCallback(async () => { setSettings(await rpc<Settings>('settings.get')); }, []);
  const savePinnedTools = useCallback((next: string[]) => {
    pinnedToolsRef.current = next;
    setPinnedTools(next);
    localStorage.setItem('wintoolbox-favorites', JSON.stringify(next));
    if (!isDesktop()) return;
    const revision = ++pinnedWrite.current;
    pinnedSaving.current = true;
    const request = uiWriteQueue.current.catch(() => {}).then(() => rpc<Settings>('settings.update', { settings: { preferences: { favorites: next } } }));
    uiWriteQueue.current = request;
    void request.then(result => { if (revision === pinnedWrite.current) { pinnedSaving.current = false; setSettings(result); } }).catch(reason => {
      if (revision === pinnedWrite.current) { pinnedSaving.current = false; error(reason); void refreshSettings().catch(error); }
    });
  }, [error, refreshSettings]);
  const togglePinnedTool = useCallback((id: string) => {
    if (!nav.some(item => item.id === id && item.id !== 'home' && item.id !== 'settings')) return;
    const previous = pinnedToolsRef.current;
    savePinnedTools(previous.includes(id) ? previous.filter(value => value !== id) : [...previous, id]);
  }, [savePinnedTools]);
  const drag = useRef<{ id: string; x: number; y: number; active: boolean; target?: string; after?: boolean } | undefined>(undefined);
  const suppressNavClick = useRef(false);
  const [dragMark, setDragMark] = useState<{ source: string; target?: string; after?: boolean }>();
  const finishDrag = (cancel = false) => {
    const state = drag.current;
    if (state?.active) {
      suppressNavClick.current = true;
      if (!cancel && state.target) savePinnedTools(moveSidebarItem(pinnedToolsRef.current, state.id, state.target, state.after));
    }
    drag.current = undefined; setDragMark(undefined);
  };
  const refreshJobs = useCallback(async () => { const result = await rpc<{ jobs: Job[] }>('jobs.list'); setJobs(result.jobs || []); }, []);
  const refresh = useCallback(async () => {
    if (!isDesktop()) return;
    const result = await Promise.allSettled([rpc<AppInfo>('app.info'), rpc<Settings>('settings.get'), rpc<{ jobs: Job[] }>('jobs.list')]);
    if (result[0].status === 'fulfilled') { setInfo(result[0].value); setConnected(true); } else { setConnected(false); error(result[0].reason); }
    if (result[1].status === 'fulfilled') setSettings(result[1].value); else error(result[1].reason);
    if (result[2].status === 'fulfilled') setJobs(result[2].value.jobs || []); else error(result[2].reason);
  }, [error]);
  const run = useCallback(async <T,>(action: () => Promise<T>, message?: string) => { try { const result = await action(); if (message) success(message); return result; } catch (reason) { error(reason); return undefined; } }, [error, success]);
  const track = useCallback((job: Job, message = '已开始处理，进度与结果显示在当前工具内。') => { setJobs(old => [job, ...old.filter(item => item.id !== job.id)]); success(message); }, [success]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (!settings) return;
    if (settings.preferences.ui_state_synced !== true) {
      if (uiMigration.current || !isDesktop()) return;
      uiMigration.current = true;
      const preferences = migratedUiPreferences(settings.preferences, localStorage.getItem('wintoolbox-theme'), localStorage.getItem('wintoolbox-favorites'));
      const request = uiWriteQueue.current.catch(() => {}).then(() => rpc<Settings>('settings.update', { settings: { preferences } }));
      uiWriteQueue.current = request;
      void request.then(result => { setSettings(result); }).catch(error).finally(() => { uiMigration.current = false; });
      return;
    }
    const selectedTheme = settings.preferences.theme;
    if (validTheme(selectedTheme)) { updateTheme(selectedTheme); localStorage.setItem('wintoolbox-theme', selectedTheme); }
    const favorites = settings.preferences.favorites;
    if (!pinnedSaving.current && validFavorites(favorites)) { setPinnedTools(favorites.map(id => id === 'files' ? 'scripts' : id)); localStorage.setItem('wintoolbox-favorites', JSON.stringify(favorites)); }
  }, [settings, error]);
  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const apply = () => { document.documentElement.dataset.theme = theme === 'system' ? (media.matches ? 'dark' : 'light') : theme; };
    apply(); media.addEventListener('change', apply); return () => media.removeEventListener('change', apply);
  }, [theme]);
  useEffect(() => { if (!toast) return; const timeout = setTimeout(() => setToast(undefined), toast.kind === 'error' ? 14000 : 4500); return () => clearTimeout(timeout); }, [toast]);
  useEffect(() => {
    if (!isDesktop()) return;
    let disposed = false; let off: (() => void) | undefined; let timer: ReturnType<typeof setTimeout> | undefined;
    void subscribe(event => {
      if (!document.hidden && (event.type.startsWith('job') || event.type === 'jobs.changed')) { clearTimeout(timer); timer = setTimeout(() => { void refreshJobs().catch(error); }, 180); }
      if (event.type === 'app.restored') { void refreshSettings().catch(error); success('已恢复备份，请重启工具箱以加载全部数据。'); }
      if (event.type === 'quick.error') error(String(event.message || '快捷菜单未能打开'));
      if (event.type === 'gpu_guard.activity') success(String(event.message || '检测到独显活动'));
      if (event.type === 'relay.open') { navigate('relay'); if (event.error) error(event.error); }
    }).then(unlisten => { if (disposed) unlisten(); else off = unlisten; }).catch(error);
    const checkRelay = () => { if (!overlay && !captions) return rpc<{ job_id?: string } | null>('relay.pending').then(value => { if (value?.job_id) navigate('relay'); }).catch(() => {}); };
    const stopPolling = visiblePolling(() => Promise.allSettled([refreshJobs(), checkRelay()]), 15000);
    return () => { disposed = true; off?.(); clearTimeout(timer); stopPolling(); };
  }, [refreshJobs, refreshSettings, error, success]);

  useEffect(() => {
    if (!isDesktop()) return;
    let disposed = false; let off: (() => void) | undefined;
    void listen<{ page: string }>('tool-open', e => { if (e.payload.page === 'quick-settings') { sessionStorage.setItem('wintoolbox-settings-tab', 'quick'); navigate('settings'); window.dispatchEvent(new Event('quick-settings-open')); return; } if (e.payload.page === 'files') { navigate('scripts'); return; } if (['home', 'relay', 'captions', 'settings', 'ram', 'orb-settings', 'filesync', 'memory', 'media', 'live', 'practice', 'phonetics', 'expenses', 'network', 'shizuku', 'fnconnect', 'gpu', 'codex', 'scripts', 'plugins'].includes(e.payload.page)) navigate(e.payload.page as Page); }).then(fn => { if (disposed) fn(); else off = fn; }).catch(error);
    return () => { disposed = true; off?.(); };
  }, [navigate]);

  const value = useMemo(() => ({ page, navigate, setBeforeNavigate, connected, info, settings, jobs, theme, setTheme, pinnedTools, togglePinnedTool, refresh, refreshJobs, refreshSettings, error, success, run, track }), [page, navigate, setBeforeNavigate, connected, info, settings, jobs, theme, setTheme, pinnedTools, togglePinnedTool, refresh, refreshJobs, refreshSettings, error, success, run, track]);
  const sidebarNav = [...new Set(pinnedTools)].flatMap(id => { const item = nav.find(item => item.id === id && id !== 'home' && id !== 'settings'); return item ? [item] : []; });
  const activeNav = page === 'phonetics' ? { name: '音标教学' } : nav.find(item => item.id === page)!;
  const pages = { 'orb-settings': <OrbSettingsPage />, ram: <SystemMemoryPage />, relay: <RelayPage />, filesync: <FileSyncPage />, home: <HomePage />, memory: <ProjectMemoryPage />, media: <MediaPage />, live: <LivePage />, captions: <CaptionsPage />, practice: <PracticePage />, phonetics: <PhoneticsPage />, expenses: <ExpensesPage />, network: <NetworkPage />, shizuku: <ShizukuPage />, fnconnect: <FnConnectPage />, gpu: <GpuGuardPage />, codex: <CodexPage />, scripts: <QuickScriptsPage />, plugins: <PluginsPage />, settings: <SettingsPage /> };

  return <AppContext.Provider value={value}>
    {captions ? <Suspense fallback={null}><CaptionOverlay /></Suspense> : overlay ? <div className="overlay-shell"><Suspense fallback={<div role="status">加载中…</div>}><LivePage overlay /></Suspense></div> : <div className={cx('app-shell', collapsed && 'nav-collapsed')}>
      <aside className="sidebar"><button className="brand" onClick={() => navigate('home')} aria-label="WinToolbox 首页"><span className="brand-logo"><Boxes size={22} strokeWidth={1.8} /></span><span className="brand-text">WinToolbox</span></button>
        <nav aria-label="主导航">{sidebarNav.map(item => <div key={item.id} data-sidebar-id={item.id} className={cx('sidebar-tool', dragMark?.source === item.id && 'dragging', dragMark?.target === item.id && (dragMark.after ? 'drop-after' : 'drop-before'))}>
          <button title={`${item.name} · 拖动排序`} aria-label={item.name} aria-current={page === item.id ? 'page' : undefined} className={cx('nav-item', (page === item.id || page === 'phonetics' && item.id === 'practice') && 'active')}
            onPointerDown={event => { if (event.button !== 0) return; suppressNavClick.current = false; drag.current = { id: item.id, x: event.clientX, y: event.clientY, active: false }; event.currentTarget.setPointerCapture(event.pointerId); }}
            onPointerMove={event => { const state = drag.current; if (!state || state.id !== item.id) return; if (!state.active && Math.hypot(event.clientX - state.x, event.clientY - state.y) < 6) return; state.active = true; const row = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>('[data-sidebar-id]'); state.target = row?.dataset.sidebarId; state.after = row ? event.clientY > row.getBoundingClientRect().top + row.getBoundingClientRect().height / 2 : false; setDragMark({ source: state.id, target: state.target, after: state.after }); }}
            onPointerUp={() => finishDrag()} onPointerCancel={() => finishDrag(true)} onLostPointerCapture={() => { if (drag.current) finishDrag(true); }}
            onKeyDown={event => { if (event.key === 'Escape') finishDrag(true); if (event.altKey && ['ArrowUp', 'ArrowDown'].includes(event.key)) { event.preventDefault(); const index = sidebarNav.findIndex(value => value.id === item.id); const down = event.key === 'ArrowDown'; const target = sidebarNav[index + (down ? 1 : -1)]; if (target) savePinnedTools(moveSidebarItem(pinnedToolsRef.current, item.id, target.id, down)); } }}
            onClick={() => { if (suppressNavClick.current) { suppressNavClick.current = false; return; } navigate(item.id); }}><item.icon size={19} strokeWidth={1.8} /><span>{item.name}</span></button>
        </div>)}</nav>
        <div className="sidebar-bottom"><button title="设置" aria-label="设置" aria-current={page === 'settings' ? 'page' : undefined} className={cx('nav-item', page === 'settings' && 'active')} onClick={() => navigate('settings')}><Settings2 size={19} strokeWidth={1.8} /><span>设置</span></button><div className="local-status"><span className={cx('connection-dot', connected && 'online')} /><span>{connected ? '服务已连接' : isDesktop() ? '服务未连接' : '浏览器预览'}</span><DataSyncIndicator /></div><button className="collapse-button" aria-label={collapsed ? '展开侧栏' : '收起侧栏'} title={collapsed ? '展开侧栏' : '收起侧栏'} onClick={() => setCollapsed(!collapsed)}>{collapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}<span>收起侧栏</span></button></div>
      </aside>
      <main className="main-panel"><header className="topbar"><h1 className="page-title">{activeNav.name}</h1><div className="topbar-actions"><IconButton label={theme === 'dark' ? '切换浅色模式' : '切换深色模式'} onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}</IconButton></div></header>
        {!connected && <div className="connection-banner"><FileStack size={15} /><span>{isDesktop() ? '本地服务尚未连接。配置与任务将在连接恢复后可用。' : '浏览器预览模式 · 打开桌面程序后即可连接本地文件和处理引擎。'}</span>{isDesktop() && <button onClick={() => void refresh()}>重新连接</button>}</div>}
        <div className="content-scroll"><div className="page-content" key={page}><Suspense fallback={<div role="status">加载中…</div>}>{pages[page]}</Suspense></div></div>
      </main>
    </div>}
    {toast && <div className={cx('toast', toast.kind)} role={toast.kind === 'error' ? 'alert' : 'status'}>{toast.kind === 'error' ? <AlertCircle size={19} /> : <CheckCircle2 size={19} />}<span>{toast.message}</span><IconButton label="关闭提示" onClick={() => setToast(undefined)}><X size={17} /></IconButton></div>}
  </AppContext.Provider>;
}
