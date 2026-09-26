import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDownToLine, ArrowUpFromLine, FolderOpen, Link2, RefreshCw, Save } from 'lucide-react';
import { errorText, native, rpc, type Job } from '../api';
import { useApp } from '../context';
import { activeJob, Button, CheckField, dateText, Empty, Field, Modal, Notice, Progress, Section } from '../ui';
import { emptyWebDavDraft, webdavBackupPasswordValid, webdavConnectionParams, webdavDirty, type WebDavConnection, type WebDavDraft, type WebDavSnapshot } from '../webdavState';
import '../webdav.css';

interface WebDavList { snapshots: WebDavSnapshot[]; remote_path: string }
interface WebDavResult { name?: string; ok?: boolean; message?: string; restart_recommended?: boolean; recovery_path?: string; warnings?: string[] }
type WebDavJob = Job & { result?: WebDavResult };
const done = (status: string) => ['completed', 'success', 'succeeded'].includes(status);
const sizeText = (value?: number) => value === undefined ? '' : value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(1)} GB` : value >= 1024 ** 2 ? `${(value / 1024 ** 2).toFixed(1)} MB` : `${Math.max(1, Math.round(value / 1024))} KB`;

export default function WebDavSync() {
  const { connected, jobs, track, refreshJobs, refreshSettings, settings } = useApp();
  const [saved, setSaved] = useState<WebDavConnection>();
  const [draft, setDraft] = useState<WebDavDraft>(emptyWebDavDraft);
  const [includeMedia, setIncludeMedia] = useState(true);
  const [includeModels, setIncludeModels] = useState(false);
  const [includeSecrets, setIncludeSecrets] = useState(true);
  const [backupPassword, setBackupPassword] = useState('');
  const [restorePassword, setRestorePassword] = useState('');
  const [snapshots, setSnapshots] = useState<WebDavSnapshot[]>([]);
  const [selected, setSelected] = useState('');
  const [listed, setListed] = useState(false);
  const [busy, setBusy] = useState('');
  const [cancelling, setCancelling] = useState(false);
  const [failure, setFailure] = useState('');
  const [message, setMessage] = useState('');
  const [restoreOpen, setRestoreOpen] = useState(false);
  const [lastJob, setLastJob] = useState('');
  const [recoveryPath, setRecoveryPath] = useState('');
  const [restart, setRestart] = useState(false);
  const mounted = useRef(true);
  const connectionRevision = useRef(0);
  const requested = useRef(new Map<string, { revision: number; kind: 'upload' | 'restore' }>());
  const handled = useRef(new Set<string>());
  const backgroundJobs = useMemo(() => jobs.filter(job => job.tool.startsWith('webdav.')) as WebDavJob[], [jobs]);
  const running = backgroundJobs.find(job => activeJob(job.status));
  const progressJob = running || backgroundJobs.find(job => job.id === lastJob);
  const dirty = webdavDirty(draft, saved);
  const optionsDirty = includeMedia !== (saved?.include_media !== false) || includeModels !== !!saved?.include_models || includeSecrets !== !!saved?.include_secrets;
  const locked = !!busy || !!running;
  const usable = connected && !!saved?.url && !dirty && !locked && !restart;
  const chosen = snapshots.find(snapshot => snapshot.name === selected);
  const report = useCallback((reason: unknown) => { if (mounted.current) setFailure(errorText(reason)); }, []);
  const cancelSync = async () => {
    if (!running || cancelling) return;
    setCancelling(true);
    try { await rpc('jobs.cancel', { id: running.id }); await refreshJobs(); }
    catch (reason) { report(reason); }
    finally { if (mounted.current) setCancelling(false); }
  };
  const applyConnection = useCallback((value: WebDavConnection) => {
    setSaved(value); setDraft({ url: value.url || '', remote_path: value.remote_path || '', username: value.username || '', password: '' });
  }, []);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (!connected) return;
    let disposed = false;
    void rpc<WebDavConnection>('webdav.get').then(value => {
      if (disposed) return;
      applyConnection(value); setIncludeMedia(value.include_media !== false); setIncludeModels(!!value.include_models); setIncludeSecrets(!!value.include_secrets);
    }).catch(report);
    return () => { disposed = true; };
  }, [connected, applyConnection, report]);
  const list = useCallback(async () => {
    const revision = connectionRevision.current;
    const result = await rpc<WebDavList>('webdav.list');
    if (mounted.current && revision === connectionRevision.current) {
      setSnapshots(result.snapshots || []); setListed(true);
      setSelected(old => result.snapshots.some(snapshot => snapshot.name === old) ? old : '');
    }
  }, []);
  useEffect(() => {
    for (const job of backgroundJobs) {
      const request = requested.current.get(job.id);
      if (!request || handled.current.has(job.id) || activeJob(job.status)) continue;
      handled.current.add(job.id);
      if (request.revision !== connectionRevision.current) continue;
      if (!done(job.status)) { report(job.error || job.message || '同步任务未完成，请重试。'); continue; }
      const result = job.result || {};
      if (request.kind === 'upload') {
        setMessage(result.name ? `快照已上传：${result.name}` : '快照已上传。');
        if (result.warnings?.length) setFailure(result.warnings.join('\n'));
        setBusy('list'); void list().catch(report).finally(() => { if (mounted.current) setBusy(''); });
      } else {
        setRestart(true); setMessage(result.message || '快照已恢复，请重启工具箱。'); setRecoveryPath(result.recovery_path || '');
        void refreshSettings().catch(report);
      }
    }
  }, [backgroundJobs, list, refreshSettings, report]);
  const save = async () => {
    setBusy('save'); setFailure(''); setMessage('');
    try {
      const result = await rpc<WebDavConnection>('webdav.save', { ...webdavConnectionParams(draft), include_media: includeMedia, include_models: includeModels, include_secrets: includeSecrets });
      if (!mounted.current) return;
      if (dirty) { connectionRevision.current++; setSnapshots([]); setSelected(''); setListed(false); }
      applyConnection(result); setMessage('连接与快照选项已保存。');
    } catch (reason) { report(reason); }
    finally { if (mounted.current) setBusy(''); }
  };
  const test = async () => {
    setBusy('test'); setFailure(''); setMessage('');
    try { const result = await rpc<{ ok: boolean; message: string }>('webdav.test'); if (!result.ok) throw new Error(result.message || '连接测试失败。'); if (mounted.current) setMessage(result.message || '连接成功。'); }
    catch (reason) { report(reason); }
    finally { if (mounted.current) setBusy(''); }
  };
  const refresh = async () => { setBusy('list'); setFailure(''); try { await list(); } catch (reason) { report(reason); } finally { if (mounted.current) setBusy(''); } };
  const upload = async () => {
    setBusy('upload'); setFailure(''); setMessage('');
    try {
      const job = await rpc<WebDavJob>('webdav.upload', { include_media: includeMedia, include_models: includeModels, include_secrets: includeSecrets, ...(backupPassword ? { backup_password: backupPassword } : {}) });
      if (!mounted.current) return;
      requested.current.set(job.id, { revision: connectionRevision.current, kind: 'upload' }); setLastJob(job.id); setBackupPassword(''); track(job, '正在上传 WebDAV 快照。');
    } catch (reason) { report(reason); }
    finally { if (mounted.current) setBusy(''); }
  };
  const restore = async () => {
    if (!chosen || !usable) return;
    setBusy('restore'); setFailure(''); setMessage('');
    try {
      const job = await rpc<WebDavJob>('webdav.restore', { name: chosen.name, ...(restorePassword ? { backup_password: restorePassword } : {}) });
      if (!mounted.current) return;
      requested.current.set(job.id, { revision: connectionRevision.current, kind: 'restore' }); setLastJob(job.id); setRestorePassword(''); setRestoreOpen(false); track(job, '正在恢复 WebDAV 快照。');
    } catch (reason) { report(reason); }
    finally { if (mounted.current) setBusy(''); }
  };
  const closeRestore = useCallback(() => { if (!busy) { setRestoreOpen(false); setRestorePassword(''); } }, [busy]);
  return <Section title="WebDAV 同步" className="webdav-sync">
    <fieldset className="plain-fieldset" disabled={!connected || !saved || locked || restart}>
      <div className="form-grid"><Field label="WebDAV 地址"><input type="url" value={draft.url} onChange={event => setDraft(value => ({ ...value, url: event.target.value }))} placeholder="https://dav.example.com" autoComplete="url" /></Field><Field label="远端目录"><input value={draft.remote_path} onChange={event => setDraft(value => ({ ...value, remote_path: event.target.value }))} placeholder="WinToolbox" /></Field></div>
      <div className="form-grid"><Field label="用户名"><input value={draft.username} onChange={event => setDraft(value => ({ ...value, username: event.target.value }))} autoComplete="username" /></Field><Field label="WebDAV 密码" hint={saved?.has_password ? '已保存密码；留空保持。更换地址或用户名时请重新填写。' : '密码加密保存在本机。'}><input type="password" autoComplete="new-password" value={draft.password} onChange={event => setDraft(value => ({ ...value, password: event.target.value }))} placeholder={saved?.has_password ? '已保存，留空保持' : '输入 WebDAV 密码'} /></Field></div>
    </fieldset>
    <div className="webdav-connection-actions"><span className="small-note">{dirty ? '请先保存连接，再上传或恢复。' : ''}</span><Button disabled={!connected || !saved || locked || !draft.url.trim() || (!dirty && !optionsDirty) || restart} busy={busy === 'save'} onClick={save}><Save size={14} />保存连接</Button><Button disabled={!usable} busy={busy === 'test'} onClick={test}><Link2 size={14} />测试连接</Button></div>
    <div className="section-divider" />
    <div className="inline-checks webdav-options"><CheckField checked={includeMedia} disabled={locked || restart} onChange={setIncludeMedia} label="包含媒体缓存" hint="录音、朗读与媒体结果" /><CheckField checked={includeModels} disabled={locked || restart} onChange={setIncludeModels} label="包含本地模型" /><CheckField checked={includeSecrets} disabled={locked || restart} onChange={setIncludeSecrets} label="同步 API 服务密钥" hint="包含百炼及“其他 API 服务”的密钥，随配置加密同步。" /></div>
    {!includeSecrets && settings?.providers.some(provider => provider.has_key) && <Notice>当前只同步服务配置，不含 API 密钥；另一台电脑恢复后需要重新填写密钥。</Notice>}
    <div className="webdav-upload"><Field label={includeSecrets ? '备份密码（必填）' : '备份密码（可选）'} hint={includeSecrets ? '至少 8 位；另一台电脑恢复时输入同一密码，密钥会重新加密保存在接收电脑。' : '填写后加密快照，至少 8 位；与 WebDAV 密码不同。'}><input type="password" autoComplete="new-password" disabled={locked || restart} value={backupPassword} onChange={event => setBackupPassword(event.target.value)} placeholder={includeSecrets ? '至少 8 位' : '留空不加密'} /></Field><Button variant="primary" disabled={!usable || !webdavBackupPasswordValid(includeSecrets, backupPassword)} busy={busy === 'upload'} onClick={upload}><ArrowUpFromLine size={15} />上传快照</Button></div>
    {progressJob && activeJob(progressJob.status) && <div className="webdav-progress"><span>{progressJob.message || '正在同步…'}</span><Button variant="ghost" busy={cancelling} disabled={!connected || progressJob.status === 'cancelling'} onClick={cancelSync}>取消</Button><Progress value={progressJob.progress || 0} /></div>}
    {failure && <Notice tone="warning"><span className="webdav-message">{failure}</span></Notice>}
    {message && <Notice tone="success"><span className="webdav-message">{message}</span>{restart && <p>请重启工具箱以加载恢复的数据。</p>}{recoveryPath && <button className="inline-link" onClick={() => void native.open(recoveryPath).catch(report)}><FolderOpen size={13} />打开恢复前备份</button>}</Notice>}
    <div className="webdav-snapshots-heading"><h3>远端快照</h3><Button disabled={!usable} busy={busy === 'list'} onClick={refresh}><RefreshCw size={14} />{listed ? '刷新列表' : '读取快照'}</Button></div>
    {snapshots.length ? <div className="webdav-snapshot-list">{snapshots.map(snapshot => <label className="webdav-snapshot" key={snapshot.name}><input type="radio" name="webdav-snapshot" disabled={locked || dirty || restart} checked={selected === snapshot.name} onChange={() => setSelected(snapshot.name)} /><span><strong>{snapshot.name}</strong><small>{[dateText(snapshot.modified_at || undefined), sizeText(snapshot.size), snapshot.encrypted ? '已加密' : ''].filter(Boolean).join(' · ')}</small></span></label>)}</div> : <Empty title={listed ? '暂无远端快照' : '读取快照后选择要恢复的版本'} />}
    <div className="webdav-restore-action"><Button disabled={!usable || !chosen} onClick={() => { setRestorePassword(''); setRestoreOpen(true); }}><ArrowDownToLine size={15} />恢复选中快照</Button></div>
    {restoreOpen && chosen && <Modal title="恢复 WebDAV 快照" onClose={closeRestore}><div className="modal-body"><p className="break-word">{chosen.name}</p><Notice tone="warning">恢复会覆盖当前数据。工具箱会先自动创建本地备份；恢复完成后需要重启。</Notice><Field label={chosen.encrypted ? '快照密码' : '快照密码（如已加密）'}><input type="password" autoComplete="off" disabled={!!busy} value={restorePassword} onChange={event => setRestorePassword(event.target.value)} placeholder={chosen.encrypted ? '输入上传时设置的备份密码' : '未加密可留空'} /></Field><div className="modal-footer"><Button disabled={!!busy} onClick={closeRestore}>取消</Button><Button variant="danger" disabled={!usable || (chosen.encrypted && !restorePassword)} busy={busy === 'restore'} onClick={restore}>确认恢复</Button></div></div></Modal>}
  </Section>;
}
