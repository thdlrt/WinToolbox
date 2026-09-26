import { useCallback, useEffect, useState } from 'react';
import { errorText, rpc, type Job } from '../api';
import { useApp } from '../context';
import { activeJob, Button, CheckField, Notice } from '../ui';
import LedgerSync from '../LedgerSync';
import type { WebDavConnection } from '../webdavState';

interface MemorySync { auto_sync: boolean; last_sync_at?: string | number; last_error?: string }
interface RelayStatus { configured: boolean; migration?: { pending?: boolean; warning?: string; legacy_path?: string } }
export default function WebDavDataSync({ connection }: { connection?: WebDavConnection }) {
  const { connected, jobs, track, navigate } = useApp();
  const [memory, setMemory] = useState<MemorySync>();
  const [relay, setRelay] = useState<RelayStatus>();
  const [failure, setFailure] = useState('');
  const [busy, setBusy] = useState(false);
  const refresh = useCallback(async () => {
    const values = await Promise.allSettled([rpc<MemorySync>('memory.sync.status'), rpc<RelayStatus>('relay.get')]);
    if (values[0].status === 'fulfilled') setMemory(values[0].value);
    if (values[1].status === 'fulfilled') setRelay(values[1].value);
    const failed = values.find(value => value.status === 'rejected');
    setFailure(failed?.status === 'rejected' ? errorText(failed.reason) : '');
  }, []);
  useEffect(() => { if (!connected) return; void refresh(); const timer = setInterval(() => void refresh(), 5000); return () => clearInterval(timer); }, [connected, refresh]);
  const running = jobs.some(job => job.tool === 'memory.sync.run' && activeJob(job.status));
  const memorySync = async () => { setBusy(true); try { track(await rpc<Job>('memory.sync.run'), '正在同步项目记忆。'); } catch (reason) { setFailure(errorText(reason)); } finally { setBusy(false); } };
  const memoryAuto = async (enabled: boolean) => { setBusy(true); try { setMemory(await rpc<MemorySync>('memory.sync.configure', { auto_sync: enabled })); } catch (reason) { setFailure(errorText(reason)); } finally { setBusy(false); } };
  return <div className="webdav-services">
    <p className="small-note">记账与网络诊断配置在启动后自动合并，离线修改会在连接恢复后继续同步。</p>
    <div className="webdav-service"><div className="webdav-service-heading"><h3>记账与网络诊断</h3><Button variant="ghost" onClick={() => navigate('expenses')}>打开记账</Button></div><LedgerSync onSynced={() => {}} /></div>
    <div className="webdav-service"><div className="webdav-service-heading"><h3>项目记忆</h3><Button variant="ghost" onClick={() => navigate('memory')}>打开项目记忆</Button></div><div className="webdav-service-actions"><CheckField checked={!!memory?.auto_sync} disabled={!connected || busy || !memory} onChange={value => void memoryAuto(value)} label="自动同步" /><span className="small-note">{running ? '正在同步…' : memory?.last_error ? '同步失败，记录保留在本机' : memory?.auto_sync ? '运行期间每 5 分钟同步' : '自动同步未开启'}</span><Button disabled={!connected || !connection?.configured || busy || running} onClick={memorySync}>立即同步</Button></div>{memory?.last_error && <Notice tone="warning">{memory.last_error}</Notice>}</div>
    <div className="webdav-service"><div className="webdav-service-heading"><h3>文件中转站</h3><Button variant="ghost" onClick={() => navigate('relay')}>打开中转站</Button></div><p className="small-note">{relay?.configured ? '上传和下载使用统一 WebDAV 连接。' : '保存上方连接后即可上传和下载文件。'}</p>{relay?.migration?.warning && <Notice tone="warning">{relay.migration.warning}</Notice>}{relay?.migration?.pending && !relay.migration.warning && <Notice>旧中转文件将在下次连接时复制到统一目录，原文件保留。</Notice>}</div>
    {connection?.service_paths && <details className="webdav-service-paths"><summary>云端保存位置</summary><dl><dt>配置备份</dt><dd>{connection.service_paths.config_backups}</dd><dt>记账与网络诊断</dt><dd>{connection.service_paths.ledger}</dd><dt>文件中转站</dt><dd>{connection.service_paths.relay}</dd><dt>项目记忆</dt><dd>{connection.service_paths.project_memory}</dd></dl></details>}
    {failure && <Notice tone="warning">{failure}</Notice>}
  </div>;
}
