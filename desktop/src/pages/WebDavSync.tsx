import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDownToLine, ArrowUpFromLine, FolderOpen, Link2, RefreshCw, Save } from 'lucide-react';
import { errorText, native, rpc, type Job } from '../api';
import { useApp } from '../context';
import { activeJob, Button, dateText, Empty, Field, Modal, Notice, Progress, Section } from '../ui';
import { emptyWebDavDraft, webdavLegacyPasswordRequired, webdavRestoreParams, webdavConnectionParams, webdavDirty, type WebDavConnection, type WebDavDraft, type WebDavSnapshot, webdavSnapshotKey } from '../webdavState';
import '../webdav.css';

interface WebDavPreview { token: string; source_platform: string; providers: unknown[]; secret_count: number; same_platform: boolean; message?: string }
interface WebDavList { snapshots: WebDavSnapshot[]; remote_path: string }
interface WebDavResult { name?: string; ok?: boolean; message?: string; restart_recommended?: boolean; recovery_path?: string; warnings?: string[] }
type WebDavJob = Job & { result?: WebDavResult };
const done = (status: string) => ['completed', 'success', 'succeeded'].includes(status);
const sizeText = (value?: number) => value === undefined ? '' : value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(1)} GB` : value >= 1024 ** 2 ? `${(value / 1024 ** 2).toFixed(1)} MB` : `${Math.max(1, Math.round(value / 1024))} KB`;

export default function WebDavSync() {
  const { connected, jobs, track, refreshJobs, refreshSettings } = useApp();
  const [saved, setSaved] = useState<WebDavConnection>();
  const [draft, setDraft] = useState<WebDavDraft>(emptyWebDavDraft);
  const [preview, setPreview] = useState<WebDavPreview>();
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
  const locked = !!busy || !!running;
  const usable = connected && !!saved?.configured && !dirty && !locked && !restart;
  const chosen = snapshots.find(snapshot => webdavSnapshotKey(snapshot) === selected);
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
      applyConnection(value);
    }).catch(report);
    return () => { disposed = true; };
  }, [connected, applyConnection, report]);
  const list = useCallback(async () => {
    const revision = connectionRevision.current;
    const result = await rpc<WebDavList>('webdav.list');
    if (mounted.current && revision === connectionRevision.current) {
      setSnapshots(result.snapshots || []); setListed(true);
      setSelected(old => result.snapshots.some(snapshot => webdavSnapshotKey(snapshot) === old) ? old : '');
    }
  }, []);
  useEffect(() => {
    for (const job of backgroundJobs) {
      const request = requested.current.get(job.id);
      if (!request || handled.current.has(job.id) || activeJob(job.status)) continue;
      handled.current.add(job.id);
      if (request.revision !== connectionRevision.current) continue;
      if (!done(job.status)) { report(job.error || job.message || '配置备份任务未完成，请重试。'); continue; }
      const result = job.result || {};
      if (request.kind === 'upload') {
        setMessage(result.name ? `配置备份已上传：${result.name}` : '配置备份已上传。');
        if (result.warnings?.length) setFailure(result.warnings.join('\n'));
        setBusy('list'); void list().catch(report).finally(() => { if (mounted.current) setBusy(''); });
      } else {
        setRestart(result.restart_recommended !== false); setMessage(result.message || (result.restart_recommended === false ? '配置已恢复。' : '配置已恢复，请重启工具箱。')); setRecoveryPath(result.recovery_path || '');
        void refreshSettings().catch(report);
      }
    }
  }, [backgroundJobs, list, refreshSettings, report]);
  const save = async () => {
    setBusy('save'); setFailure(''); setMessage('');
    try {
      const result = await rpc<WebDavConnection>('webdav.save', webdavConnectionParams(draft));
      if (!mounted.current) return;
      if (dirty) { connectionRevision.current++; setSnapshots([]); setSelected(''); setListed(false); }
      applyConnection(result); setMessage('连接已保存。');
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
      const job = await rpc<WebDavJob>('webdav.upload');
      if (!mounted.current) return;
      requested.current.set(job.id, { revision: connectionRevision.current, kind: 'upload' }); setLastJob(job.id); track(job, '正在上传云端配置备份。');
    } catch (reason) { report(reason); }
    finally { if (mounted.current) setBusy(''); }
  };
  const openRestore = async () => {
    if (!chosen || !usable) return;
    setRestorePassword(''); setPreview(undefined); setFailure('');
    if (chosen.kind !== 'unified' && chosen.kind !== 'legacy_ai' && chosen.location !== 'legacy_ai') { setRestoreOpen(true); return; }
    const revision = connectionRevision.current; setBusy('preview');
    try { const value = await rpc<WebDavPreview>('webdav.preview', webdavRestoreParams(chosen)); if (!value.token) throw new Error('备份预览不完整，请重试。'); if (mounted.current && revision === connectionRevision.current) { setPreview(value); setRestoreOpen(true); } }
    catch (reason) { report(reason); }
    finally { if (mounted.current) setBusy(''); }
  };
  const restore = async () => {
    if (!chosen || !usable) return;
    setBusy('restore'); setFailure(''); setMessage('');
    try {
      const job = await rpc<WebDavJob>('webdav.restore', { ...webdavRestoreParams(chosen, restorePassword), ...(preview ? { preview_token: preview.token } : {}) });
      if (!mounted.current) return;
      requested.current.set(job.id, { revision: connectionRevision.current, kind: 'restore' }); setLastJob(job.id); setRestorePassword(''); setRestoreOpen(false); track(job, '正在恢复云端配置备份。');
    } catch (reason) { report(reason); }
    finally { if (mounted.current) setBusy(''); }
  };
  const closeRestore = useCallback(() => { if (!busy) { setRestoreOpen(false); setRestorePassword(''); } }, [busy]);
  return <><Section title="统一 WebDAV 连接" className="webdav-sync">
    <fieldset className="plain-fieldset" disabled={!connected || !saved || locked || restart}>
      <div className="form-grid"><Field label="WebDAV 地址"><input type="url" value={draft.url} onChange={event => setDraft(value => ({ ...value, url: event.target.value }))} placeholder="https://dav.example.com" autoComplete="url" /></Field><Field label="根目录" hint="各功能会自动使用此目录下的专用文件夹。"><input value={draft.remote_path} onChange={event => setDraft(value => ({ ...value, remote_path: event.target.value }))} placeholder="WinToolbox" /></Field></div>
      <div className="form-grid"><Field label="用户名"><input value={draft.username} onChange={event => setDraft(value => ({ ...value, username: event.target.value }))} autoComplete="username" /></Field><Field label="WebDAV 密码" hint={saved?.has_password ? '已保存密码；留空保持。更换地址或用户名时请重新填写。' : '密码加密保存在本机。'}><input type="password" autoComplete="new-password" value={draft.password} onChange={event => setDraft(value => ({ ...value, password: event.target.value }))} placeholder={saved?.has_password ? '已保存，留空保持' : '输入 WebDAV 密码'} /></Field></div>
    </fieldset>
    <div className="webdav-connection-actions"><span className="small-note">{dirty ? '请先保存连接，再上传或恢复。' : ''}</span><Button disabled={!connected || !saved || locked || !draft.url.trim() || !dirty || restart} busy={busy === 'save'} onClick={save}><Save size={14} />保存连接</Button><Button disabled={!usable} busy={busy === 'test'} onClick={test}><Link2 size={14} />测试连接</Button></div>
    </Section>
    {progressJob && activeJob(progressJob.status) && <div className="webdav-progress"><span>{progressJob.message || '正在处理配置备份…'}</span><Button variant="ghost" busy={cancelling} disabled={!connected || progressJob.status === 'cancelling'} onClick={cancelSync}>取消</Button><Progress value={progressJob.progress || 0} /></div>}
    {failure && <Notice tone="warning"><span className="webdav-message">{failure}</span></Notice>}
    {message && <Notice tone="success"><span className="webdav-message">{message}</span>{restart && <p>请重启工具箱以加载恢复的配置。</p>}{recoveryPath && <button className="inline-link" onClick={() => void native.open(recoveryPath).catch(report)}><FolderOpen size={13} />打开恢复前备份</button>}</Notice>}
    <Section title="配置备份与恢复" className="webdav-sync">
    <p className="small-note">备份应用设置、AI 模型与密钥，统一使用已保存的 WebDAV 密码加密。请保留此密码。跨平台恢复共享 AI 配置，同平台也恢复本机设置。</p>
    <div className="webdav-upload"><Button variant="primary" disabled={!usable} busy={busy === 'upload'} onClick={upload}><ArrowUpFromLine size={15} />上传配置备份</Button></div>
    <div className="webdav-snapshots-heading"><h3>云端配置备份</h3><Button disabled={!usable} busy={busy === 'list'} onClick={refresh}><RefreshCw size={14} />{listed ? '刷新列表' : '读取备份'}</Button></div>
    {snapshots.length ? <div className="webdav-snapshot-list">{snapshots.map(snapshot => <label className="webdav-snapshot" key={webdavSnapshotKey(snapshot)}><input type="radio" name="webdav-snapshot" disabled={locked || dirty || restart} checked={selected === webdavSnapshotKey(snapshot)} onChange={() => setSelected(webdavSnapshotKey(snapshot))} /><span><strong>{snapshot.name}</strong><small>{[dateText(snapshot.modified_at || undefined), sizeText(snapshot.size), snapshot.encrypted ? '已加密' : '', snapshot.kind === 'legacy_ai' ? '旧版共享 AI 配置' : snapshot.source_platform === 'android' ? 'Android 配置' : snapshot.source_platform === 'windows' ? 'Windows 配置' : snapshot.scope === 'legacy_full' ? '旧版备份 · 仅恢复配置' : '旧版配置备份'].filter(Boolean).join(' · ')}</small></span></label>)}</div> : <Empty title={listed ? '暂无云端配置备份' : '读取备份后选择要恢复的版本'} />}
    <div className="webdav-restore-action"><Button disabled={!usable || !chosen} busy={busy === 'preview'} onClick={() => void openRestore()}><ArrowDownToLine size={15} />恢复选中配置</Button></div>
    {restoreOpen && chosen && <Modal title="恢复云端配置备份" onClose={closeRestore}><div className="modal-body"><p className="break-word">{chosen.name}</p><Notice tone="warning">{preview ? (preview.same_platform ? '将覆盖本机应用设置、AI 服务、模型与密钥。' : '将覆盖本机共享 AI 服务、模型与密钥。') : chosen.source_platform === 'android' || chosen.source_platform === 'shared' ? '将覆盖本机共享 AI 服务、模型与密钥。' : '将覆盖本机应用设置、AI 服务、模型与密钥。'}记账、附件、项目记忆和文件不受影响。恢复前会保留配置副本。</Notice>{preview && <p className="small-note">包含 {preview.providers.length} 个 AI 服务、{preview.secret_count} 个密钥。</p>}{webdavLegacyPasswordRequired(chosen) && <Field label="旧备份密码"><input type="password" autoComplete="off" disabled={!!busy} value={restorePassword} onChange={event => setRestorePassword(event.target.value)} placeholder="输入上传旧备份时设置的密码" /></Field>}<div className="modal-footer"><Button disabled={!!busy} onClick={closeRestore}>取消</Button><Button variant="danger" disabled={!usable || (webdavLegacyPasswordRequired(chosen) && !restorePassword)} busy={busy === 'restore'} onClick={restore}>确认恢复</Button></div></div></Modal>}
  </Section>
  </>;
}
