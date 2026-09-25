import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { getCurrentWebviewWindow } from '@tauri-apps/api/webviewWindow';
import { ArrowUpToLine, Check, LoaderCircle, X } from 'lucide-react';
import { errorText, isDesktop, native, rpc, subscribe, type Job } from './api';
import './orb.css';
import MemoryRocket from './MemoryRocket';
import { defaultOrbActions, orbActions, type OrbPreferences } from './orbActions';

type Mode = 'idle' | 'menu' | 'context' | 'drop' | 'job' | 'result';
interface Memory { percent: number; used: number; total: number; available: number }
interface OrbJob extends Job { result?: { uploaded?: string[]; errors?: { message: string }[]; available_change?: number } }
const gib = (n: number) => (n / 1024 ** 3).toFixed(1);

export default function Orb() {
  const [memory, setMemory] = useState<Memory>();
  const [captionsActive, setCaptionsActive] = useState(false);
  const [mode, setMode] = useState<Mode>('idle');
  const [message, setMessage] = useState('');
  const [failed, setFailed] = useState(false);
  const [job, setJob] = useState<OrbJob>();
  const [shortcuts, setShortcuts] = useState(defaultOrbActions);
  const [operation, setOperation] = useState('');
  const [dropCount, setDropCount] = useState(0);
  const modeRef = useRef<Mode>('idle'); modeRef.current = mode;
  const busy = useRef(false);
  const leaveTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const pointer = useRef<{ x: number; y: number; moved: boolean } | undefined>(undefined);
  const ignoreClick = useRef(false);
  const expand = (next: Mode) => {
    modeRef.current = next; setMode(next);
    if (isDesktop()) void native.orbResize(next !== 'idle', next === 'menu' || next === 'context').catch(e => setMessage(errorText(e)));
  };
  const dismiss = () => { if (['menu', 'context', 'result'].includes(modeRef.current) && !busy.current) expand('idle'); };
  const problem = (e: unknown) => { setFailed(true); setMessage(errorText(e)); expand('result'); };
  const open = (action: string) => { expand('idle'); void native.orbAction(action).catch(problem); };
  const start = async (method: string, params: Record<string, unknown> = {}) => {
    if (busy.current) return;
    busy.current = true; setOperation(method); setJob(undefined); setFailed(false); setMessage(method === 'memory.clean' ? '准备静默清理' : '准备上传'); expand('job');
    try {
      if (method === 'relay.upload') {
        const config = await rpc<{ configured: boolean }>('relay.get');
        if (!config.configured) throw new Error('请先在文件中转站保存 WebDAV 连接');
      }
      setJob(await rpc<OrbJob>(method, params));
    } catch (e) { busy.current = false; problem(e); }
  };
  useEffect(() => {
    if (!isDesktop()) return;
    let disposed = false; let off: (() => void) | undefined;
    const read = () => void rpc<OrbPreferences>('orb.settings.get').then(v => { if (!disposed) setShortcuts(v.actions); }).catch(e => { if (!disposed) setMessage(errorText(e)); });
    read();
    void subscribe(event => { if (event.type === 'orb.settings.changed') read(); }).then(fn => { if (disposed) fn(); else off = fn; }).catch(problem);
    return () => { disposed = true; off?.(); };
  }, []);
  useEffect(() => {
    if (!isDesktop()) return;
    let disposed = false;
    const read = () => void rpc<Memory>('memory.status').then(value => { if (!disposed) setMemory(value); }).catch(e => { if (!disposed) setMessage(errorText(e)); });
    const captions = () => void rpc<{ active: boolean }>('captions.state').then(v => { if (!disposed) setCaptionsActive(v.active); }).catch(() => {});
    read(); captions(); const captionTick = setInterval(captions, 4000); const tick = setInterval(read, 2000);
    return () => { disposed = true; clearInterval(tick); clearInterval(captionTick); };
  }, []);
  useEffect(() => {
    if (!isDesktop()) return;
    const w = getCurrentWebviewWindow(); let disposed = false; const offs: (() => void)[] = [];
    const attach = (p: Promise<() => void>) => void p.then(off => { if (disposed) off(); else offs.push(off); }).catch(problem);
    attach(w.onFocusChanged(event => { if (!event.payload) dismiss(); }));
    window.addEventListener('blur', dismiss);
    attach(w.onDragDropEvent(event => {
      const e = event.payload;
      if (busy.current) { if (e.type === 'drop') setMessage('当前任务尚未完成，请稍后再拖入文件'); return; }
      if (e.type === 'enter') setDropCount(e.paths.length);
      if (e.type === 'enter' || e.type === 'over') { clearTimeout(leaveTimer.current); if (modeRef.current !== 'drop') expand('drop'); }
      if (e.type === 'leave') { clearTimeout(leaveTimer.current); leaveTimer.current = setTimeout(() => { if (modeRef.current === 'drop' && !busy.current) expand('idle'); }, 200); }
      if (e.type === 'drop') {
        clearTimeout(leaveTimer.current);
        const paths = [...new Set(e.paths.filter(path => typeof path === 'string' && path.length > 0))];
        if (!paths.length) { problem('未读取到本地文件，请从文件资源管理器拖入文件或文件夹'); return; }
        void start('relay.upload', { paths, path: '', move: false });
      }
    }));
    attach(w.listen('orb-reset', () => { if (!busy.current) expand('idle'); }));
    let moveTimer: ReturnType<typeof setTimeout>;
    attach(w.onMoved(() => { clearTimeout(moveTimer); moveTimer = setTimeout(() => void native.orbSavePosition(), 400); }));
    return () => { disposed = true; offs.forEach(off => off()); window.removeEventListener('blur', dismiss); clearTimeout(moveTimer); clearTimeout(leaveTimer.current); };
  }, []);
  useEffect(() => {
    if (!job || !['queued', 'running', 'cancelling'].includes(job.status)) return;
    let disposed = false;
    const timer = setInterval(() => void rpc<OrbJob>('jobs.get', { id: job.id }).then(value => {
      if (disposed) return;
      setJob(value); setMessage(value.message || '处理中…');
      if (['completed', 'failed', 'cancelled', 'interrupted'].includes(value.status)) {
        busy.current = false;
        const ok = value.status === 'completed' && !value.result?.errors?.length;
        setFailed(!ok);
        setMessage(ok ? value.tool === 'memory.clean' ? `清理完成 · 可用内存${(value.result?.available_change || 0) >= 0 ? '+' : ''}${((value.result?.available_change || 0) / 1024 ** 2).toFixed(0)} MB` : `已上传 ${value.result?.uploaded?.length || 0} 个文件` : value.result?.errors?.[0]?.message || errorText(value.error || value.message || '操作未完成'));
        expand('result');
      }
    }).catch(e => { if (!disposed) setMessage(`连接中断，正在重试：${errorText(e)}`); }), 600);
    return () => { disposed = true; clearInterval(timer); };
  }, [job?.id, job?.status]);
  const toggleCaptions = () => {
    if (busy.current) return; busy.current = true;
    void (async () => {
      if (!captionsActive) await native.captionWindow(true);
      const state = await rpc<{ active: boolean }>(captionsActive ? 'captions.stop' : 'captions.start');
      setCaptionsActive(state.active); expand('idle');
    })().catch(problem).finally(() => { busy.current = false; });
  };
  const actions = shortcuts.flatMap(id => {
    const item = orbActions.find(a => a.id === id);
    return item ? [{ ...item, label: id === 'subtitle-toggle' && captionsActive ? '停止字幕' : item.label,
      action: () => id === 'clean' ? void start('memory.clean') : id === 'subtitle-toggle' ? toggleCaptions() : open(id) }] : [];
  });
  const percent = memory?.percent || 0;
  return <main className={`orb-stage orb-${mode} ${failed ? 'orb-failed' : ''}`} aria-label="工具箱悬浮球" onPointerDown={e => { if (e.button === 0 && !(e.target as Element).closest('button, .orb-context-menu, .orb-result-panel')) dismiss(); }} onContextMenu={e => { e.preventDefault(); if (!busy.current) expand(mode === 'context' ? 'idle' : 'context'); }} onKeyDown={e => { if (e.key === 'Escape' && !busy.current) expand('idle'); }}>
    {mode === 'menu' && <><div className="orb-orbit" />{actions.map((item, index) => <button key={item.id} className="orb-action" style={{ '--angle': `${index * 360 / actions.length - 90}deg`, '--delay': `${index * 24}ms` } as CSSProperties} onClick={item.action} title={item.label}><item.icon size={20}/><span>{item.label}</span></button>)}</>}
    {(mode === 'idle' || mode === 'menu' || mode === 'context') && <button className="orb-core" aria-label={`系统内存 ${memory?.percent ?? '--'}%，点击快捷菜单，拖动移动`} title={memory ? `系统内存 ${gib(memory.used)} / ${gib(memory.total)} GB\n点击快捷菜单 · 拖动移动` : message || '正在读取系统内存'} onPointerDown={e => { if (e.button === 0) { pointer.current = { x: e.clientX, y: e.clientY, moved: false }; ignoreClick.current = false; } }} onPointerMove={e => { const p = pointer.current; if (p && !p.moved && (e.buttons & 1) && Math.hypot(e.clientX - p.x, e.clientY - p.y) > 5) { p.moved = true; ignoreClick.current = true; void getCurrentWebviewWindow().startDragging().catch(problem); } }} onPointerUp={() => { pointer.current = undefined; }} onClick={() => { if (ignoreClick.current) { ignoreClick.current = false; return; } expand(mode === 'menu' ? 'idle' : 'menu'); }}>
      <svg viewBox="0 0 88 88" aria-hidden="true"><circle className="orb-track" cx="44" cy="44" r="38"/><circle className="orb-meter" cx="44" cy="44" r="38" strokeDasharray={`${238.76 * percent / 100} 238.76`}/></svg>
      <span className="orb-reading">{memory?.percent ?? '--'}<small>%</small></span><span className="orb-caption">内存</span><i className="orb-live" />
    </button>}
    {mode === 'context' && <nav className="orb-context-menu" aria-label="悬浮球右键菜单"><button onClick={() => open('orb-settings')}>自定义快捷菜单</button><button onClick={() => open('ram')}>内存清理设置</button><button onClick={() => open('captions')}>字幕设置</button><button onClick={() => open('hide')}>收起到托盘</button><button onClick={() => open('quit')}>退出工具箱</button></nav>}
    {mode === 'drop' && <section className="orb-drop-panel"><div className="orb-drop-icon"><ArrowUpToLine size={28}/></div><strong>松手，发送到中转站</strong><span>{dropCount > 0 ? `${dropCount} 项 · ` : ''}文件和文件夹 · 保留本地原文件</span><div className="orb-drop-line"/></section>}
    {(mode === 'job' || mode === 'result') && <section className="orb-result-panel">
      {operation === 'memory.clean' && !failed && <MemoryRocket complete={mode === 'result'}/>}
      <div className="orb-result-symbol" hidden={mode === 'job' && operation === 'memory.clean'}>{mode === 'job' ? operation === 'memory.clean' ? null : <LoaderCircle className="orb-spinning" size={28}/> : failed ? <X size={26}/> : <Check size={28}/>}</div>
      <strong>{mode === 'job' ? operation === 'memory.clean' ? '正在清理内存' : '正在上传文件' : failed ? '尚未完成' : '已完成'}</strong><p role="status">{message}</p>
      {mode === 'job' && <div className="orb-progress"><i style={{ width: `${Math.max(3, job?.progress || 0)}%` }}/></div>}
      <div className="orb-result-actions">{mode === 'job' ? operation !== 'memory.clean' && <button onClick={() => void rpc('jobs.cancel', { id: job?.id }).catch(problem)}>取消</button> : <button onClick={() => expand('idle')}>收起</button>}<button onClick={() => open(operation === 'memory.clean' ? 'ram' : 'relay')}>打开工具箱</button></div>
    </section>}
  </main>;
}
