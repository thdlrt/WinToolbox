import { useCallback, useEffect, useRef, useState } from 'react';
import { AudioLines, BookOpen, ChevronLeft, ChevronRight, Circle, Clock3, CornerDownLeft, FileAudio, Maximize2, Mic, Monitor, Play, Radio, RefreshCw, Send, Settings2, Square, Trash2, X } from 'lucide-react';
import { native, rpc, subscribe, type BackendEvent, type Citation, type Collection, type Job } from '../api';
import { useApp } from '../context';
import ToolJobs from '../ToolJobs';
import { Button, CheckField, cx, dateText, Empty, Field, IconButton, Modal, Notice, PageActions, Section } from '../ui';
import { modelConfig } from '../modelConfig';
import LiveLibrary, { type LibraryIndex } from './LiveLibrary';

interface Device { id: string; name: string; kind: 'microphone' | 'system'; is_default?: boolean }
interface Segment { id: string; text: string; translation?: string; final?: boolean; source?: string; start?: number; end?: number }
interface Answer { question: string; answer: string | Record<string, unknown>; sources?: Citation[] }
interface Session { id?: string; session_id?: string; started_at?: string | number; created_at?: string | number; status?: string; active?: boolean; recording_path?: string; [key: string]: unknown }
interface Recording { path: string; name: string }
interface SavedSession { session: Session; segments: Segment[]; answers: Answer[]; recordings?: Recording[] }

function answerText(answer: Answer['answer']) { return typeof answer === 'string' ? answer : Object.entries(answer).map(([key, value]) => `${({ english: 'English', chinese: '中文要点', translation: '中文题意' }[key] || key)}\n${typeof value === 'string' ? value : JSON.stringify(value)}`).join('\n\n'); }

function Sources({ sources }: { sources: Citation[] }) {
  const { run } = useApp();
  if (!sources.length) return null;
  return <div className="source-list"><span className="small-eyebrow">参考来源</span>{sources.map((source, index) => <button key={index} className="source-chip" title={source.text || String(source.path || '')} disabled={!source.path && !source.page_image} onClick={() => void run(() => native.open(String(source.page_image || source.path || '')))}><BookOpen size={13} /><span>{source.title || source.name || source.document || source.source || (source.path ? source.path.split(/[\\/]/).pop() : `来源 ${index + 1}`)}{source.page ? ` · 第 ${source.page} 页` : ''}</span></button>)}</div>;
}

export default function LivePage({ overlay = false }: { overlay?: boolean }) {
  const { connected, run, error, settings, navigate, track } = useApp();
  const config = modelConfig(settings);
  const [devices, setDevices] = useState<Device[]>([]);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [collectionId, setCollectionId] = useState('');
  const [libraryOpen, setLibraryOpen] = useState(false);
  const closeLibrary = useCallback(() => setLibraryOpen(false), []);
  const [source, setSource] = useState('system');
  const [systemId, setSystemId] = useState('');
  const [microphoneId, setMicrophoneId] = useState('');
  const [engine, setEngine] = useState(config.engine);
  const [prompt, setPrompt] = useState('');
  const [automatic, setAutomatic] = useState(true);
  const [record, setRecord] = useState(settings?.preferences.record_audio !== false);
  const [advanced, setAdvanced] = useState(false);
  const [active, setActive] = useState(false);
  const [sessionId, setSessionId] = useState('');
  const [segments, setSegments] = useState<Segment[]>([]);
  const [answers, setAnswers] = useState<Answer[]>([]);
  const [answerIndex, setAnswerIndex] = useState(-1);
  const [delta, setDelta] = useState('');
  const [status, setStatus] = useState('选择声音来源，开始一场会话。');
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [answering, setAnswering] = useState(false);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [removing, setRemoving] = useState<string>();
  useEffect(() => { if (!active && config.mode) setEngine(config.engine); }, [active, config.mode, config.engine]);
  const transcriptEnd = useRef<HTMLDivElement>(null);
  const sessionRef = useRef('');
  const loadCollections = useCallback(async () => {
    const result = await run(() => rpc<{ collections: Collection[] }>('knowledge.list'));
    if (result) setCollections(result.collections);
  }, [run]);
  const load = useCallback(async () => {
    if (!connected) return;
    const results = await Promise.allSettled([rpc<{ devices: Device[] }>('live.devices'), rpc<{ sessions: Session[] }>('live.history')]);
    if (results[0].status === 'fulfilled') { setDevices(results[0].value.devices); setSystemId(old => old || results[0].status === 'fulfilled' && results[0].value.devices.find(item => item.kind === 'system' && item.is_default)?.id || ''); setMicrophoneId(old => old || results[0].status === 'fulfilled' && results[0].value.devices.find(item => item.kind === 'microphone' && item.is_default)?.id || ''); } else error(results[0].reason);
    if (results[1].status === 'fulfilled') {
      setSessions(results[1].value.sessions);
      const current = results[1].value.sessions.find(item => item.active || ['starting', 'running', 'listening', 'recording', 'reconnecting', 'error'].includes(item.status || ''));
      if (current) { const id = current.id || current.session_id || ''; sessionRef.current = id; setSessionId(id); setActive(true); setSource(String(current.source || 'system')); setCollectionId(String(current.collection_id || '')); if (typeof current.record_audio === 'boolean') setRecord(current.record_audio); const result = await run(() => rpc<SavedSession>('live.get', { id })); if (result) { setSegments(result.segments || []); setAnswers(result.answers || []); setAnswerIndex((result.answers || []).length - 1); setRecordings(result.recordings || []); setStatus('正在监听…'); } }
    } else error(results[1].reason);
  }, [connected, error, run]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { if (connected) void loadCollections(); }, [connected, loadCollections]);
  useEffect(() => {
    let disposed = false; let off: (() => void) | undefined;
    const onEvent = (event: BackendEvent) => {
      if (event.type === 'hotkey' && event.action === 'previous') { setAnswerIndex(old => Math.max(0, old - 1)); return; }
      if (!event.type.startsWith('live.')) return;
      const payload = (event.data && typeof event.data === 'object' ? { ...event, ...event.data as Record<string, unknown> } : event) as BackendEvent;
      if (payload.session_id && sessionRef.current && payload.session_id !== sessionRef.current && active) return;
      if (payload.type === 'live.answer.error' || payload.type === 'live.translation.error') { error(payload.message || '实时处理未完成。'); setAnswering(false); setDelta(''); }
      if (payload.type === 'live.answer.cancelled') { setAnswering(false); setDelta(''); }
      if (payload.type === 'live.segment') { const segment = payload as unknown as Segment; setSegments(old => { const index = old.findIndex(item => item.id === segment.id); if (index >= 0) return old.map((item, i) => i === index ? { ...item, ...segment } : item); return [...old, segment]; }); }
      if (payload.type === 'live.answer') { const answer = { question: String(payload.question || ''), answer: payload.answer as Answer['answer'] || '', sources: payload.sources as Citation[] || [] }; setAnswers(old => { setAnswerIndex(old.length); return [...old, answer]; }); setDelta(''); setAnswering(false); }
      if (payload.type === 'live.answer.delta') { setAnswering(true); setDelta(old => old + String(payload.text || '')); }
      if (payload.type === 'live.status') { setStatus(String(payload.message || payload.status || '')); if (['running', 'listening', 'recording', 'connected', 'started'].includes(String(payload.status))) setActive(true); if (['stopped', 'idle'].includes(String(payload.status))) setActive(false); if (['error', 'failed', 'answer_cancelled'].includes(String(payload.status))) setAnswering(false); if (payload.session_id) { sessionRef.current = String(payload.session_id); setSessionId(String(payload.session_id)); } }
    };
    void subscribe(onEvent).then(unlisten => { if (disposed) unlisten(); else off = unlisten; }).catch(error);
    return () => { disposed = true; off?.(); };
  }, [active, error]);
  useEffect(() => { transcriptEnd.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }, [segments]);
  const start = async () => { setBusy(true); if (collectionId) { const index = await run(() => rpc<LibraryIndex>('knowledge.index_status', { collection_id: collectionId })); if (!index?.ready) { setLibraryOpen(true); if (index) error(index.message || '请先完成资料库索引。'); setBusy(false); return; } } const result = await run(() => rpc<{ session_id: string }>('live.start', { source, system_id: systemId || undefined, microphone_id: microphoneId || undefined, collection_id: collectionId || undefined, prompt, auto_answer: automatic, record_audio: record, engine: config.mode ? config.engine : engine, ...(config.local ? { model: config.model, device: config.device, compute_type: config.computeType } : {}) })); if (result) { sessionRef.current = result.session_id; setSessionId(result.session_id); setRecordings([]); setSegments([]); setAnswers([]); setAnswerIndex(-1); setDelta(''); setActive(true); setStatus('正在监听…'); } setBusy(false); };
  const stop = async () => { setBusy(true); const result = await run(() => rpc<{ session_id: string }>('live.stop')); if (result) { setActive(false); setStatus('会话已结束，记录已保存。'); void load(); const saved = await run(() => rpc<SavedSession>('live.get', { id: result.session_id })); if (saved) setRecordings(saved.recordings || []); } setBusy(false); };
  const ask = async () => { setAnswering(true); setDelta(''); const result = await run(() => rpc<Partial<Answer>>('live.answer', { ...(question.trim() ? { question: question.trim() } : {}), ...(collectionId ? { collection_id: collectionId } : {}) })); if (!result) setAnswering(false); else { setQuestion(''); if (result.answer) { const item = result as Answer; setAnswers(old => { if (old.some(value => value.question === item.question && answerText(value.answer) === answerText(item.answer))) return old; setAnswerIndex(old.length); return [...old, item]; }); setAnswering(false); } } };
  const cancel = async () => { const result = await run(() => rpc('live.cancel')); if (result) { setAnswering(false); setDelta(''); } };
  const openSession = async (id: string) => { const result = await run(() => rpc<SavedSession>('live.get', { id })); if (result) { setSegments(result.segments || []); setAnswers(result.answers || []); setAnswerIndex((result.answers || []).length - 1); setRecordings(result.recordings || []); setSessionId(id); sessionRef.current = id; setHistoryOpen(false); setStatus('正在查看已保存会话'); } };
  const removeSession = async () => { if (!removing) return; const result = await run(() => rpc('live.delete', { id: removing }), '会话与录音已删除。'); if (result) { if (removing === sessionId) { setSessionId(''); sessionRef.current = ''; setSegments([]); setAnswers([]); setRecordings([]); setAnswerIndex(-1); } setRemoving(undefined); void load(); } };
  const retranscribe = async () => { const job = await run(() => rpc<Job>('jobs.submit', { tool: 'media', params: { paths: recordings.map(item => item.path), origin: 'live', session_id: sessionId, engine: config.mode ? config.engine : engine, ...(config.local ? { model: config.model, device: config.device, compute_type: config.computeType } : {}), transcribe: true, translate: true } })); if (job) track(job, '录音已开始重新转写，结果显示在本页。'); };
  const answer = answers[answerIndex];

  return <>
    {overlay ? <div className="overlay-topbar" data-tauri-drag-region><span className="overlay-brand"><Radio size={17} />实时助手</span><span className={cx('connection-dot', active && 'online')} /><span className="muted">{active ? '监听中' : '已暂停'}</span><div className="flex-spacer" /><IconButton label="关闭悬浮窗" onClick={() => void run(() => native.overlay(false))}><X size={17} /></IconButton></div> : <PageActions action={<div className="button-row live-page-actions"><Button disabled={!connected} onClick={() => setLibraryOpen(true)}><BookOpen size={16} />{collectionId ? `资料库 · ${collections.find(item => item.id === collectionId)?.name || '已选择'}` : '资料库'}</Button><Button onClick={() => setHistoryOpen(!historyOpen)}><Clock3 size={16} />会话记录</Button><Button onClick={() => void run(() => native.overlay(true))} disabled={!connected}><Maximize2 size={16} />悬浮窗</Button></div>} />}
    {!overlay && libraryOpen && <LiveLibrary collections={collections} selected={collectionId} onSelect={setCollectionId} locked={active || busy} onRefresh={loadCollections} onClose={closeLibrary} />}
    {!overlay && historyOpen && <Section title="会话记录" action={<Button variant="ghost" onClick={() => void load()}><RefreshCw size={15} />刷新</Button>}>{sessions.length ? <div className="session-list">{sessions.map((item, index) => <div className="session-record" key={item.id || index}><button disabled={active} onClick={() => void openSession(item.id || item.session_id || '')}><Clock3 size={17} /><strong>{dateText(item.started_at || item.created_at)}</strong><small>{String(item.name || item.id || item.session_id || '')}</small><ChevronRight size={16} /></button><IconButton disabled={active} label="删除此会话与录音" onClick={() => setRemoving(item.id || item.session_id || '')}><Trash2 size={15} /></IconButton></div>)}</div> : <div className="quiet-empty">暂无会话记录</div>}</Section>}
    {!overlay && <div className="live-controls"><div className="source-picker">{[['system', '线上会议', Monitor], ['microphone', '现场交流', Mic], ['both', '双路采集', AudioLines]].map(([id, label, Icon]) => { const Component = Icon as typeof Monitor; return <button key={String(id)} disabled={active} className={cx(source === id && 'selected')} onClick={() => setSource(String(id))}><Component size={18} /><span>{String(label)}</span></button>; })}</div><div className="live-control-actions"><button className="current-mode-link" disabled={active} onClick={() => navigate('settings')}>{config.label}</button><IconButton label="会话设置" onClick={() => setAdvanced(!advanced)}><Settings2 size={18} /></IconButton><Button variant={active ? 'secondary' : 'primary'} busy={busy} disabled={!connected} onClick={active ? stop : start}>{active ? <Square size={15} /> : <Play size={15} />}{active ? '结束会话' : '开始会话'}</Button></div></div>}
    {!overlay && advanced && <Section title="会话设置" description="开始会话后，设置会锁定到本次会话。" action={<Button variant="ghost" onClick={() => void load()}><RefreshCw size={14} />刷新设备</Button>}><fieldset disabled={active} className="plain-fieldset"><div className="form-grid">{source !== 'microphone' && <Field label="系统声音"><select value={systemId} onChange={e => setSystemId(e.target.value)}><option value="">默认输出设备</option>{devices.filter(item => item.kind === 'system').map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>}{source !== 'system' && <Field label="麦克风"><select value={microphoneId} onChange={e => setMicrophoneId(e.target.value)}><option value="">默认输入设备</option>{devices.filter(item => item.kind === 'microphone').map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>}{!config.mode && <Field label="识别方式"><select value={engine} onChange={e => setEngine(e.target.value)}><option value="api">API · 流式识别</option><option value="faster-whisper">本地 Whisper · 分段识别</option></select></Field>}</div><Field label="回答要求（可选）"><textarea rows={2} placeholder="例如：我是论文汇报者，用第一人称给出简短英文回答，再附中文要点。" value={prompt} onChange={e => setPrompt(e.target.value)} /></Field><div className="inline-checks"><CheckField checked={automatic} onChange={setAutomatic} label="检测到完整问题后自动回答" /><CheckField checked={record} onChange={setRecord} label="保存会话录音" /></div></fieldset>{!config.mode && engine !== 'api' && <Notice>本地模式采用分段识别；请先安装 faster-whisper 模型。<button className="inline-link" onClick={() => navigate('settings')}>管理模型</button></Notice>}</Section>}
    <div className={cx('live-status-strip', active && 'active')}><span className={cx('connection-dot', active && 'online')} /><span>{status}</span>{active && record && <small><Circle size={8} fill="currentColor" />录音保存中</small>}</div>
    <div className={cx('live-grid', overlay && 'overlay-live-grid')}>
      {!overlay && <section className="live-transcript"><div className="panel-heading"><h2>双语记录</h2><span className="muted">{segments.filter(item => item.final).length} 段</span></div><div className="transcript-scroll">{segments.length ? segments.map((segment, index) => <div className={cx('transcript-segment', segment.final === false && 'pending')} key={segment.id || index}><div className="segment-meta"><span>{segment.source === 'microphone' ? '麦克风' : '系统声音'}</span><span>{segment.start !== undefined ? `${Math.floor(segment.start / 60).toString().padStart(2, '0')}:${Math.floor(segment.start % 60).toString().padStart(2, '0')}` : ''}{segment.final === false ? ' · 识别中' : ''}</span></div><p>{segment.text}</p>{segment.translation && <p className="translation">{segment.translation}</p>}</div>) : <Empty title={active ? '正在听取声音…' : '开始会话后显示转写'} />}<div ref={transcriptEnd} /></div></section>}
      <section className="live-answer-panel"><div className="panel-heading"><h2>回答助手</h2><div className="answer-pagination"><IconButton label="上一题" disabled={answerIndex <= 0} onClick={() => setAnswerIndex(answerIndex - 1)}><ChevronLeft size={16} /></IconButton><span>{answers.length ? `${answerIndex + 1} / ${answers.length}` : '—'}</span><IconButton label="下一题" disabled={answerIndex >= answers.length - 1} onClick={() => setAnswerIndex(answerIndex + 1)}><ChevronRight size={16} /></IconButton></div></div>
        <div className="answer-scroll">{delta ? <div className="answer-body streaming">{delta}<span className="typing-caret" /></div> : answer ? <><div className="question-block"><span className="muted">问题</span><p>{answer.question}</p></div><div className="answer-body">{answerText(answer.answer)}</div><Sources sources={answer.sources || []} /></> : <Empty title={answering ? '正在生成回答…' : '暂无回答'} />}</div>
        <div className="question-composer"><textarea aria-label="手动提问" rows={overlay ? 2 : 3} value={question} onChange={e => setQuestion(e.target.value)} placeholder={active ? "输入问题，或留空回答最近的问题…" : "开始会话后可手动提问…"} onKeyDown={e => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && !answering && connected && active) { e.preventDefault(); void ask(); } }} /><div className="composer-actions"><span><CornerDownLeft size={12} />Ctrl + Enter</span>{answering ? <Button onClick={cancel}><Square size={14} />取消回答</Button> : <Button variant="primary" disabled={!connected || !active || (!question.trim() && !segments.length)} onClick={ask}><Send size={14} />{question.trim() ? '发送问题' : '立即回答'}</Button>}</div></div>
      </section>
    </div>
    {!overlay && recordings.length > 0 && <Section className="top-gap" title="会话录音" description={`${recordings.length} 个录音片段，可回听或重新转写。`} action={<Button disabled={active} onClick={retranscribe}><RefreshCw size={15} />重新转写全部</Button>}><details className="details"><summary>展开录音片段</summary><div className="recording-list">{recordings.map(item => <div key={item.path}><FileAudio size={18} /><span>{item.name}</span><Button variant="ghost" onClick={() => void run(() => native.open(item.path))}><Play size={14} />打开播放</Button></div>)}</div></details></Section>}
    {!overlay && <ToolJobs scope="live" title="录音处理" />}
    {removing && <Modal title="删除会话与录音" onClose={() => setRemoving(undefined)}><div className="modal-body"><p>将删除此会话保存的文字、回答及录音文件。此操作无法撤销。</p><div className="modal-footer"><Button onClick={() => setRemoving(undefined)}>保留</Button><Button variant="danger" onClick={removeSession}>删除会话</Button></div></div></Modal>}
    {!overlay && <details className="details top-gap"><summary>快捷键</summary><p className="small-note">Ctrl + Alt + Space 回答 · Ctrl + Alt + Esc 取消 · Ctrl + Alt + ← 上一题</p></details>}
  </>;
}
