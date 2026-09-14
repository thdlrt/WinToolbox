import { useState } from 'react';
import { ArrowDownToLine, ChevronLeft, ChevronRight, Search } from 'lucide-react';
import { native, rpc, type Subtitle } from './api';
import { useApp } from './context';
import { basename, Button, Empty, Field, IconButton, Modal } from './ui';

export default function SubtitleEditor({ path, onClose }: { path: string; onClose: () => void }) {
  const { run, success } = useApp();
  const [segments, setSegments] = useState<Subtitle[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(0);
  const [dirty, setDirty] = useState(false);
  const load = async () => { setBusy(true); const result = await run(() => rpc<{ segments: Subtitle[] }>('subtitles.load', { path })); if (result) { setSegments(result.segments); setLoaded(true); } setBusy(false); };
  const update = (id: string, patch: Partial<Subtitle>) => { setSegments(old => old.map(segment => segment.id === id ? { ...segment, ...patch } : segment)); setDirty(true); };
  const save = async (saveAs = false) => {
    if (segments.some(segment => !Number.isFinite(segment.start) || !Number.isFinite(segment.end) || segment.start < 0 || segment.end <= segment.start)) { await run(async () => { throw new Error('每段字幕的起始时间须为非负数，结束时间须大于起始时间。'); }); return; }
    let target = path;
    if (saveAs) { const result = await run(() => native.save(basename(path).replace(/\.[^.]+$/, '.srt'), [{ name: '字幕', extensions: ['srt', 'vtt', 'json', 'txt'] }])); if (!result) return; target = result; }
    setBusy(true); const result = await run(() => rpc('subtitles.save', { path: target, segments })); if (result) { success(`字幕已保存：${basename(target)}`); setDirty(false); } setBusy(false);
  };
  const filtered = segments.filter(item => `${item.text} ${item.translation || ''} ${item.speaker || ''}`.toLowerCase().includes(query.toLowerCase()));
  const pages = Math.max(1, Math.ceil(filtered.length / 30));
  return <Modal title={`字幕编辑 · ${basename(path)}`} onClose={onClose} wide><div className="subtitle-editor">{!loaded ? <Empty title="加载字幕进行逐段编辑" action={<Button variant="primary" busy={busy} onClick={load}>加载字幕</Button>}>时间单位为秒；可导出 SRT、VTT、JSON 或 TXT。</Empty> : <><div className="list-toolbar"><span className="muted">{segments.length} 段字幕{dirty ? ' · 有未保存修改' : ''}</span><div className="search-input"><Search size={16} /><input placeholder="搜索原文或译文" value={query} onChange={e => { setQuery(e.target.value); setPage(0); }} /></div></div><div className="subtitle-scroll">{filtered.slice(page * 30, page * 30 + 30).map((segment, index) => <div className="subtitle-row" key={segment.id}><div className="subtitle-time"><span className="subtitle-number">{page * 30 + index + 1}</span><input type="number" min="0" step="0.01" aria-label="起始秒数" value={segment.start} onChange={e => update(segment.id, { start: Number(e.target.value) })} /><span>→</span><input type="number" min="0" step="0.01" aria-label="结束秒数" value={segment.end} onChange={e => update(segment.id, { end: Number(e.target.value) })} /><input className="speaker-input" value={segment.speaker || ''} placeholder="说话人" aria-label="说话人" onChange={e => update(segment.id, { speaker: e.target.value })} /></div><div className="subtitle-text"><Field label="原文"><textarea rows={2} value={segment.text} onChange={e => update(segment.id, { text: e.target.value })} /></Field><Field label="译文"><textarea rows={2} value={segment.translation || ''} onChange={e => update(segment.id, { translation: e.target.value })} /></Field></div></div>)}</div><div className="modal-footer"><div className="pagination"><IconButton label="上一页" disabled={page === 0} onClick={() => setPage(page - 1)}><ChevronLeft size={17} /></IconButton><span>{page + 1} / {pages}</span><IconButton label="下一页" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}><ChevronRight size={17} /></IconButton></div><div className="button-row"><Button busy={busy} onClick={() => void save(true)}><ArrowDownToLine size={16} />导出为…</Button><Button variant="primary" busy={busy} onClick={() => void save(false)}>保存修改</Button></div></div></>}</div></Modal>;
}
