import { useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { errorText, rpc } from './api';
import { useApp } from './context';
import { visiblePolling } from './visiblePolling';
import { cx } from './ui';
import { dataSyncError, dataSyncTitle, observeDataSyncCompletion, type DataSyncStatus } from './dataSyncState';

export default function DataSyncIndicator() {
  const { connected, navigate, error, success } = useApp();
  const [status, setStatus] = useState<DataSyncStatus>();
  const [failure, setFailure] = useState('');
  const [busy, setBusy] = useState(false);
  const completedSync = useRef<number | null | undefined>(undefined);
  useEffect(() => {
    if (!connected || !status) return;
    const completion = observeDataSyncCompletion(completedSync.current, status);
    completedSync.current = completion.lastSync;
    if (completion.notify) success('数据同步完成');
  }, [connected, status, success]);
  const lastSync = useRef<number | null | undefined>(undefined);
  useEffect(() => { if (status && lastSync.current !== undefined && status.last_sync !== lastSync.current) window.dispatchEvent(new Event('toolbox-data-synced')); if (status) lastSync.current = status.last_sync; }, [status]);
  const mounted = useRef(false), manual = useRef(false), polling = useRef(false), serial = useRef(0);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; serial.current++; };
  }, []);
  useEffect(() => {
    if (!connected) { setStatus(undefined); completedSync.current = undefined; return; }
    let disposed = false;
    const refresh = async () => {
      if (manual.current || polling.current) return;
      polling.current = true;
      const request = ++serial.current;
      try { const value = await rpc<DataSyncStatus>('data_sync.status'); if (!disposed && request === serial.current) { setStatus(value); setFailure(''); } }
      catch (reason) { if (!disposed && request === serial.current) setFailure(errorText(reason)); }
      finally { polling.current = false; }
    };
    const stopPolling = visiblePolling(refresh, 4000);
    return () => { disposed = true; stopPolling(); };
  }, [connected]);
  const sync = async () => {
    if (!connected || busy || status?.syncing) return;
    if (status && !status.configured) { sessionStorage.setItem('wintoolbox-settings-tab', 'data'); navigate('settings'); return; }
    manual.current = true; serial.current++; setBusy(true); setFailure('');
    try { const value = await rpc<DataSyncStatus>('data_sync.now'); if (mounted.current) { setStatus(value); if (dataSyncError(value)) error(dataSyncError(value)); } }
    catch (reason) { if (mounted.current) { setFailure(errorText(reason)); error(reason); } }
    finally { manual.current = false; if (mounted.current) setBusy(false); }
  };
  const spinning = busy || status?.syncing;
  return <button className={cx('data-sync-indicator', (failure || dataSyncError(status)) && 'failed')} aria-label="同步数据" title={dataSyncTitle(status, connected, failure, busy)} disabled={!connected || !!spinning} onClick={() => void sync()}><RefreshCw size={15} className={spinning ? 'spin' : ''} /></button>;
}
