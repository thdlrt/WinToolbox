export interface WebDavConnection {
  url: string; remote_path: string; username: string; has_password: boolean;
  configured?: boolean; include_media?: boolean; include_models?: boolean; include_secrets?: boolean;
  service_paths?: { config_backups: string; ledger: string; relay: string; project_memory: string };
}
export interface WebDavDraft { url: string; remote_path: string; username: string; password: string }
export interface WebDavSnapshot { name: string; size?: number; modified_at?: string | number | null; encrypted?: boolean; scope?: 'config' | 'legacy_full'; location?: 'config' | 'legacy_root' | 'unified' | 'legacy_ai'; path?: string; kind?: 'unified' | 'legacy_ai'; source_platform?: 'windows' | 'android' | 'shared' }
export function webdavSnapshotKey(snapshot: WebDavSnapshot) { return snapshot.path || `${snapshot.location || 'config'}:${snapshot.name}`; }
export const emptyWebDavDraft = (): WebDavDraft => ({ url: '', remote_path: '', username: '', password: '' });
export function webdavConnectionParams(draft: WebDavDraft): Record<string, unknown> {
  return { url: draft.url.trim(), remote_path: draft.remote_path.trim(), username: draft.username, ...(draft.password !== '' ? { password: draft.password } : {}) };
}
export function webdavDirty(draft: WebDavDraft, saved?: WebDavConnection): boolean {
  return draft.url.trim() !== (saved?.url || '') || draft.remote_path.trim() !== (saved?.remote_path || '') || draft.username !== (saved?.username || '') || draft.password !== '';
}
export function webdavBackupPasswordValid(includeSecrets: boolean, password: string): boolean {
  return password.length >= 8 || (!includeSecrets && password.length === 0);
}

export function webdavLegacyPasswordRequired(snapshot: WebDavSnapshot) {
  return !!snapshot.encrypted && snapshot.kind !== 'unified' && snapshot.kind !== 'legacy_ai' && snapshot.location !== 'legacy_ai';
}
export function webdavRestoreParams(snapshot: WebDavSnapshot, password = '') {
  return { name: snapshot.name, location: snapshot.location || 'config', ...(snapshot.path ? { path: snapshot.path } : {}), ...(webdavLegacyPasswordRequired(snapshot) && password ? { backup_password: password } : {}) };
}
