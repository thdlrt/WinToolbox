import { useEffect, useRef, useState } from 'react';
import { getCurrentWebviewWindow } from '@tauri-apps/api/webviewWindow';
import { ArrowUp, Download, Folder, FolderPlus, RefreshCw, Settings2, Trash2, Upload } from 'lucide-react';
import { isDesktop, native, rpc, type Job } from '../api';
import { useApp } from '../context';
import { activeJob, Button, CheckField, dateText, Empty, Field, Notice, Section, statusLabel } from '../ui';
import '../relay.css';

interface Entry { path: string; name: string; directory: boolean; size: number; modified: number | null; etag: string }
interface Config { url: string; username: string; remote_path: string; download_dir: string; has_password: boolean; configured: boolean; shared_configured?: boolean; migration?: { pending?: boolean; warning?: string; legacy_path?: string }; context_menu: { enabled: boolean; supported: boolean } }
interface Plan { token: string; files: Entry[]; bytes: number; skipped: number; days: number }
interface Result { path?: string; entries?: Entry[]; paths?: string[]; directory?: string; uploaded?: string[]; moved?: string[]; errors?: { path: string; message: string }[]; removed?: string[]; skipped?: string[]; message?: string }
interface RelayJob extends Job { result?: Omit<Result, 'skipped'> & { skipped?: number | string[]; token?: string; files?: Entry[]; bytes?: number; days?: number } }
const bytes = (n: number) => n < 1024 ? `${n} B` : n < 1024 ** 2 ? `${(n / 1024).toFixed(1)} KB` : n < 1024 ** 3 ? `${(n / 1024 ** 2).toFixed(1)} MB` : `${(n / 1024 ** 3).toFixed(2)} GB`;
const initial: Config = { url: '', username: '', remote_path: '', download_dir: '', has_password: false, configured: false, context_menu: { enabled: false, supported: false } };
function jobMessage(job: RelayJob) {
  if (job.status !== 'completed') return `${statusLabel(job.status)} · ${job.message || ''}`;
  if (job.result?.uploaded) return `上传 ${job.result.uploaded.length} 个文件${job.result.errors?.length ? `，${job.result.errors.length} 项未完成（查看详情）` : '，已完成'}`;
  if (job.result?.entries) return `已列出 ${job.result.entries.length} 项`;
  return '已完成';
}

export default function RelayPage() {
  const { connected, jobs, run, track, error, navigate } = useApp();
  const [config, setConfig] = useState(initial);
  const [settings, setSettings] = useState(false);
  const [logs, setLogs] = useState(false);
  const [logDetail, setLogDetail] = useState<RelayJob>();
  const [dragStatus, setDragStatus] = useState('');
  const [cacheJob, setCacheJob] = useState<RelayJob>();
  const dragCache = useRef(new Map<string, { paths: string[]; until: number }>());
  const outgoing = useRef(false);
  const pointer = useRef<{ x: number; y: number; entry: Entry; moved: boolean } | undefined>(undefined);
  const pendingCache = useRef(new Map<string, Promise<string[]>>());
  const startNativeDrag = async (paths: string[]) => {
    if (outgoing.current) return;
    outgoing.current = true; pointer.current = undefined;
    try { await native.dragFiles(paths); } catch (e) { error(e); }
    finally { outgoing.current = false; setDragging(false); }
  };
  const prepareDrag = (entry: Entry) => {
    const targets = selected.includes(entry.path) ? entries.filter(e => selected.includes(e.path)) : [entry];
    const key = JSON.stringify([config.url, config.username, config.remote_path, targets.map(e => [e.path, e.etag, e.modified, e.size])]);
    const cached = dragCache.current.get(key);
    if (cached && cached.until > Date.now()) return { key, ready: cached.paths, promise: Promise.resolve(cached.paths) };
    let promise = pendingCache.current.get(key);
    if (!promise) {
      setDragStatus('正在下载到本机，准备好后可拖出…');
      promise = (async () => {
        let task = await rpc<RelayJob>('relay.prepare_drag', { paths: targets.map(e => e.path) });
        setCacheJob(task);
        while (activeJob(task.status)) {
          await new Promise(resolve => setTimeout(resolve, 300));
          task = await rpc<RelayJob>('jobs.get', { id: task.id }); setCacheJob(task);
        }
        if (task.status !== 'completed' || !task.result?.paths?.length) throw new Error(String(task.error || task.message || '缓存未完成'));
        dragCache.current.set(key, { paths: task.result.paths, until: Date.now() + 60000 });
        setDragStatus('已准备好，按住文件名拖到其他软件或文件夹。');
        return task.result.paths;
      })().finally(() => pendingCache.current.delete(key));
      pendingCache.current.set(key, promise);
    }
    return { key, ready: undefined, promise };
  };
  const [path, setPath] = useState('');
  const pathRef = useRef(path); pathRef.current = path;
  const [entries, setEntries] = useState<Entry[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [query, setQuery] = useState('');
  const [ascending, setAscending] = useState(false);
  const [move, setMove] = useState(false);
  const [days, setDays] = useState(7);
  const [plan, setPlan] = useState<Plan>();
  const [job, setJob] = useState<RelayJob>();
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState('');
  const [dragging, setDragging] = useState(false);
  const openAfter = useRef(false);
  const observedUploads = useRef(new Set(jobs.filter(j => j.tool === 'relay.upload' && !activeJob(j.status)).map(j => j.id)));
  const uploadRef = useRef<(paths: string[]) => void>(() => {});
  const listRef = useRef<(next: string) => Promise<void>>(async () => {});
  const submit = async (method: string, params: Record<string, unknown> = {}) => {
    setBusy(true); setReport('');
    const result = await run(() => rpc<RelayJob>('relay.' + method, params));
    if (result) { setJob(result); track(result); }
    setBusy(false);
  };
  const list = async (next = path) => { setSelected([]); setPlan(undefined); await submit('list', { path: next }); };
  const saveConnection = async (test = false) => {
    setBusy(true);
    try {
      const value = await run(() => rpc<Config>('relay.save', { download_dir: config.download_dir }));
      if (!value) return;
      setConfig(value); setPlan(undefined); setEntries([]); setSelected([]); setPath(''); setReport('设置已保存');
      if (test) await submit('test');
    } finally { setBusy(false); }
  };
  listRef.current = list;
  useEffect(() => {
    if (connected) void run(async () => { const value = await rpc<Config>('relay.get'); setConfig(value); setSettings(!value.configured); if (value.configured) await listRef.current(''); });
  }, [connected]);
  const working = busy || !!job && activeJob(job.status);
  const upload = async (paths: string[]) => {
    if (!paths.length || working) return;
    if (move && !window.confirm(`上传并下载校验成功后，移除本地原文件（${paths.length} 项）。继续？`)) return;
    setPlan(undefined); await submit('upload', { paths, path, move });
  };
  uploadRef.current = paths => { void upload(paths); };
  useEffect(() => {
    if (!isDesktop()) return;
    let disposed = false; let off: (() => void) | undefined;
    void getCurrentWebviewWindow().onDragDropEvent(event => {
      setDragging(event.payload.type === 'over' || event.payload.type === 'enter');
      if (event.payload.type === 'drop' && !outgoing.current) uploadRef.current(event.payload.paths);
    }).then(unlisten => { if (disposed) unlisten(); else off = unlisten; }).catch(error);
    return () => { disposed = true; off?.(); };
  }, [error]);
  useEffect(() => {
    if (!job || !activeJob(job.status)) return;
    let disposed = false;
    const timer = setInterval(() => {
      void rpc<RelayJob>('jobs.get', { id: job.id }).then(async value => {
        if (disposed) return;
        setJob(value);
        if (value.status === 'completed') {
          void rpc<Config>('relay.get').then(fresh => setConfig(current => ({ ...current, migration: fresh.migration }))).catch(error);
          const result = value.result || {};
          if (value.tool === 'relay.list') { setPath(result.path || ''); setEntries(result.entries || []); }
          else if (value.tool === 'relay.cleanup_preview') setPlan(result as Plan);
          else if (value.tool === 'relay.download') {
            setReport(`已下载 ${result.paths?.length || 0} 个文件到 ${result.directory}`);
            if (openAfter.current && result.paths?.[0]) await run(() => native.open(result.paths![0]));
            openAfter.current = false;
          } else if (value.tool === 'relay.upload') {
            setReport('');
            // Read listing separately to preserve the transfer result and its errors.
            const scan = await rpc<RelayJob>('relay.list', { path });
            setRefreshJob(scan.id);
          } else if ((value.tool === 'relay.cleanup' || value.tool === 'relay.delete')) {
            setReport(`已清理 ${result.removed?.length || 0} 个文件；${Array.isArray(result.skipped) ? result.skipped.length : 0} 个已变化项目跳过。`);
            const scan = await rpc<RelayJob>('relay.list', { path }); setRefreshJob(scan.id);
          } else if (value.tool === 'relay.test') { setReport(result.message || '连接成功'); setPath(''); setEntries(result.entries || []); }
          else if (value.tool === 'relay.mkdir') { const scan = await rpc<RelayJob>('relay.list', { path }); setRefreshJob(scan.id); }
        } else if (value.status === 'failed') { if (value.tool !== 'relay.upload') error(value.error || value.message); openAfter.current = false; }
      }).catch(error);
    }, 600);
    return () => { disposed = true; clearInterval(timer); };
  }, [job?.id, job?.status]);
  const [refreshJob, setRefreshJob] = useState<string>();
  useEffect(() => {
    if (!config.configured) return;
    let changed = false;
    for (const entry of jobs) {
      if (entry.tool === 'relay.upload' && !activeJob(entry.status) && !observedUploads.current.has(entry.id)) {
        observedUploads.current.add(entry.id);
        if (entry.id !== job?.id) changed = true;
      }
    }
    if (changed) void rpc<RelayJob>('relay.list', { path: pathRef.current }).then(value => setRefreshJob(value.id)).catch(error);
  }, [jobs, config.configured, job?.id, error]);
  useEffect(() => {
    if (!refreshJob) return;
    let disposed = false;
    const timer = setInterval(() => void rpc<RelayJob>('jobs.get', { id: refreshJob }).then(value => {
      if (disposed || activeJob(value.status)) return;
      setRefreshJob(undefined);
      if (value.status === 'completed') { if (value.result?.path === pathRef.current) setEntries(value.result?.entries || []); } else error(value.error || value.message);
    }).catch(error), 600);
    return () => { disposed = true; clearInterval(timer); };
  }, [refreshJob]);
  const remove = (targets: Entry[]) => {
    if (!targets.length || working) return;
    if (window.confirm(`永久删除这 ${targets.length} 个远端文件？不会删除本地副本。\n${targets.slice(0, 6).map(e => e.name).join('\n')}`)) void submit('delete', { entries: targets });
  };
  const download = (paths: string[], open = false) => { openAfter.current = open; void submit('download', { paths }); };
  const visible = entries.filter(e => !e.name.startsWith('.relay-upload-') && e.name.toLocaleLowerCase().includes(query.toLocaleLowerCase())).sort((a, b) => Number(b.directory) - Number(a.directory) || (ascending ? 1 : -1) * ((a.modified || 0) - (b.modified || 0)) || a.name.localeCompare(b.name));
  const background = jobs.filter(j => j.tool === 'relay.upload');
  if (logs) return <>
    <div className="relay-toolbar"><Button onClick={() => { setLogs(false); setLogDetail(undefined); }}>返回中转文件</Button></div>
    <Section title="上传日志">{background.length ? background.map(item => <div className="relay-log-row" key={item.id}>
      <span>{dateText(item.created_at)}</span><span>{jobMessage(item as RelayJob)}</span>
      <Button onClick={() => void run(async () => setLogDetail(await rpc<RelayJob>('jobs.get', { id: item.id })))}>详情</Button>
      {activeJob(item.status) && <Button onClick={() => void run(() => rpc('jobs.cancel', { id: item.id }))}>取消</Button>}
      {!activeJob(item.status) && <Button disabled={working || !config.configured} onClick={() => void submit('upload', { paths: item.params.paths, path: item.params.path || '', move: false })}>重新上传</Button>}
    </div>) : <Empty title="暂无上传记录" />}</Section>
    {logDetail && <Section title="上传详情"><p>{jobMessage(logDetail)}</p><p>{String(logDetail.error || '')}</p>
      {logDetail.result?.uploaded?.map(name => <div key={name}>已上传：{name}</div>)}
      {logDetail.result?.errors?.map((item, index) => <Notice key={index} tone="warning">{item.path}：{item.message}</Notice>)}
      <div className="muted">{(logDetail.params.paths as string[] || []).map(name => <div key={name}>{name}</div>)}</div>
    </Section>}
  </>;
  return <>
    <div className="relay-toolbar"><Button onClick={() => { setLogs(true); setReport(''); }}>上传日志{background.some(item => activeJob(item.status)) ? '（上传中）' : ''}</Button><Button onClick={() => setSettings(!settings)}><Settings2 size={16} />下载与右键菜单</Button><Button disabled={!config.download_dir} onClick={() => void run(() => native.open(config.download_dir))}>打开本机下载目录</Button></div>
    {settings && <Section title="中转设置">
      <p className="muted">{config.configured ? '使用统一 WebDAV 连接。' : '尚未配置统一 WebDAV 连接。'} <button className="inline-link" onClick={() => { sessionStorage.setItem('wintoolbox-settings-tab', 'data'); navigate('settings'); }}>管理 WebDAV 连接</button></p>
      {config.remote_path && <p className="small-note">云端目录：{config.remote_path}</p>}
      <div className="form-grid">
      <Field label="本机下载目录"><div className="button-row"><input value={config.download_dir} onChange={e => setConfig({ ...config, download_dir: e.target.value })} /><Button onClick={() => void run(async () => { const chosen = await native.directory(); if (chosen) setConfig({ ...config, download_dir: chosen }); })}>选择</Button></div></Field>
    </div><div className="section-footer"><Button disabled={!connected || working} onClick={() => void saveConnection()}>保存设置</Button><Button variant="primary" disabled={!connected || working || !config.configured} onClick={() => void saveConnection(true)}>保存并测试连接</Button></div>
      <div className="section-footer"><span className="muted">安装或首次启动自动添加菜单。右键 → 显示更多选项 → 发送到 → 文件中转站；也可使用独立的“发送到文件中转站”。</span><Button disabled={!config.context_menu.supported || working} onClick={() => void run(async () => { const menu = await rpc<Config['context_menu']>('relay.context_menu', { enabled: !config.context_menu.enabled }); setConfig({ ...config, context_menu: menu }); })}>{config.context_menu.enabled ? '移除右键菜单' : '添加右键菜单'}</Button></div>
    </Section>}
    {config.migration?.warning && <Notice tone="warning">{config.migration.warning}</Notice>}
    {config.migration?.pending && !config.migration.warning && <Notice>旧中转文件将在连接时复制到统一目录，原文件保留。</Notice>}
    <Section title="中转文件" action={<Button disabled={!config.configured || working} onClick={() => void list()}><RefreshCw size={16} />刷新</Button>}>
      <nav className="relay-breadcrumb" aria-label="当前远端目录"><button disabled={working} onClick={() => void list('')}>中转根目录</button>{path.split('/').filter(Boolean).map((part, index, all) => <span key={index}> / <button disabled={working} onClick={() => void list(all.slice(0, index + 1).join('/'))}>{part}</button></span>)}</nav>
      <div className="relay-toolbar"><Button disabled={!path || working} onClick={() => void list(path.split('/').slice(0, -1).join('/'))}><ArrowUp size={16} />上一级</Button><Button disabled={!config.configured || working} onClick={() => void run(async () => { const paths = await native.files(true); await upload(paths); })}><Upload size={16} />上传文件</Button><Button disabled={!config.configured || working} onClick={() => void run(async () => { const directory = await native.directory(); if (directory) await upload([directory]); })}>上传文件夹</Button><Button disabled={!config.configured || working} onClick={() => { const name = window.prompt('新文件夹名称'); if (name) void submit('mkdir', { path: path ? path + '/' + name : name }); }}><FolderPlus size={16} />新建文件夹</Button><Button disabled={!selected.length || working} onClick={() => download(selected)}><Download size={16} />下载所选（{selected.length}）</Button><Button variant="danger" disabled={working || !selected.length || entries.some(e => selected.includes(e.path) && e.directory)} onClick={() => remove(entries.filter(e => selected.includes(e.path)))}><Trash2 size={16} />删除所选</Button></div>
      <div className="relay-toolbar"><input aria-label="筛选文件" placeholder="筛选当前目录文件名" value={query} onChange={e => setQuery(e.target.value)} /><select aria-label="文件时间排序" value={ascending ? 'old' : 'new'} onChange={e => setAscending(e.target.value === 'old')}><option value="new">最新在前</option><option value="old">最早在前</option></select><CheckField checked={move} onChange={setMove} label="上传校验后移走本地文件" /></div>
      <div className={`relay-drop ${dragging ? 'dragging' : ''}`}><span className="muted">拖入上传；按住文件名拖出到其他软件，首次需下载缓存。双击打开。</span>
      {visible.length ? <div className="relay-files" role="table" aria-label="远端文件列表"><div className="relay-row relay-head" role="row"><input type="checkbox" aria-label="全选当前显示项目" checked={visible.length > 0 && visible.every(e => selected.includes(e.path))} onChange={e => setSelected(e.target.checked ? visible.map(v => v.path) : [])} /><span>名称（双击打开）</span><span>大小</span><span>修改时间</span><span /></div>{visible.map(entry => <div className="relay-row" role="row" key={entry.path} onDoubleClick={() => { if (!working) entry.directory ? void list(entry.path) : download([entry.path], true); }}><input type="checkbox" aria-label={`选择 ${entry.name}`} checked={selected.includes(entry.path)} onChange={e => setSelected(e.target.checked ? [...selected, entry.path] : selected.filter(p => p !== entry.path))} /><button className="relay-name" disabled={working} title="双击打开；按住拖到其他软件（首次需缓存）" onPointerDown={event => {
        if (event.button !== 0 || !isDesktop()) return;
        event.currentTarget.setPointerCapture(event.pointerId);
        pointer.current = { x: event.clientX, y: event.clientY, entry, moved: false };
      }} onPointerMove={event => {
        const current = pointer.current;
        if (!current || current.moved || !(event.buttons & 1) || Math.hypot(event.clientX - current.x, event.clientY - current.y) < 6) return;
        current.moved = true;
        const prepared = prepareDrag(current.entry);
        if (prepared.ready) void startNativeDrag(prepared.ready);
        else void prepared.promise.then(paths => { if (pointer.current === current) void startNativeDrag(paths); }).catch(e => { pointer.current = undefined; setDragStatus('缓存失败，请重试'); error(e); });
      }} onPointerUp={() => { pointer.current = undefined; }} onPointerCancel={() => { pointer.current = undefined; }} onClick={() => { if (!entry.directory) setSelected([entry.path]); }}>{entry.directory && <Folder size={16} />}<span>{entry.name}</span></button><span>{entry.directory ? '—' : bytes(entry.size)}</span><span>{dateText(entry.modified || undefined)}</span><div className="relay-row-actions"><Button disabled={working} onClick={() => download([entry.path])}>下载</Button>{!entry.directory && <Button variant="danger" disabled={working} onClick={() => remove([entry])}>删除</Button>}</div></div>)}</div> : <Empty title={config.configured ? '当前目录没有显示的文件' : '先在设置中配置统一 WebDAV 连接'} />}
      </div>
    </Section>
    {job && job.tool !== 'relay.upload' && job.tool !== 'relay.list' && <Notice tone={job.status === 'failed' ? 'warning' : 'info'}>{jobMessage(job)}{activeJob(job.status) && <button className="inline-link" onClick={() => void run(() => rpc('jobs.cancel', { id: job.id }))}>取消</button>}</Notice>}
    {report && job?.tool !== 'relay.upload' && <Notice>{report}</Notice>}
    {dragStatus && <Notice>{dragStatus}{cacheJob && activeJob(cacheJob.status) && <> {Math.round(cacheJob.progress)}% <button className="inline-link" onClick={() => void run(() => rpc('jobs.cancel', { id: cacheJob.id }))}>取消</button></>}</Notice>}
    <Section title="按时间清理"><div className="relay-toolbar"><span>清理整个中转目录中超过</span><select aria-label="清理保留时间" value={days} onChange={e => { setDays(Number(e.target.value)); setPlan(undefined); }}><option value={1}>1 天</option><option value={7}>1 周</option><option value={30}>30 天</option><option value={90}>90 天</option></select><span>未修改的文件</span><Button disabled={!config.configured || working} onClick={() => { setPlan(undefined); void submit('cleanup_preview', { days }); }}><Trash2 size={16} />预览清理</Button></div>
      {plan && <><Notice tone="warning">将永久删除 {plan.files.length} 个文件，释放 {bytes(plan.bytes)}。空文件夹保留。{plan.skipped > 0 ? `${plan.skipped} 个缺少可靠时间或版本标识的文件不会清理。` : ''}</Notice><div className="relay-cleanup-list">{plan.files.map(entry => <div key={entry.path}>{entry.path} · {bytes(entry.size)} · {dateText(entry.modified || undefined)}</div>)}</div><Button variant="danger" disabled={working || !plan.files.length} onClick={() => { if (window.confirm(`永久删除预览中的 ${plan.files.length} 个远端文件？`)) { void submit('cleanup', { token: plan.token }); setPlan(undefined); } }}>确认清理 {plan.files.length} 个文件</Button></>}
    </Section>
  </>;
}
