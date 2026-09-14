import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BookOpen, ChevronLeft, ChevronRight, FolderCog, FolderOpen, Pause, Pencil, Play, Plus, RefreshCw, Save, Trash2, Volume2, X } from 'lucide-react';
import { errorText, native, rpc, subscribe, type Job } from '../api';
import { useApp } from '../context';
import { activeJob, Button, cx, dateText, Empty, Field, IconButton, Modal, Notice, Progress, Section } from '../ui';
import { emptyPlayback, PracticePlayer, practiceTokens, type PracticeAudio, type PracticeFragment, type PracticeItem, type PracticePlayback, type PracticePlayMode } from '../practicePlayer';
import { acceptPracticeCompletion, emptyPracticeDraft, filterPracticeItems, practiceDirty, practiceDraft, practicePlayable, type PracticeDraft } from '../practiceState';

interface WordInfo { word: string; ipa: string[]; source?: string; label?: string; note?: string }
interface Folder { id: string; name: string; item_count: number }
interface Voices { model: string; source: string; source_url?: string; custom_allowed: boolean; notice?: string; voices: { id: string; name: string; description?: string }[] }
type PracticeJob = Job & { result?: PracticeItem | PracticeFragment };
interface Transition { proceed: () => void; cancel?: () => void }
const completed = (status: string) => ['completed', 'succeeded', 'success'].includes(status);
const timeText = (time: number) => `${Math.floor(time / 60)}:${Math.floor(time % 60).toString().padStart(2, '0')}`;

export default function PracticePage() {
  const { connected, settings, jobs, run, track, refreshJobs, setBeforeNavigate, navigate } = useApp();
  const [draft, setDraft] = useState<PracticeDraft>(emptyPracticeDraft);
  const [items, setItems] = useState<PracticeItem[]>([]);
  const [folders, setFolders] = useState<Folder[]>([]);
  const foldersRef = useRef<Folder[]>([]);
  const [filter, setFilter] = useState('all');
  const [voices, setVoices] = useState<Voices>();
  const [item, setItem] = useState<PracticeItem>();
  const [audioTarget, setAudioTarget] = useState<PracticeItem>();
  const [selection, setSelection] = useState('');
  const [word, setWord] = useState<WordInfo>();
  const [wordLoading, setWordLoading] = useState(false);
  const [busy, setBusy] = useState('');
  const [cancelling, setCancelling] = useState(false);
  const [failure, setFailure] = useState('');
  const [rate, setRate] = useState(1);
  const [mode, setMode] = useState<PracticePlayMode>('continuous');
  const [playback, setPlayback] = useState<PracticePlayback>(emptyPlayback);
  const [transition, setTransition] = useState<Transition>();
  const [deleting, setDeleting] = useState<PracticeFragment | 'paragraph'>();
  const [foldersOpen, setFoldersOpen] = useState(false);
  const [fragmentsOpen, setFragmentsOpen] = useState(false);
  const player = useRef<PracticePlayer | undefined>(undefined);
  const mounted = useRef(true);
  const view = useRef(0);
  const wordRequest = useRef(0);
  const textView = useRef<HTMLDivElement>(null);
  const requestedJobs = useRef(new Map<string, { parentId: string; kind: 'paragraph' | 'fragment'; view: number }>());
  const handledJobs = useRef(new Set<string>());
  const dirty = practiceDirty(draft, item);
  const current = useRef({ draft, item, dirty, busy, audioTarget });
  current.current = { draft, item, dirty, busy, audioTarget };
  const closeFolders = useCallback(() => setFoldersOpen(false), []);
  const closeDelete = useCallback(() => { if (!current.current.busy) setDeleting(undefined); }, []);
  const closeTransition = useCallback(() => { transition?.cancel?.(); setTransition(undefined); }, [transition]);
  const generationJobs = useMemo(() => jobs.filter(job => ['practice.generate', 'practice.fragment'].includes(job.tool)) as PracticeJob[], [jobs]);
  const currentJob = item ? generationJobs.find(job => activeJob(job.status) && (job.params.id === item.id || requestedJobs.current.get(job.id)?.parentId === item.id)) : undefined;
  const locked = !!busy || !!currentJob || item?.status === 'generating';
  const anyGeneration = generationJobs.some(job => activeJob(job.status));
  const report = useCallback((reason: unknown) => { if (mounted.current) setFailure(errorText(reason)); }, []);
  const cancelGeneration = async () => {
    if (!currentJob || cancelling) return;
    setCancelling(true);
    try { await rpc('jobs.cancel', { id: currentJob.id }); await refreshJobs(); }
    catch (reason) { report(reason); }
    finally { if (mounted.current) setCancelling(false); }
  };
  const clearAudio = useCallback(() => { player.current?.setItem(undefined); setAudioTarget(undefined); }, []);
  const activateAudio = useCallback((target: PracticeItem, index = 0, play = true) => {
    if (!practicePlayable(target)) return;
    player.current?.setItem(target); setAudioTarget(target);
    if (play) void player.current?.play(index, true);
  }, []);
  const adopt = useCallback((value: PracticeItem, resetPlayer = false) => {
    setItem(value); setDraft(practiceDraft(value));
    current.current = { ...current.current, item: value, draft: practiceDraft(value), dirty: false };
    if (resetPlayer) { clearAudio(); if (practicePlayable(value)) activateAudio(value, 0, false); }
  }, [activateAudio, clearAudio]);
  const load = useCallback(async () => {
    const result = await Promise.all([rpc<{ items: PracticeItem[] }>('practice.list'), rpc<{ folders: Folder[] }>('practice.folders.list')]);
    if (mounted.current) { setItems(result[0].items || []); foldersRef.current = result[1].folders || []; setFolders(foldersRef.current); }
    return result[0].items || [];
  }, []);
  const loadVoices = useCallback(async () => {
    try { const result = await rpc<Voices>('practice.voices'); if (mounted.current) setVoices(result); }
    catch (reason) { report(reason); }
  }, [report]);
  const open = useCallback(async (id: string) => {
    const token = ++view.current; setBusy('open'); setFailure(''); clearAudio();
    try {
      const result = await rpc<PracticeItem>('practice.get', { id });
      if (mounted.current && token === view.current) { wordRequest.current++; setWord(undefined); setWordLoading(false); setSelection(''); setFragmentsOpen(false); adopt(result, true); }
    } catch (reason) { report(reason); }
    finally { if (mounted.current && token === view.current) setBusy(''); }
  }, [adopt, clearAudio, report]);
  const requestTransition = useCallback((proceed: () => void) => {
    if (current.current.busy) return;
    if (current.current.dirty) setTransition({ proceed }); else proceed();
  }, []);
  const newParagraph = () => requestTransition(() => {
    view.current++; wordRequest.current++; clearAudio(); setItem(undefined); setSelection(''); setWord(undefined); setFailure(''); setFragmentsOpen(false);
    const value = { ...emptyPracticeDraft(), folder_id: filter !== 'all' && filter !== 'unfiled' ? filter : null };
    setDraft(value); current.current = { ...current.current, item: undefined, draft: value, dirty: false };
  });
  const changeDraft = (patch: Partial<PracticeDraft>) => {
    view.current++; clearAudio(); wordRequest.current++; setWord(undefined); setWordLoading(false); setSelection('');
    setDraft(value => ({ ...value, ...patch }));
  };
  const saveDraft = useCallback(async (): Promise<PracticeItem | undefined> => {
    const snapshot = current.current;
    if (snapshot.item && !snapshot.dirty) return snapshot.item;
    if (!snapshot.draft.text.trim() || !/[A-Za-z]/.test(snapshot.draft.text)) { report('请输入需要保存的英文段落。'); return; }
    if (!snapshot.draft.voice.trim()) { report('请选择音色或填写自定义音色 ID。'); return; }
    const onlyMove = snapshot.item && snapshot.draft.text === snapshot.item.text && snapshot.draft.voice === snapshot.item.voice;
    const method = onlyMove ? 'practice.move' : 'practice.save';
    const params = onlyMove ? { id: snapshot.item!.id, folder_id: snapshot.draft.folder_id, ...(snapshot.item!.revision !== undefined ? { revision: snapshot.item!.revision } : {}) } : { ...snapshot.draft, ...(snapshot.item ? { id: snapshot.item.id, ...(snapshot.item.revision !== undefined ? { revision: snapshot.item.revision } : {}) } : {}) };
    try { const result = await rpc<PracticeItem>(method, params); if (mounted.current) { adopt(result); await load(); } return result; }
    catch (reason) { report(reason); return undefined; }
  }, [adopt, load, report]);
  const save = async () => { setBusy('save'); setFailure(''); const result = await saveDraft(); if (result && practicePlayable(result)) activateAudio(result, 0, false); setBusy(''); };
  const saveAndContinue = async () => { setBusy('save'); const result = await saveDraft(); setBusy(''); if (result) { const next = transition; setTransition(undefined); next?.proceed(); } };
  const discardAndContinue = () => {
    const next = transition; const original = current.current.item;
    const value = original ? practiceDraft(original) : emptyPracticeDraft();
    setDraft(value); current.current = { ...current.current, draft: value, dirty: false };
    setTransition(undefined); next?.proceed();
  };

  useEffect(() => {
    mounted.current = true;
    const audio = new Audio(); audio.preload = 'auto';
    const instance = new PracticePlayer(audio, id => rpc<PracticeAudio>('practice.audio', { id }), setPlayback, report);
    player.current = instance;
    return () => { mounted.current = false; view.current++; wordRequest.current++; instance.dispose(); player.current = undefined; };
  }, [report]);
  useEffect(() => { player.current?.setMode(mode); }, [mode]);
  useEffect(() => { player.current?.setRate(rate); }, [rate]);
  useEffect(() => {
    setBeforeNavigate?.(() => {
      if (current.current.busy) return false;
      if (!current.current.dirty) return true;
      return new Promise<boolean>(resolve => setTransition({ proceed: () => resolve(true), cancel: () => resolve(false) }));
    });
    return () => setBeforeNavigate?.(undefined);
  }, [setBeforeNavigate]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (current.current.dirty) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, []);
  useEffect(() => {
    if (!connected) return;
    let disposed = false; let off: (() => void) | undefined; const initialView = view.current;
    void load().then(values => { if (!disposed && initialView === view.current && !current.current.dirty && !current.current.item && values[0]) void open(values[0].id); }).catch(report);
    void subscribe(event => {
      if (event.type !== 'practice.changed') return;
      void load().catch(report);
      const selected = current.current.item;
      const refreshView = view.current;
      if (selected && event.id === selected.id && !current.current.dirty && !current.current.busy) {
        void rpc<PracticeItem>('practice.get', { id: selected.id }).then(fresh => {
          if (!disposed && refreshView === view.current && acceptPracticeCompletion(selected.id, current.current.item?.id, current.current.dirty) && !current.current.busy) adopt(fresh);
        }).catch(report);
      }
    }).then(unlisten => { if (disposed) unlisten(); else off = unlisten; }).catch(report);
    return () => { disposed = true; off?.(); };
  }, [connected, load, open, adopt, report]);
  useEffect(() => { if (connected) void loadVoices(); }, [connected, settings, loadVoices]);
  useEffect(() => {
    for (const job of generationJobs) {
      const requested = requestedJobs.current.get(job.id);
      if (!requested || handledJobs.current.has(job.id) || activeJob(job.status)) continue;
      handledJobs.current.add(job.id);
      const completionView = view.current;
      void (async () => {
        try {
          await load();
          if (completionView !== view.current || !acceptPracticeCompletion(requested.parentId, current.current.item?.id, current.current.dirty) || !mounted.current) return;
          const fresh = await rpc<PracticeItem>('practice.get', { id: requested.parentId });
          if (completionView !== view.current || !acceptPracticeCompletion(requested.parentId, current.current.item?.id, current.current.dirty) || !mounted.current) return;
          adopt(fresh);
          if (completed(job.status) && job.result && view.current === requested.view) {
            const target = requested.kind === 'fragment' ? fresh.fragments?.find(fragment => fragment.id === job.result!.id) : fresh;
            if (target && practicePlayable(target)) activateAudio(target);
            if (requested.kind === 'fragment') setFragmentsOpen(true);
          } else if (!completed(job.status)) report(job.error || job.message || '生成未完成，可重试。');
        } catch (reason) { report(reason); }
      })();
    }
  }, [generationJobs, load, adopt, activateAudio, report]);

  const generate = async (fragmentText?: string, fragment?: PracticeFragment) => {
    if (locked) return;
    setBusy('generate'); setFailure(''); clearAudio();
    try {
      const parent = await saveDraft(); if (!parent || !mounted.current) return;
      const method = fragmentText ? 'practice.fragment' : 'practice.generate';
      const params = { id: parent.id, ...(parent.revision !== undefined ? { revision: parent.revision } : {}), ...(fragmentText ? { text: fragmentText.trim(), voice: current.current.draft.voice, ...(fragment ? { fragment_id: fragment.id, force: true } : {}) } : { force: !dirty && practicePlayable(parent) }) };
      const job = await rpc<PracticeJob>(method, params);
      requestedJobs.current.set(job.id, { parentId: parent.id, kind: fragmentText ? 'fragment' : 'paragraph', view: view.current });
      setSelection(''); track(job, fragmentText ? '片段将保存在当前段落中。' : '正在生成段落朗读。');
    } catch (reason) { report(reason); }
    finally { if (mounted.current) setBusy(''); }
  };
  const deleteRecord = async () => {
    if (!item || !deleting || locked) return;
    setBusy('delete'); setFailure('');
    try {
      await rpc('practice.delete', { id: item.id, ...(deleting !== 'paragraph' ? { fragment_id: deleting.id } : {}), ...(item.revision !== undefined ? { revision: item.revision } : {}) });
      if (deleting === 'paragraph') { view.current++; clearAudio(); setItem(undefined); setDraft(emptyPracticeDraft()); setWord(undefined); setSelection(''); current.current = { ...current.current, item: undefined, draft: emptyPracticeDraft(), dirty: false }; }
      else { if (audioTarget?.id === deleting.id) clearAudio(); adopt(await rpc<PracticeItem>('practice.get', { id: item.id })); }
      setDeleting(undefined); await load();
    } catch (reason) { report(reason); }
    finally { setBusy(''); }
  };
  const lookup = async (value: string) => {
    const selected = window.getSelection(); if (selected && !selected.isCollapsed && textView.current?.contains(selected.anchorNode)) return;
    const token = ++wordRequest.current; setWord({ word: value, ipa: [] }); setWordLoading(true);
    try { const result = await rpc<WordInfo>('practice.word', { word: value }); if (mounted.current && token === wordRequest.current) setWord(result); }
    catch (reason) { if (token === wordRequest.current) report(reason); }
    finally { if (mounted.current && token === wordRequest.current) setWordLoading(false); }
  };
  const captureSelection = () => { const selected = window.getSelection(); if (selected && textView.current?.contains(selected.anchorNode) && textView.current.contains(selected.focusNode)) setSelection(selected.toString().trim()); };
  const refreshFolders = async () => {
    await load();
    if (filter !== 'all' && filter !== 'unfiled' && !foldersRef.current.some(folder => folder.id === filter)) setFilter('all');
    if (current.current.item && !current.current.dirty) adopt(await rpc<PracticeItem>('practice.get', { id: current.current.item.id }));
  };
  const filtered = filterPracticeItems(items, filter);
  const canGenerate = connected && !locked && !!draft.voice.trim() && /[A-Za-z]/.test(draft.text);
  const parentPlayable = !dirty && practicePlayable(item);
  const readingSentences = useMemo(() => parentPlayable && item ? item.sentences : draft.text.trim() ? [{ text: draft.text, audio_id: '' }] : [], [parentPlayable, item, draft.text]);
  const words = useMemo(() => readingSentences.map(sentence => practiceTokens(sentence.text)), [readingSentences]);
  const voiceKnown = voices?.voices.some(value => value.id === draft.voice);
  const audioFragment = audioTarget && item && audioTarget.id !== item.id;

  return <div className="practice-layout"><div className="practice-main">
    <Section><div className="practice-editor-heading"><span>{item ? '段落' : '新段落'}{dirty && <small>未保存</small>}</span><div className="button-row"><Button variant="ghost" onClick={() => navigate('phonetics')}><BookOpen size={14} />音标教学</Button><Button disabled={!connected || locked || !dirty || !draft.text.trim()} busy={busy === 'save'} onClick={save}><Save size={14} />保存</Button>{item && <IconButton label="删除段落" disabled={locked} onClick={() => requestTransition(() => setDeleting('paragraph'))}><Trash2 size={15} /></IconButton>}</div></div>
      <Field label="英文文本"><textarea className="practice-input" rows={5} maxLength={6000} disabled={locked} value={draft.text} spellCheck={false} placeholder="粘贴需要练习的英文…" onChange={event => changeDraft({ text: event.target.value })} onSelect={event => { const input = event.currentTarget; setSelection(input.value.slice(input.selectionStart, input.selectionEnd).trim()); }} /></Field>
      <div className="practice-options"><Field label="音色"><div className="practice-voice-select"><select aria-label="音色" disabled={locked || !connected} value={voiceKnown ? draft.voice : '__custom'} onChange={event => changeDraft({ voice: event.target.value === '__custom' ? '' : event.target.value })}>{voices?.voices.map(value => <option key={value.id} value={value.id}>{value.name}</option>)}<option value="__custom">自定义音色</option></select><IconButton label="刷新音色目录" disabled={!connected || locked} onClick={() => void loadVoices()}><RefreshCw size={14} /></IconButton></div>{!voiceKnown && <input aria-label="自定义音色 ID" value={draft.voice} disabled={locked} maxLength={100} onChange={event => changeDraft({ voice: event.target.value })} placeholder="输入音色 ID，例如 Cherry" />}</Field><Field label="分类"><select aria-label="段落分类" disabled={locked} value={draft.folder_id || ''} onChange={event => changeDraft({ folder_id: event.target.value || null })}><option value="">未分类</option>{folders.map(folder => <option key={folder.id} value={folder.id}>{folder.name}</option>)}</select></Field></div>
      <div className="practice-generate"><span className="small-note" title={voices?.notice}>{voices?.voices.length ? '预置官方音色目录' : voices?.notice || '音色目录读取中…'}</span><span className="small-note">{draft.text.length} / 6000</span><Button variant="primary" disabled={!canGenerate || !draft.voice.trim()} busy={busy === 'generate'} title={parentPlayable ? '重新调用 API 生成音频，可能产生费用' : undefined} onClick={() => void generate()}><Volume2 size={15} />{parentPlayable ? '重新生成' : dirty && item ? '保存并生成' : '生成朗读'}</Button></div>
      {(currentJob || item?.status === 'generating') && <div className="practice-generation"><span>{currentJob?.message || '正在生成…'}</span>{currentJob && <Button variant="ghost" busy={cancelling} disabled={!connected || currentJob.status === 'cancelling'} onClick={cancelGeneration}>取消</Button>}<Progress value={currentJob?.progress || 0} /></div>}
    </Section>
    {failure && <Notice tone="warning">{failure}</Notice>}
    {selection && <div className="practice-selection"><span title={selection}>{selection}</span><Button disabled={!canGenerate || !/[A-Za-z]/.test(selection)} onClick={() => void generate(selection)}><Volume2 size={14} />生成选段朗读</Button><IconButton label="取消选择" onClick={() => { setSelection(''); window.getSelection()?.removeAllRanges(); }}><X size={15} /></IconButton></div>}
    {readingSentences.length > 0 ? <Section className="practice-reader"><div className="practice-reader-heading"><span>{parentPlayable && item ? `${item.voice} · ${item.sentences.length} 句` : item?.stale ? '文字已更新，请重新生成朗读' : '文字预览 · 点击单词查看音标'}</span>{parentPlayable && item?.path && <Button variant="ghost" onClick={() => void run(() => native.open(item.path!))}><FolderOpen size={14} />打开音频</Button>}</div><div className="practice-sentences" ref={textView} onMouseUp={captureSelection} onKeyUp={captureSelection}>{readingSentences.map((_, index) => <div className={cx('practice-sentence', parentPlayable && audioTarget?.id === item?.id && playback.index === index && 'selected')} key={`${item?.id || 'draft'}-${index}`}>{parentPlayable && item && <IconButton label={`播放第 ${index + 1} 句`} onClick={() => activateAudio(item, index)}><Play size={15} /></IconButton>}<p lang="en">{words[index].map((token, offset) => token.word ? <button className={cx('practice-word', word?.word.toLowerCase() === token.word.toLowerCase() && 'selected')} key={offset} onClick={() => void lookup(token.word!)}>{token.text}</button> : <span key={offset}>{token.text}</span>)}</p></div>)}</div>
      {word && <div className="practice-word-info"><div><strong>{word.word}</strong><span className="practice-ipa">{wordLoading ? '查询音标…' : word.ipa?.length ? word.ipa.map(value => `/${value}/`).join('  ') : '本地词典暂无音标'}</span><small title={word.note}>{word.label || '美式音标 · 本地词典'}</small></div><Button disabled={!canGenerate || wordLoading} onClick={() => void generate(word.word)}><Volume2 size={14} />生成词发音</Button><IconButton label="关闭单词" onClick={() => { wordRequest.current++; setWordLoading(false); setWord(undefined); }}><X size={15} /></IconButton></div>}
      {!!item?.fragments?.length && <details className="details practice-fragments" open={fragmentsOpen} onToggle={event => setFragmentsOpen(event.currentTarget.open)}><summary>片段与单词 · {item.fragments.length}</summary><div>{item.fragments.map(fragment => <div className="practice-fragment" key={fragment.id}><Button variant="ghost" disabled={!practicePlayable(fragment) || dirty} onClick={() => activateAudio(fragment)}><Play size={14} />播放</Button><span title={fragment.text}>{fragment.text}{fragment.stale && <small>需重新生成</small>}</span><IconButton label={`重新生成片段 ${fragment.text}`} title="重新调用 API 生成音频，可能产生费用" disabled={locked || dirty} onClick={() => void generate(fragment.text, fragment)}><RefreshCw size={14} /></IconButton><IconButton label={`删除片段 ${fragment.text}`} disabled={locked} onClick={() => requestTransition(() => setDeleting(fragment))}><Trash2 size={14} /></IconButton></div>)}</div></details>}
      {audioTarget && !dirty && <div className="practice-player">{audioFragment && <div className="practice-fragment-playing"><span>片段：{audioTarget.text}</span>{parentPlayable && item && <Button variant="ghost" onClick={() => activateAudio(item)}>返回段落播放</Button>}</div>}<div className="practice-play-actions"><IconButton label="上一句" disabled={playback.index <= 0} onClick={() => void player.current?.play(playback.index - 1, true)}><ChevronLeft size={18} /></IconButton><Button variant="primary" busy={playback.loading} onClick={() => playback.playing ? player.current?.pause() : void player.current?.play()}>{playback.playing ? <Pause size={16} /> : <Play size={16} />}{playback.playing ? '暂停' : '播放'}</Button><IconButton label="下一句" disabled={playback.index >= audioTarget.sentences.length - 1} onClick={() => void player.current?.play(playback.index + 1, true)}><ChevronRight size={18} /></IconButton><span className="small-note">{playback.index + 1} / {audioTarget.sentences.length}</span><div className="flex-spacer" /><select aria-label="播放方式" value={mode} onChange={event => setMode(event.target.value as PracticePlayMode)}><option value="continuous">连续播放</option><option value="repeat">单句循环</option></select><select aria-label="播放速度" value={rate} onChange={event => setRate(Number(event.target.value))}><option value={0.75}>0.75×</option><option value={1}>1×</option><option value={1.25}>1.25×</option></select></div><div className="practice-seek"><span>{timeText(playback.time)}</span><input aria-label="当前句播放进度" type="range" min={0} max={playback.duration || 1} step={0.1} value={Math.min(playback.time, playback.duration || 1)} disabled={!playback.duration || playback.loading} onChange={event => player.current?.seek(Number(event.target.value))} /><span>{timeText(playback.duration)}</span></div></div>}
    </Section> : <Empty title="新建段落，保存文字并生成朗读" />}
  </div><Section className="practice-history" title="段落条目" action={<IconButton label="新建段落" disabled={!!busy} onClick={newParagraph}><Plus size={16} /></IconButton>}><div className="practice-folder-filter"><select aria-label="分类筛选" value={filter} onChange={event => setFilter(event.target.value)}><option value="all">全部段落</option><option value="unfiled">未分类</option>{folders.map(folder => <option key={folder.id} value={folder.id}>{folder.name}</option>)}</select><IconButton label="管理分类" disabled={!!busy || anyGeneration} onClick={() => requestTransition(() => setFoldersOpen(true))}><FolderCog size={16} /></IconButton><IconButton label="刷新段落" disabled={!connected || !!busy} onClick={() => void load().catch(report)}><RefreshCw size={14} /></IconButton></div>{filtered.length ? <div className="practice-history-list">{filtered.map(record => <button className={cx('practice-history-item', item?.id === record.id && 'selected')} disabled={!!busy} key={record.id} onClick={() => requestTransition(() => { void open(record.id); })}><BookOpen size={15} /><span><strong>{record.text}</strong><small>{record.status === 'draft' ? '待生成' : record.stale ? '待重新生成' : record.voice} · {dateText(record.updated_at || record.created_at)}</small></span></button>)}</div> : <p className="small-note">暂无段落</p>}</Section>
    {transition && <Modal title="保存修改" onClose={closeTransition}><div className="modal-body"><p>当前段落有未保存的修改。</p><div className="modal-footer"><Button onClick={() => { transition.cancel?.(); setTransition(undefined); }}>继续编辑</Button><div className="button-row"><Button disabled={!!busy} onClick={discardAndContinue}>放弃修改</Button><Button variant="primary" busy={busy === 'save'} onClick={saveAndContinue}>保存并继续</Button></div></div></div></Modal>}
    {deleting && <Modal title={deleting === 'paragraph' ? '删除段落' : '删除片段'} onClose={closeDelete}><div className="modal-body"><p>{deleting === 'paragraph' ? '删除此段落及其片段条目。' : `删除片段“${deleting.text}”。父段落保留。`}</p><div className="modal-footer"><Button disabled={!!busy} onClick={() => setDeleting(undefined)}>取消</Button><Button variant="danger" busy={busy === 'delete'} onClick={deleteRecord}>删除</Button></div></div></Modal>}
    {foldersOpen && <PracticeFolders folders={folders} locked={anyGeneration} onRefresh={refreshFolders} onClose={closeFolders} />}
  </div>;
}

function PracticeFolders({ folders, locked, onRefresh, onClose }: { folders: Folder[]; locked: boolean; onRefresh: () => Promise<void>; onClose: () => void }) {
  const { run } = useApp(); const [name, setName] = useState(''); const [renaming, setRenaming] = useState(''); const [rename, setRename] = useState(''); const [deleting, setDeleting] = useState<Folder>(); const [busy, setBusy] = useState(false);
  const mutate = async (method: string, params: Record<string, unknown>) => {
    setBusy(true);
    try { const result = await run(() => rpc(method, params)); if (result) { setName(''); setRenaming(''); setDeleting(undefined); await run(onRefresh); } }
    finally { setBusy(false); }
  };
  return <Modal title="管理分类" onClose={onClose}><div className="modal-body"><div className="practice-folder-create"><input aria-label="新分类名称" placeholder="新分类名称" value={name} disabled={busy || locked} onChange={event => setName(event.target.value)} /><Button disabled={!name.trim() || busy || locked} onClick={() => void mutate('practice.folders.create', { name: name.trim() })}><Plus size={14} />新建分类</Button></div><div className="practice-folder-list">{folders.map(folder => <div key={folder.id}>{renaming === folder.id ? <><input aria-label="重命名分类" value={rename} disabled={busy} onChange={event => setRename(event.target.value)} /><Button disabled={!rename.trim() || busy || locked} onClick={() => void mutate('practice.folders.rename', { id: folder.id, name: rename.trim() })}>保存</Button><IconButton label="取消重命名" onClick={() => setRenaming('')}><X size={14} /></IconButton></> : <><span>{folder.name}<small>{folder.item_count} 个段落</small></span><IconButton label={`重命名分类 ${folder.name}`} disabled={busy || locked} onClick={() => { setRenaming(folder.id); setRename(folder.name); }}><Pencil size={14} /></IconButton><IconButton label={`删除分类 ${folder.name}`} disabled={busy || locked} onClick={() => setDeleting(folder)}><Trash2 size={14} /></IconButton></>}</div>)}</div>{deleting && <Notice tone="warning"><p>删除“{deleting.name}”分类，段落将移至未分类。</p><div className="button-row spaced"><Button disabled={busy} onClick={() => setDeleting(undefined)}>取消</Button><Button variant="danger" busy={busy} onClick={() => void mutate('practice.folders.delete', { id: deleting.id })}>删除分类</Button></div></Notice>}<div className="modal-footer"><span className="small-note">删除分类不会删除段落。</span><Button onClick={onClose}>完成</Button></div></div></Modal>;
}
