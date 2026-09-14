import { useCallback, useEffect, useRef, useState } from 'react';
import { CheckCircle2, FileText, FolderPlus, RefreshCw, Upload } from 'lucide-react';
import { errorText, native, rpc, type Collection, type Job } from '../api';
import { useApp } from '../context';
import ToolJobs from '../ToolJobs';
import { activeJob, Button, CheckField, Empty, Field, IconButton, Modal, Notice, Progress } from '../ui';

export interface LibraryIndex {
  collection_id: string; status: 'empty' | 'indexing' | 'ready' | 'keyword-only' | 'needs-index';
  ready: boolean; document_count: number; chunk_count: number; indexed_chunk_count: number;
  embedding_model: string; job_id?: string; progress?: number; message: string; warnings?: string[];
}
interface Document { id: string; name: string; status: string; pages?: number; path?: string; warnings?: string[] }
interface Props {
  collections: Collection[]; selected: string; locked: boolean;
  onSelect: (id: string) => void; onRefresh: () => Promise<void>; onClose: () => void;
}

export default function LiveLibrary({ collections, selected, locked, onSelect, onRefresh, onClose }: Props) {
  const { connected, jobs, run, track } = useApp();
  const [name, setName] = useState('');
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState('');
  const [visual, setVisual] = useState(false);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [index, setIndex] = useState<LibraryIndex>();
  const [failure, setFailure] = useState('');
  const request = useRef(0);
  const relatedJobs = jobs.filter(job => job.tool.startsWith('knowledge') && job.params?.collection_id === selected);
  const currentJob = relatedJobs.find(job => activeJob(job.status));
  const jobStamp = relatedJobs.map(job => `${job.id}:${job.status}:${job.progress}`).join('|');
  const indexing = !!currentJob || index?.status === 'indexing';
  const load = useCallback(async () => {
    const token = ++request.current;
    if (!selected) { setDocuments([]); setIndex(undefined); setFailure(''); return; }
    const results = await Promise.allSettled([rpc<{ documents: Document[] }>('knowledge.documents', { collection_id: selected }), rpc<LibraryIndex>('knowledge.index_status', { collection_id: selected })]);
    if (token !== request.current) return;
    if (results[0].status === 'fulfilled') setDocuments(results[0].value.documents);
    if (results[1].status === 'fulfilled') setIndex(results[1].value);
    const failed = results.find(result => result.status === 'rejected');
    setFailure(failed?.status === 'rejected' ? errorText(failed.reason) : '');
  }, [selected]);
  useEffect(() => { setDocuments([]); setIndex(undefined); return () => { request.current++; }; }, [load]);
  useEffect(() => { void load(); if (selected) void onRefresh(); }, [selected, jobStamp, load, onRefresh]);
  const create = async () => {
    setBusy('create');
    const result = await run(() => rpc<Collection>('knowledge.create', { name: name.trim() }));
    if (result) { await onRefresh(); onSelect(result.id); setName(''); setCreating(false); }
    setBusy('');
  };
  const ingest = async () => {
    const paths = await run(() => native.files(true, [{ name: '资料文档', extensions: ['pdf', 'pptx', 'docx', 'md', 'txt', 'png', 'jpg', 'jpeg'] }]));
    if (!paths?.length) return;
    setBusy('ingest');
    const job = await run(() => rpc<Job>('knowledge.ingest', { collection_id: selected, paths, visual }));
    if (job) { track(job, '正在导入并建立索引。完成后即可用于实时问答。'); void load(); }
    setBusy('');
  };
  const reindex = async () => {
    setBusy('reindex');
    const job = await run(() => rpc<Job>('knowledge.reindex', { collection_id: selected }));
    if (job) { track(job, '正在为当前模型重建索引。'); void load(); }
    setBusy('');
  };
  return <Modal title="资料库" onClose={onClose}><div className="modal-body live-library">
    <div className="library-selection"><Field label="使用资料库"><select disabled={locked || !connected} value={selected} onChange={event => onSelect(event.target.value)}><option value="">不使用资料库</option>{collections.map(collection => <option key={collection.id} value={collection.id}>{collection.name}</option>)}</select></Field><Button disabled={locked || !connected} onClick={() => setCreating(!creating)}><FolderPlus size={15} />新建</Button></div>
    {creating && <div className="library-create"><input aria-label="资料库名称" placeholder="输入资料库名称" disabled={locked} value={name} onChange={event => setName(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && name.trim() && !busy && !locked) void create(); }} /><Button variant="primary" disabled={locked || !name.trim()} busy={busy === 'create'} onClick={create}>创建</Button></div>}
    {locked && <p className="small-note">会话中使用已选资料库。停止会话后可切换或修改。</p>}
    {failure && <Notice tone="warning">{failure}</Notice>}
    {selected ? <>
      <div className="library-index-status"><div>{index?.ready ? <CheckCircle2 size={16} className="library-ready" /> : null}<span>{indexing ? currentJob?.message || index?.message || '正在建立索引…' : index?.message || '正在读取索引状态…'}</span></div><IconButton label="刷新索引状态" onClick={() => { void load(); void onRefresh(); }}><RefreshCw size={15} /></IconButton></div>
      {indexing && <Progress value={currentJob?.progress ?? index?.progress ?? 0} />}
      {index && <p className="small-note library-count">{index.document_count} 份文档 · {index.indexed_chunk_count} / {index.chunk_count} 段已索引</p>}
      {!indexing && index && ['keyword-only', 'needs-index'].includes(index.status) && <Button disabled={locked} busy={busy === 'reindex'} onClick={reindex}><RefreshCw size={14} />重建索引</Button>}
      <div className="library-documents">{documents.length ? documents.map(document => <div className="library-document" key={document.id}><FileText size={17} /><div><strong>{document.name}</strong><small>{document.pages ? `${document.pages} 页 · ` : ''}{document.status === 'ready' ? '已处理' : document.status === 'partial' ? '需查看提示' : document.status}</small>{document.warnings?.length ? <details className="details"><summary>处理提示</summary>{document.warnings.map((warning, i) => <p className="small-note" key={i}>{warning}</p>)}</details> : null}</div>{document.path && <Button variant="ghost" onClick={() => void run(() => native.open(document.path!))}>打开</Button>}</div>) : <Empty title={indexing ? '索引完成后显示文档' : '导入少量文档，预先建立索引'} />}</div>
      {!indexing && index?.warnings?.length ? <details className="details"><summary>索引提示</summary>{index.warnings.map((warning, i) => <p className="small-note" key={i}>{warning}</p>)}</details> : null}
      <div className="library-import"><CheckField disabled={locked || indexing} checked={visual} onChange={setVisual} label="识别图表与扫描页" /><Button variant="primary" disabled={locked || indexing || !connected} busy={busy === 'ingest'} onClick={ingest}><Upload size={15} />导入并建立索引</Button></div>
    </> : <p className="small-note">不使用资料库时，仍可直接提问。</p>}
    <ToolJobs scope="knowledge" collectionId={selected} title="索引处理" />
    <div className="modal-footer"><span className="small-note">导入时建立索引，提问时直接检索。</span><Button onClick={onClose}>完成</Button></div>
  </div></Modal>;
}
