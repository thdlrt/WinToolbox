import { useCallback, useEffect, useState } from 'react';
import { errorText, rpc, subscribe, type Job } from './api';
import { useApp } from './context';
import { Button, Field, Modal, Notice } from './ui';
type Version = { op_id: string; local: boolean; name: string; model: string; deleted: boolean };
type Conflict = { id: string; parents: string[]; versions: Version[] };
export default function AiSyncConflicts() {
  const { connected, track } = useApp();
  const [remoteOpen, setRemoteOpen] = useState(false);
  const [items, setItems] = useState<Conflict[]>([]);
  const [selected, setSelected] = useState<Record<string, string>>({});
  const [confirm, setConfirm] = useState<Conflict>();
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState('');
  const refresh = useCallback(async () => { const value = await rpc<{items: Conflict[]}>('ai_aggregation.conflicts'); setItems(value.items); }, []);
  useEffect(() => {
    if (!connected) return;
    let disposed = false, off: (() => void) | undefined;
    void refresh().catch(e => { if (!disposed) setFailure(errorText(e)); });
    void subscribe(event => { if (['ai.config.changed', 'data_sync.changed'].includes(event.type)) void refresh().catch(e => { if (!disposed) setFailure(errorText(e)); }); }).then(value => { if (disposed) value(); else off = value; });
    return () => { disposed = true; off?.(); };
  }, [connected, refresh]);
  const resolve = async () => {
    if (!confirm) return;
    setBusy(true); setFailure('');
    try { await rpc('ai_aggregation.resolve', { id: confirm.id, parents: confirm.parents, op_id: selected[confirm.id] }); setConfirm(undefined); await refresh(); }
    catch (reason) { setFailure(errorText(reason)); }
    finally { setBusy(false); }
  };
  const useRemote = async () => {
    setBusy(true); setFailure('');
    try { const job = await rpc<Job>('ai_aggregation.use_remote'); track(job, '正在备份本机并读取远端 AI 聚合配置。'); setRemoteOpen(false); }
    catch (reason) { setFailure(errorText(reason)); }
    finally { setBusy(false); }
  };
  return <details className="details" open={items.length > 0}><summary>配置同步{items.length ? ' · 冲突 ' + items.length : ''}</summary>
    <p className="small-note">账户和调用配置自动同步。迁移失败时可先备份本机，再使用远端配置。</p>
    <Button variant="danger" disabled={!connected || busy} onClick={() => setRemoteOpen(true)}>使用远端覆盖 AI 聚合配置</Button>
    {!!(items.length || failure) && <Notice tone="warning">{failure || 'AI 聚合配置有冲突，请选择需要保留的版本。其他版本保留在同步历史中。'}</Notice>}
    {items.map(item => <div className="form-grid" key={item.id}><Field label={item.versions.find(v => !v.deleted)?.name || '已删除账户'}><select disabled={busy} value={selected[item.id] || ''} onChange={event => setSelected({ ...selected, [item.id]: event.target.value })}><option value="">选择保留的版本</option>{item.versions.map(version => <option key={version.op_id} value={version.op_id}>{version.local ? '本机' : '远端'} · {version.name}{version.model ? ' · ' + version.model : ''}</option>)}</select></Field><div className="button-row"><Button disabled={busy || !selected[item.id]} onClick={() => setConfirm(item)}>采用所选版本</Button></div></div>)}
    {confirm && <Modal title="采用 AI 聚合配置" onClose={() => { if (!busy) setConfirm(undefined); }}><div className="modal-body"><p>将先备份本机账户配置，再采用所选版本。历史版本和调用记录保留。</p><div className="modal-footer"><Button disabled={busy} onClick={() => setConfirm(undefined)}>取消</Button><Button variant="danger" busy={busy} onClick={() => void resolve()}>备份并采用</Button></div></div></Modal>}
    {remoteOpen && <Modal title="使用远端 AI 聚合配置" onClose={() => { if (!busy) setRemoteOpen(false); }}><div className="modal-body"><Notice tone="warning">将先保存本机账户和调用配置的恢复副本，再完整采用远端配置。本机尚未同步的配置会从当前账户列表移出，调用记录保留。</Notice><div className="modal-footer"><Button disabled={busy} onClick={() => setRemoteOpen(false)}>取消</Button><Button variant="danger" busy={busy} onClick={() => void useRemote()}>备份并使用远端</Button></div></div></Modal>}
  </details>;
}
