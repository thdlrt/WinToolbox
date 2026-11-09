import { useEffect, useState } from 'react';
import { errorText, rpc } from './api';
import { useApp } from './context';
import { Button, Field, Notice } from './ui';

type Source = { key: string; url: string; username: string; remote_path: string; error?: string | null };
export default function WebDavMigrations({ connection }: { connection: string }) {
  const { connected } = useApp();
  const [sources, setSources] = useState<Source[]>([]);
  const [draft, setDraft] = useState<Source>();
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState('');
  const [message, setMessage] = useState('');
  useEffect(() => {
    if (!connected) return;
    let alive = true;
    void rpc<{ sources: Source[] }>('webdav.migration.sources').then(value => { if (alive) setSources(value.sources); }).catch(reason => { if (alive) setFailure(errorText(reason)); });
    return () => { alive = false; };
  }, [connected, connection]);
  const refresh = async () => {
    setBusy(true); setFailure('');
    try { const result = await rpc<{ sources: Source[] }>('webdav.migration.sources'); setSources(result.sources); setDraft(undefined); setPassword(''); }
    catch (reason) { setFailure(errorText(reason)); }
    finally { setBusy(false); }
  };
  const save = async () => {
    if (!draft) return;
    setBusy(true); setFailure(''); setMessage('');
    try {
      const result = await rpc<{ sources: Source[] }>('webdav.migration.save', { key: draft.key, url: draft.url, username: draft.username, remote_path: draft.remote_path, password });
      setSources(result.sources); setPassword(''); setDraft(undefined); setMessage('旧位置连接已更新，将重试合并。');
    } catch (reason) { setFailure(errorText(reason)); }
    finally { setBusy(false); }
  };
  if (!sources.length && !message && !failure) return null;
  return <details className="details"><summary>旧目录连接{sources.length ? ' · 待合并 ' + sources.length : ''}</summary>
    <p className="small-note">旧账号或密码失效时，在这里更新旧位置连接。当前目录会继续同步。</p>
    {failure && <Notice tone="warning">{failure}</Notice>}{message && <Notice tone="success">{message}</Notice>}
    <div className="button-row">{sources.map(source => <Button key={source.key} disabled={busy} onClick={() => { setDraft(source); setPassword(''); setMessage(''); }}>{source.username} · {source.remote_path}</Button>)}<Button disabled={!connected || busy} onClick={() => void refresh()}>刷新</Button></div>
    {draft && <div className="form-stack">
      <Field label="旧 WebDAV 地址"><input type="url" value={draft.url} disabled={busy} onChange={event => setDraft({ ...draft, url: event.target.value })} /></Field>
      <div className="form-grid"><Field label="旧目录"><input value={draft.remote_path} disabled={busy} onChange={event => setDraft({ ...draft, remote_path: event.target.value })} /></Field><Field label="用户名"><input value={draft.username} disabled={busy} onChange={event => setDraft({ ...draft, username: event.target.value })} /></Field></div>
      <Field label="旧位置密码" hint="留空保留；更换地址或账号时请重新填写。"><input type="password" autoComplete="new-password" disabled={busy} value={password} onChange={event => setPassword(event.target.value)} /></Field>
      {draft.error && <Notice tone="warning">{draft.error}</Notice>}<Button busy={busy} disabled={!connected || busy} onClick={() => void save()}>保存并重试</Button>
    </div>}
  </details>;
}
