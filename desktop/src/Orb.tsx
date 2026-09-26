import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { getCurrentWebviewWindow } from '@tauri-apps/api/webviewWindow';
import { ArrowUpToLine, Check, LoaderCircle, Minus, X } from 'lucide-react';
import { errorText, isDesktop, native, rpc, subscribe, type Job } from './api';
import MemoryRocket from './MemoryRocket';
import { defaultOrbActions, orbActions, type OrbPreferences } from './orbActions';
import { OrbMotion, ORB_MOTION_MS, type OrbPoint } from './orbMotion';
import './orb.css';

type Mode = 'idle' | 'menu' | 'context' | 'drop' | 'job' | 'result';
type Feedback = '' | 'success' | 'cancelled' | 'error';
interface Memory { percent: number; used: number; total: number; available: number }
interface OrbJob extends Job { result?: { uploaded?: string[]; errors?: { message: string }[]; available_change?: number } }
const gib = (n: number) => (n / 1024 ** 3).toFixed(1);
const active = (job: OrbJob) => ['queued', 'running', 'cancelling'].includes(job.status);
const defaultLayout: OrbPoint = { x: 256, y: 256, menu_x: 256, menu_y: 256 };

export default function Orb() {
  const [memory, setMemory] = useState<Memory>();
  const [captionsActive, setCaptionsActive] = useState(false);
  const [mode, setMode] = useState<Mode>('idle');
  const [expanded, setExpanded] = useState(false);
  const [layout, setLayout] = useState<OrbPoint>(defaultLayout);
  const [ready, setReady] = useState(!isDesktop());
  const [message, setMessage] = useState('');
  const [feedback, setFeedback] = useState<Feedback>('');
  const [working, setWorking] = useState(false);
  const [job, setJob] = useState<OrbJob>();
  const [shortcuts, setShortcuts] = useState(defaultOrbActions);
  const [operation, setOperation] = useState('');
  const [dropCount, setDropCount] = useState(0);
  const [queued, setQueued] = useState(0);
  const [dropWarning, setDropWarning] = useState('');
  const uploadQueue = useRef<string[][]>([]);
  const modeRef = useRef<Mode>('idle');
  const motion = useRef<OrbMotion | undefined>(undefined);
  const busy = useRef(false);
  const manualDetails = useRef(false);
  const finished = useRef('');
  const serial = useRef(0);
  const compactTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const feedbackTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const leaveTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const moveTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const userMoving = useRef(false);
  const pointer = useRef<{ x: number; y: number; moved: boolean } | undefined>(undefined);
  const ignoreClick = useRef(false);
  const dropSignature = useRef({ key: '', time: 0 });
  const setBusy = (value: boolean) => { busy.current = value; setWorking(value); };
  const expand = (next: Mode) => {
    modeRef.current = next; setMode(next);
    if (next === 'idle') motion.current?.hide();
    else void motion.current?.show(next === 'menu' || next === 'context');
  };
  const dismiss = () => { if (['menu', 'context', 'result', 'job'].includes(modeRef.current)) expand('idle'); };
  const acknowledge = () => { if (!busy.current) setFeedback(''); expand('idle'); };
  const problem = (error: unknown) => {
    clearTimeout(compactTimer.current); clearTimeout(feedbackTimer.current);
    setFeedback('error'); setMessage(errorText(error)); expand('result');
  };
  const open = (action: string) => { expand('idle'); void native.orbAction(action).catch(problem); };
  const complete = (value: OrbJob) => {
    setJob(value); setMessage(value.message || '处理中…');
    if (active(value) || finished.current === value.id) return;
    finished.current = value.id; setBusy(false); clearTimeout(compactTimer.current);
    if (value.status === 'cancelled') {
      uploadQueue.current = []; setQueued(0);
      setFeedback('cancelled'); setMessage('已取消传输'); expand('idle');
    } else if (value.status !== 'completed' || value.result?.errors?.length) {
      problem(value.result?.errors?.[0]?.message || value.error || value.message || '操作未完成'); return;
    } else {
      if (uploadQueue.current.length) { runNext(); return; }
      setFeedback('success');
      const change = value.result?.available_change;
      setMessage(value.tool === 'memory.clean' ? typeof change === 'number' ? `清理完成 · 可用内存${change >= 0 ? '+' : ''}${(change / 1024 ** 2).toFixed(0)} MB` : '清理完成' : `已上传 ${value.result?.uploaded?.length || 0} 个文件`);
      if (manualDetails.current && modeRef.current === 'job') {
        expand('result'); compactTimer.current = setTimeout(() => { if (modeRef.current === 'result') expand('idle'); }, 1700);
      } else expand('idle');
    }
    feedbackTimer.current = setTimeout(() => setFeedback(''), 2800);
  };
  const start = async (method: string, params: Record<string, unknown> = {}, background = false) => {
    if (busy.current) return;
    const request = ++serial.current;
    clearTimeout(feedbackTimer.current); clearTimeout(compactTimer.current);
    setBusy(true); manualDetails.current = false; finished.current = '';
    setOperation(method); setJob(undefined); setFeedback(''); setDropWarning('');
    setMessage(method === 'memory.clean' ? '准备静默清理' : '准备上传'); expand(background ? 'idle' : 'job');
    try {
      if (method === 'relay.upload') {
        const config = await rpc<{ configured: boolean }>('relay.get');
        if (!config.configured) throw new Error('请先在文件中转站保存 WebDAV 连接');
      }
      const value = await rpc<OrbJob>(method, params);
      if (request !== serial.current) return;
      complete(value);
      if (active(value)) compactTimer.current = setTimeout(() => {
        if (busy.current && !manualDetails.current && modeRef.current === 'job') expand('idle');
      }, 850);
    } catch (error) { if (request === serial.current) { setBusy(false); problem(error); } }
  };
  const runNext = () => {
    const paths = uploadQueue.current.shift(); setQueued(uploadQueue.current.length);
    if (paths) void start('relay.upload', { paths, path: '', move: false }, true);
  };
  const cancelUpload = () => {
    uploadQueue.current = []; setQueued(0); setMessage('正在取消…');
    if (job?.id) void rpc('jobs.cancel', { id: job.id }).catch(error => setMessage(errorText(error)));
  };
  useEffect(() => {
    const controller = new OrbMotion(
      (show, focus) => isDesktop() ? native.orbResize(show, focus) : Promise.resolve(defaultLayout),
      (show, point) => { if (point) { setLayout(point); setReady(true); } setExpanded(show); },
      () => matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : ORB_MOTION_MS,
      error => { setMessage(errorText(error)); setFeedback('error'); setReady(true); },
    );
    motion.current = controller;
    void controller.initialize();
    return () => { controller.dispose(); clearTimeout(compactTimer.current); clearTimeout(feedbackTimer.current); };
  }, []);
  useEffect(() => {
    if (!isDesktop()) return;
    let disposed = false; let off: (() => void) | undefined;
    const read = () => void rpc<OrbPreferences>('orb.settings.get').then(value => { if (!disposed) setShortcuts(value.actions); }).catch(error => { if (!disposed) setMessage(errorText(error)); });
    read();
    void subscribe(event => { if (event.type === 'orb.settings.changed') read(); }).then(fn => { if (disposed) fn(); else off = fn; }).catch(error => setMessage(errorText(error)));
    return () => { disposed = true; off?.(); };
  }, []);
  useEffect(() => {
    if (!isDesktop()) return;
    let disposed = false;
    const read = () => void rpc<Memory>('memory.status').then(value => { if (!disposed) setMemory(value); }).catch(() => {});
    const captions = () => void rpc<{ active: boolean }>('captions.state').then(value => { if (!disposed) setCaptionsActive(value.active); }).catch(() => {});
    read(); captions(); const captionTick = setInterval(captions, 4000); const tick = setInterval(read, 2000);
    return () => { disposed = true; clearInterval(tick); clearInterval(captionTick); };
  }, []);
  useEffect(() => {
    if (!isDesktop()) return;
    const w = getCurrentWebviewWindow(); let disposed = false; const offs: (() => void)[] = [];
    const attach = (promise: Promise<() => void>) => void promise.then(off => { if (disposed) off(); else offs.push(off); }).catch(error => setMessage(errorText(error)));
    attach(w.onFocusChanged(event => { if (!event.payload) dismiss(); }));
    window.addEventListener('blur', dismiss);
    attach(w.onDragDropEvent(event => {
      const value = event.payload;
      if (value.type === 'enter') setDropCount(value.paths.length);
      if (value.type === 'enter' || value.type === 'over') {
        clearTimeout(leaveTimer.current); clearTimeout(feedbackTimer.current);
        if (modeRef.current !== 'drop') { setFeedback(''); expand('drop'); }
      }
      if (value.type === 'leave') {
        clearTimeout(leaveTimer.current);
        leaveTimer.current = setTimeout(() => { if (modeRef.current === 'drop') expand('idle'); }, 240);
      }
      if (value.type === 'drop') {
        clearTimeout(leaveTimer.current);
        const paths = [...new Set(value.paths.filter(path => typeof path === 'string' && path.length > 0))];
        if (!paths.length) { problem('未读取到本地文件，请从文件资源管理器拖入文件或文件夹'); return; }
        const key = JSON.stringify([...paths].sort()), now = performance.now();
        if (dropSignature.current.key === key && now - dropSignature.current.time < 750) return;
        dropSignature.current = { key, time: now };
        if (busy.current) {
          if (uploadQueue.current.length >= 32) { setDropWarning('待传队列已满，这一批未接收，请稍后重试'); manualDetails.current = true; expand('job'); return; }
          uploadQueue.current.push(paths); setQueued(uploadQueue.current.length); expand('idle');
        } else void start('relay.upload', { paths, path: '', move: false });
      }
    }));
    attach(w.listen('orb-reset', () => expand('idle')));
    attach(w.onScaleChanged(() => {
      void native.orbSavePosition().then(point => { if (!disposed) setLayout(point); }).catch(error => setMessage(errorText(error)));
    }));
    attach(w.onMoved(() => {
      if (!userMoving.current) return;
      clearTimeout(moveTimer.current);
      moveTimer.current = setTimeout(() => {
        userMoving.current = false;
        void native.orbSavePosition().then(point => { if (!disposed) setLayout(point); }).catch(error => setMessage(errorText(error)));
      }, 350);
    }));
    return () => { disposed = true; offs.forEach(off => off()); window.removeEventListener('blur', dismiss); clearTimeout(moveTimer.current); clearTimeout(leaveTimer.current); };
  }, []);
  useEffect(() => {
    if (!job || !active(job)) return;
    let disposed = false; let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await rpc<OrbJob>('jobs.get', { id: job.id });
        if (disposed) return;
        complete(value);
        if (!active(value)) return;
      } catch (error) { if (!disposed) setMessage(`连接中断，正在重试：${errorText(error)}`); }
      if (!disposed) timer = setTimeout(poll, 550);
    };
    timer = setTimeout(poll, 300);
    return () => { disposed = true; clearTimeout(timer); };
  }, [job?.id, job?.status]);
  const toggleCaptions = async () => {
    if (busy.current) return;
    let succeeded = false;
    setBusy(true); setOperation('captions'); setFeedback(''); setMessage(captionsActive ? '正在停止字幕' : '正在开启字幕'); expand('idle');
    try {
      if (!captionsActive) await native.captionWindow(true);
      const state = await rpc<{ active: boolean }>(captionsActive ? 'captions.stop' : 'captions.start');
      setCaptionsActive(state.active); setFeedback('success'); setMessage(state.active ? '字幕已开启' : '字幕已停止');
      succeeded = true;
      feedbackTimer.current = setTimeout(() => setFeedback(''), 2200);
    } catch (error) { if (!captionsActive) void native.captionWindow(false); problem(error); }
    finally { setBusy(false); if (succeeded && uploadQueue.current.length) runNext(); }
  };
  const actions = shortcuts.flatMap(id => {
    const item = orbActions.find(action => action.id === id);
    return item ? [{ ...item, label: id === 'subtitle-toggle' && captionsActive ? '停止字幕' : item.label,
      action: () => id === 'clean' ? void start('memory.clean') : id === 'subtitle-toggle' ? void toggleCaptions() : open(id) }] : [];
  });
  const panel = expanded && ['drop', 'job', 'result'].includes(mode);
  const showing = (value: Mode) => expanded && mode === value;
  const percent = working ? Math.max(0, Math.min(100, job?.progress || 0)) : memory?.percent || 0;
  const coordinates = { '--home-x': `${layout.x}px`, '--home-y': `${layout.y}px`, '--menu-x': `${layout.menu_x ?? 256}px`, '--menu-y': `${layout.menu_y ?? 256}px` } as CSSProperties;
  return <main className={`orb-stage ${expanded ? 'is-expanded' : ''} ${ready ? 'is-ready' : ''} ${panel ? 'has-panel' : ''} feedback-${feedback} ${working ? 'is-working' : ''}`} style={coordinates} aria-label="工具箱悬浮球"
    onPointerDown={event => { if (event.button === 0 && !(event.target as Element).closest('button,nav,section')) dismiss(); }}
    onContextMenu={event => { event.preventDefault(); expand(modeRef.current === 'context' ? 'idle' : 'context'); }}
    onKeyDown={event => { if (event.key === 'Escape') acknowledge(); }}>
    <div className="orb-menu-layer" data-active={showing('menu')} inert={!showing('menu')}>
      <div className="orb-orbit"/>{actions.map((item, index) => <button key={item.id} className="orb-action" style={{ '--angle': `${index * 360 / actions.length - 90}deg`, '--delay': `${index * 18}ms` } as CSSProperties} onClick={item.action} title={item.label}><item.icon size={20}/><span>{item.label}</span></button>)}
    </div>
    <button className="orb-core" inert={panel} tabIndex={panel ? -1 : 0}
      aria-label={working ? '任务进行中，点击查看进度' : feedback ? `${message}，点击查看` : `系统内存 ${memory?.percent ?? '--'}%，点击快捷菜单，拖动移动`}
      title={working || feedback ? message : memory ? `系统内存 ${gib(memory.used)} / ${gib(memory.total)} GB\n点击快捷菜单 · 按住移动` : '正在读取系统内存'}
      onPointerDown={event => { if (event.button === 0 && modeRef.current === 'idle') { pointer.current = { x: event.clientX, y: event.clientY, moved: false }; ignoreClick.current = false; } }}
      onPointerMove={event => { const p = pointer.current; if (p && !p.moved && (event.buttons & 1) && Math.hypot(event.clientX - p.x, event.clientY - p.y) > 5) { p.moved = true; ignoreClick.current = true; userMoving.current = true; void getCurrentWebviewWindow().startDragging().catch(error => { userMoving.current = false; problem(error); }); } }}
      onPointerUp={() => { pointer.current = undefined; }}
      onClick={() => { if (ignoreClick.current) { ignoreClick.current = false; return; } if (working) { manualDetails.current = true; expand('job'); } else if (feedback === 'error') expand('result'); else expand(modeRef.current === 'menu' ? 'idle' : 'menu'); }}>
      <svg className={`orb-ring ${working && !percent ? 'orb-indeterminate' : ''}`} viewBox="0 0 88 88" aria-hidden="true"><circle className="orb-track" cx="44" cy="44" r="38"/><circle className="orb-meter" cx="44" cy="44" r="38" strokeDasharray={`${238.76 * (working && !percent ? 20 : percent) / 100} 238.76`}/></svg>
      <span className="orb-reading" data-active={!working && !feedback}>{memory?.percent ?? '--'}<small>%</small></span>
      <span className="orb-core-symbol" data-active={!!feedback || working}>
        {feedback === 'success' ? <Check className="orb-check" size={32}/> : feedback === 'cancelled' ? <Minus size={28}/> : feedback === 'error' ? <X size={28}/> : working && operation === 'memory.clean' ? <MemoryRocket/> : working ? <ArrowUpToLine className="orb-uploading" size={27}/> : null}
      </span>
      <span className="orb-caption">{working ? operation === 'memory.clean' ? '清理中' : operation === 'captions' ? '字幕' : queued ? `待传 ${queued} 批` : percent ? `${Math.round(percent)}%` : '上传中' : feedback === 'success' ? operation === 'relay.upload' ? `${job?.result?.uploaded?.length || 0} 个已上传` : '已完成' : feedback === 'cancelled' ? '已取消' : feedback === 'error' ? '请查看' : '内存'}</span>
    </button>
    <nav className="orb-context-menu" data-active={showing('context')} inert={!showing('context')} aria-label="悬浮球右键菜单">
      <button onClick={() => open('orb-settings')}>自定义快捷菜单</button><button onClick={() => open('ram')}>内存清理设置</button><button onClick={() => open('captions')}>字幕设置</button><button onClick={() => open('hide')}>收起到托盘</button><button onClick={() => open('quit')}>退出工具箱</button>
    </nav>
    <section className="orb-drop-panel" data-active={showing('drop')} inert={!showing('drop')}>
      <div className="orb-drop-icon"><ArrowUpToLine size={28}/></div><strong>{working ? '松手，加入上传队列' : '松手，发送到中转站'}</strong><span>{dropCount > 0 ? `${dropCount} 项 · ` : ''}{working ? `前面 ${queued + 1} 批 · ` : ''}保留本地原文件</span><div className="orb-drop-line"/>
    </section>
    <section className="orb-result-panel" data-active={showing('job') || showing('result')} inert={!showing('job') && !showing('result')}>
      <button className="orb-panel-close" aria-label="收起到小球，任务继续" onClick={acknowledge}><X size={15}/></button>
      {operation === 'memory.clean' && working ? <MemoryRocket/> : <div className="orb-result-symbol">{working ? <LoaderCircle className="orb-spinning" size={28}/> : feedback === 'error' ? <X size={26}/> : <Check className="orb-check" size={28}/>}</div>}
      <strong>{working ? operation === 'memory.clean' ? '正在清理内存' : operation === 'captions' ? '正在处理字幕' : '正在上传文件' : feedback === 'error' ? '尚未完成' : '已完成'}</strong><p role="status">{dropWarning || message}{queued > 0 && <span className="orb-queue-count">另有 {queued} 批文件等待上传</span>}</p>
      {working && <div className={`orb-progress ${!job?.progress ? 'orb-progress-pending' : ''}`}><i style={{ width: `${Math.max(5, job?.progress || 0)}%` }}/></div>}
      <div className="orb-result-actions">{queued > 0 && <button onClick={() => { uploadQueue.current = []; setQueued(0); }}>取消待传</button>}{!working && queued > 0 && <button onClick={runNext}>继续待传</button>}{working && operation === 'relay.upload' && <button disabled={!job?.id || job.status === 'cancelling'} onClick={cancelUpload}>取消上传</button>}<button onClick={() => open(operation === 'memory.clean' ? 'ram' : operation === 'captions' ? 'captions' : 'relay')}>{operation === 'memory.clean' ? '内存设置' : operation === 'captions' ? '字幕设置' : '打开中转站'}</button></div>
    </section>
  </main>;
}
