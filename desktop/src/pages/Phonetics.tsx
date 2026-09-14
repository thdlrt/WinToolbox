import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, RefreshCw } from 'lucide-react';
import { errorText, native, rpc } from '../api';
import { useApp } from '../context';
import { Button, Modal, Notice } from '../ui';
import { courseExport, PHONETICS_CHANNEL, phoneticsState, PhoneticsWriter, trustedPhoneticsMessage, type PhoneticsState } from '../phonetics';
import '../phonetics.css';

export default function PhoneticsPage() {
  const { connected, navigate, setBeforeNavigate } = useApp();
  const frame = useRef<HTMLIFrameElement>(null);
  const frameReady = useRef(false);
  const token = useRef(crypto.randomUUID());
  const alive = useRef(true);
  const initial = useRef<PhoneticsState | undefined>(undefined);
  const writer = useRef<PhoneticsWriter | undefined>(undefined);
  const stopRequest = useRef<{ id: string; discard: boolean; resolve: () => void; reject: (reason: unknown) => void } | undefined>(undefined);
  const [loaded, setLoaded] = useState(false);
  const [loadBusy, setLoadBusy] = useState(false);
  const [failure, setFailure] = useState('');
  const [saveStatus, setSaveStatus] = useState('');
  const [leaving, setLeaving] = useState(false);
  const [leaveChoice, setLeaveChoice] = useState<((answer: boolean) => void)>();
  const exportBusy = useRef(false);
  const origin = location.origin;
  const report = useCallback((reason: unknown) => { if (alive.current) setFailure(errorText(reason)); }, []);
  const post = useCallback((type: string, payload: Record<string, unknown> = {}) => frame.current?.contentWindow?.postMessage({ channel: PHONETICS_CHANNEL, token: token.current, type, ...payload }, origin), [origin]);
  const load = useCallback(async () => {
    setLoadBusy(true); setFailure('');
    try {
      const saved = phoneticsState(await rpc('phonetics.state.get'));
      if (!alive.current) return;
      initial.current = saved; writer.current = new PhoneticsWriter(state => rpc('phonetics.state.update', { state }));
      setLoaded(true); setSaveStatus('');
    } catch (reason) { report(reason); }
    finally { if (alive.current) setLoadBusy(false); }
  }, [report]);
  const retrySave = useCallback(async () => {
    setSaveStatus('正在保存…'); setFailure('');
    try { await writer.current?.flush(); if (alive.current) setSaveStatus('已保存'); return true; }
    catch (reason) { report(reason); if (alive.current) setSaveStatus('尚未保存'); return false; }
  }, [report]);
  const stop = useCallback(async (discard = false) => {
    if (!frame.current?.contentWindow || !frameReady.current) return;
    setLeaving(true);
    await new Promise<void>((resolve, reject) => { const id = crypto.randomUUID(); stopRequest.current = { id, discard, resolve, reject }; post('shutdown', { requestId: id }); });
  }, [post]);
  const begin = useCallback(() => { if (initial.current) post('init', { state: initial.current, theme: document.documentElement.dataset.theme || 'light' }); }, [post]);
  useEffect(() => { alive.current = true; return () => { alive.current = false; post('shutdown', { requestId: 'unmount' }); }; }, [post]);
  useEffect(() => { if (connected && !loaded) void load(); }, [connected, loaded, load]);
  useEffect(() => {
    const observer = new MutationObserver(() => post('theme', { theme: document.documentElement.dataset.theme || 'light' }));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => observer.disconnect();
  }, [post]);
  useEffect(() => {
    const receive = async (event: MessageEvent) => {
      if (!trustedPhoneticsMessage(event, frame.current?.contentWindow || null, origin, token.current)) return;
      const data = event.data;
      if (data.type === 'ready') { frameReady.current = true; begin(); return; }
      if (data.type === 'stopped') {
        const waiting = stopRequest.current;
        if (waiting && waiting.id === data.requestId) { try { if (!waiting.discard) await writer.current?.flush(); waiting.resolve(); } catch (reason) { waiting.reject(reason); } finally { stopRequest.current = undefined; } }
        return;
      }
      // Changes posted just before a navigation click precede the shutdown ACK
      // on this channel. Keep accepting them until the final flush above.
      if (!loaded || leaving && data.type !== 'change') return;
      if (data.type === 'change') {
        try {
          const state = phoneticsState(data.state); setSaveStatus('正在保存…'); setFailure('');
          await writer.current!.enqueue(state); if (alive.current) setSaveStatus('已保存');
        } catch (reason) { report(reason); if (alive.current) setSaveStatus('尚未保存'); }
      } else if (data.type === 'external') {
        try { const url = new URL(data.url); if (!['https:', 'http:'].includes(url.protocol)) throw new Error('只能打开 http 或 https 链接。'); await native.openExternal(url.href); }
        catch (reason) { report(reason); post('result', { requestId: data.requestId, error: errorText(reason) }); }
      } else if (data.type === 'export' && !exportBusy.current) {
        exportBusy.current = true;
        try { const file = courseExport(data.name, data.bytes); const path = await native.saveCourseFile(file.name, file.bytes); post('result', { requestId: data.requestId, message: path ? '文件已保存。' : '已取消保存。' }); }
        catch (reason) { report(reason); post('result', { requestId: data.requestId, error: errorText(reason) }); }
        finally { exportBusy.current = false; }
      }
    };
    window.addEventListener('message', receive); return () => window.removeEventListener('message', receive);
  }, [begin, leaving, loaded, origin, post, report]);
  useEffect(() => {
    setBeforeNavigate?.(async () => {
      if (!loaded) return true;
      if (exportBusy.current) { report('请先完成或取消文件保存。'); return false; }
      let discard = false;
      if (!await retrySave()) {
        discard = await new Promise<boolean>(resolve => setLeaveChoice(() => resolve));
        if (!discard) return false;
      }
      try { await stop(discard); return true; }
      catch (reason) { report(reason); setLeaving(false); post('resume'); setSaveStatus('尚未保存'); return false; }
    });
    return () => setBeforeNavigate?.(undefined);
  }, [loaded, post, report, retrySave, setBeforeNavigate, stop]);
  const chooseLeave = useCallback((value: boolean) => { leaveChoice?.(value); setLeaveChoice(undefined); }, [leaveChoice]);
  return <div className="phonetics-page"><div className="phonetics-toolbar"><Button variant="ghost" disabled={leaving} onClick={() => navigate('practice')}><ArrowLeft size={15} />返回口语练习</Button><span className="small-note" role="status">{leaving ? '正在停止音频与麦克风，请处理尚未关闭的权限提示…' : saveStatus}</span></div>
    {failure && <Notice tone="warning"><p>{failure}</p><Button variant="ghost" busy={loadBusy} onClick={() => void (loaded ? retrySave() : load())}><RefreshCw size={14} />{loaded ? '重试保存' : '重新加载'}</Button></Notice>}
    {loaded ? <iframe ref={frame} className="phonetics-frame" title="音标教学课程" src={`/phonetics/index.html?bridge=${token.current}`} allow="microphone" onLoad={begin} /> : !failure && <p className="small-note">{connected ? '正在加载学习进度…' : '连接本地服务后加载课程。'}</p>}
    {leaveChoice && <Modal title="进度尚未保存" onClose={() => chooseLeave(false)}><div className="modal-body"><p>继续学习可保留当前修改并重试保存；现在离开会丢失尚未保存的进度。</p><div className="modal-footer"><Button onClick={() => chooseLeave(false)}>继续学习</Button><Button variant="danger" onClick={() => chooseLeave(true)}>不保存并离开</Button></div></div></Modal>}
  </div>;
}
