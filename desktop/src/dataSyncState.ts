export interface DataSyncService { id: string; label: string; configured: boolean; syncing: boolean; last_sync: number | null; error: string | null }
export interface DataSyncStatus { configured: boolean; syncing: boolean; last_sync: number | null; error: string | null; services: DataSyncService[] }
export function syncRelativeTime(seconds: number, now = Date.now()) {
  const age = Math.max(0, Math.floor((now - seconds * 1000) / 1000));
  if (age < 60) return '刚刚';
  if (age < 3600) return `${Math.floor(age / 60)} 分钟前`;
  if (age < 86400) return `${Math.floor(age / 3600)} 小时前`;
  return `${Math.floor(age / 86400)} 天前`;
}
export function dataSyncError(status?: DataSyncStatus) {
  return status?.error || status?.services?.filter(service => service.configured !== false && service.error).map(service => `${service.label}：${service.error}`).join('；') || '';
}
export function dataSyncTitle(status?: DataSyncStatus, connected = true, requestError = '', busy = false, now = Date.now()) {
  if (!connected) return '数据同步：本地服务未连接';
  const error = requestError || dataSyncError(status);
  if (busy || status?.syncing) return `正在同步数据${error ? `；上次错误：${error}` : ''}`;
  if (error) return `数据同步失败：${error}`;
  if (!status) return '正在读取数据同步状态';
  if (!status.configured) return '数据同步：未配置 WebDAV，点击打开设置';
  return status.last_sync ? `最近同步：${syncRelativeTime(status.last_sync, now)}；点击立即同步` : '数据尚未同步；点击立即同步';
}

// The first observation is historical state. Only a later completed sync can notify.
export function observeDataSyncCompletion(previous: number | null | undefined, status: DataSyncStatus) {
  const timestamp = typeof status.last_sync === 'number' && Number.isFinite(status.last_sync) && status.last_sync > 0 ? status.last_sync : null;
  if (previous === undefined) return { lastSync: timestamp, notify: false };
  const complete = status.configured && !status.syncing && !dataSyncError(status)
    && !status.services?.some(service => service.configured && service.syncing);
  const notify = !!complete && timestamp !== null && timestamp > (previous ?? 0);
  return { lastSync: notify ? timestamp : previous, notify };
}
