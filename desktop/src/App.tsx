import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AudioLines, Boxes, Captions, CheckCircle2, Command, FileStack, FolderSync, House, Moon, PanelLeftClose, PanelLeftOpen, Radio, ReceiptText, Settings2, Smartphone, Speech, Sun, X, AlertCircle } from 'lucide-react';
import { errorText, isDesktop, rpc, subscribe, type Job, type Settings } from './api';
import { AppContext, type AppInfo, type Page } from './context';
import { cx, IconButton } from './ui';
import { migratedUiPreferences, validTheme } from './uiPreferences';
import HomePage from './pages/Home';
import MediaPage from './pages/Media';
import LivePage from './pages/Live';
import CodexPage from './pages/Codex';
import FilesPage from './pages/Files';
import PluginsPage from './pages/Plugins';
import SettingsPage from './pages/Settings';
import CaptionsPage, { CaptionOverlay } from './pages/Captions';
import PracticePage from './pages/Practice';
import PhoneticsPage from './pages/Phonetics';
import ExpensesPage from './pages/Expenses';
import ShizukuPage from './pages/Shizuku';
import FnConnectPage from './pages/FnConnect';

const nav: { id: Page; name: string; icon: typeof House }[] = [
  { id: 'home', name: '工具首页', icon: House },
  { id: 'media', name: '音视频工作台', icon: AudioLines }, { id: 'live', name: '实时助手', icon: Radio },
  { id: 'captions', name: '实时字幕', icon: Captions },
  { id: 'practice', name: '口语练习', icon: Speech },
  { id: 'expenses', name: '记账', icon: ReceiptText },
  { id: 'shizuku', name: 'Shizuku', icon: Smartphone },
  { id: 'fnconnect', name: '回家 VPN', icon: Radio },
  { id: 'codex', name: 'Codex 配置', icon: Command },
  { id: 'files', name: '文件整理', icon: FolderSync }, { id: 'plugins', name: '扩展工具', icon: Boxes },
  { id: 'settings', name: '设置', icon: Settings2 },
];

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
  const themeWrite = useRef(0);
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
    if (Array.isArray(favorites) && favorites.every(item => typeof item === 'string')) localStorage.setItem('wintoolbox-favorites', JSON.stringify(favorites));
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
      if (event.type.startsWith('job') || event.type === 'jobs.changed') { clearTimeout(timer); timer = setTimeout(() => { void refreshJobs().catch(error); }, 180); }
      if (event.type === 'app.restored') { void refreshSettings().catch(error); success('已恢复备份，请重启工具箱以加载全部数据。'); }
    }).then(unlisten => { if (disposed) unlisten(); else off = unlisten; }).catch(error);
    const interval = setInterval(() => { void refreshJobs().catch(() => {}); }, 4000);
    return () => { disposed = true; off?.(); clearTimeout(timer); clearInterval(interval); };
  }, [refreshJobs, refreshSettings, error, success]);

  const value = useMemo(() => ({ page, navigate, setBeforeNavigate, connected, info, settings, jobs, theme, setTheme, refresh, refreshJobs, refreshSettings, error, success, run, track }), [page, navigate, setBeforeNavigate, connected, info, settings, jobs, theme, setTheme, refresh, refreshJobs, refreshSettings, error, success, run, track]);
  const activeNav = page === 'phonetics' ? { name: '音标教学' } : nav.find(item => item.id === page)!;
  const pages = { home: <HomePage />, media: <MediaPage />, live: <LivePage />, captions: <CaptionsPage />, practice: <PracticePage />, phonetics: <PhoneticsPage />, expenses: <ExpensesPage />, shizuku: <ShizukuPage />, fnconnect: <FnConnectPage />, codex: <CodexPage />, files: <FilesPage />, plugins: <PluginsPage />, settings: <SettingsPage /> };

  return <AppContext.Provider value={value}>
    {captions ? <CaptionOverlay /> : overlay ? <div className="overlay-shell"><LivePage overlay /></div> : <div className={cx('app-shell', collapsed && 'nav-collapsed')}>
      <aside className="sidebar"><button className="brand" onClick={() => navigate('home')} aria-label="WinToolbox 首页"><span className="brand-logo"><Boxes size={22} strokeWidth={1.8} /></span><span className="brand-text">WinToolbox</span></button>
        <nav aria-label="主导航">{nav.map(item => <div key={item.id}><button title={item.name} className={cx('nav-item', (page === item.id || page === 'phonetics' && item.id === 'practice') && 'active')} onClick={() => navigate(item.id)}><item.icon size={19} strokeWidth={1.8} /><span>{item.name}</span></button></div>)}</nav>
        <div className="sidebar-bottom"><div className="local-status"><span className={cx('connection-dot', connected && 'online')} /><span>{connected ? '服务已连接' : isDesktop() ? '服务未连接' : '浏览器预览'}</span></div><button className="collapse-button" aria-label={collapsed ? '展开侧栏' : '收起侧栏'} title={collapsed ? '展开侧栏' : '收起侧栏'} onClick={() => setCollapsed(!collapsed)}>{collapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}<span>收起侧栏</span></button></div>
      </aside>
      <main className="main-panel"><header className="topbar"><h1 className="page-title">{activeNav.name}</h1><div className="topbar-actions"><IconButton label={theme === 'dark' ? '切换浅色模式' : '切换深色模式'} onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}</IconButton></div></header>
        {!connected && <div className="connection-banner"><FileStack size={15} /><span>{isDesktop() ? '本地服务尚未连接。配置与任务将在连接恢复后可用。' : '浏览器预览模式 · 打开桌面程序后即可连接本地文件和处理引擎。'}</span>{isDesktop() && <button onClick={() => void refresh()}>重新连接</button>}</div>}
        <div className="content-scroll"><div className="page-content" key={page}>{pages[page]}</div></div>
      </main>
    </div>}
    {toast && <div className={cx('toast', toast.kind)} role={toast.kind === 'error' ? 'alert' : 'status'}>{toast.kind === 'error' ? <AlertCircle size={19} /> : <CheckCircle2 size={19} />}<span>{toast.message}</span><IconButton label="关闭提示" onClick={() => setToast(undefined)}><X size={17} /></IconButton></div>}
  </AppContext.Provider>;
}
