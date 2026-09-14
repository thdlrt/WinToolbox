import { useState } from 'react';
import { ArrowRight, Check, FileClock, FileCode2, History, ScanSearch } from 'lucide-react';
import { native, rpc } from '../api';
import { useApp } from '../context';
import { basename, Button, cx, dateText, Empty, Field, Modal, Notice, PageActions, PathInput, Section } from '../ui';

interface Model { id: string; name?: string; default_context?: number; max_context?: number; context_window?: number; visibility?: string }
interface Scan { models: Model[]; current: { model?: string; context?: number }; config_path: string; cache_path: string; warnings?: string[]; hash: string }
interface Preview { before: string; after: string; hash: string; model?: string; context?: number; config_path?: string }
interface Backup { path?: string; backup_path?: string; created_at?: string | number; name?: string }

export default function CodexPage() {
  const { connected, run, error, success } = useApp();
  const [home, setHome] = useState('');
  const [configPath, setConfigPath] = useState('');
  const [cachePath, setCachePath] = useState('');
  const [scan, setScan] = useState<Scan>();
  const [modelId, setModelId] = useState('');
  const [mode, setMode] = useState('default');
  const [custom, setCustom] = useState('128000');
  const [preview, setPreview] = useState<Preview>();
  const [backups, setBackups] = useState<Backup[]>();
  const [restore, setRestore] = useState<string>();
  const [busy, setBusy] = useState('');
  const [applied, setApplied] = useState('');
  const paths = () => ({ ...(home ? { home } : {}), ...(configPath ? { config_path: configPath } : {}), ...(cachePath ? { cache_path: cachePath } : {}) });
  const params = () => ({ ...paths(), model: modelId, context: mode === 'custom' ? Number(custom) : mode });
  const selected = scan?.models.find(item => item.id === modelId);
  const changed = () => { setPreview(undefined); setApplied(''); };
  const scanModels = async () => { setBusy('scan'); setPreview(undefined); const result = await run(() => rpc<Scan>('codex.scan', paths())); if (result) { setScan(result); setModelId(result.current.model || result.models[0]?.id || ''); } setBusy(''); };
  const getPreview = async () => { setBusy('preview'); const result = await run(() => rpc<Preview>('codex.preview', params())); if (result) setPreview(result); setBusy(''); };
  const apply = async () => { if (!preview) return; setBusy('apply'); const result = await run(() => rpc<{ backup_path: string }>('codex.apply', { ...params(), expected_hash: preview.hash })); if (result) { setApplied(result.backup_path); setPreview(undefined); success('配置已更新，原配置已备份。'); const next = await run(() => rpc<Scan>('codex.scan', paths())); if (next) setScan(next); } setBusy(''); };
  const listBackups = async () => { setBusy('backups'); const result = await run(() => rpc<{ backups: (Backup | string)[] }>('codex.backups', paths())); if (result) setBackups(result.backups.map(item => typeof item === 'string' ? { path: item } : item)); setBusy(''); };
  const restoreBackup = async () => { if (!restore) return; setBusy('restore'); const check = await run(() => rpc<Scan>('codex.scan', paths())); if (check) { const result = await run(() => rpc('codex.restore', { ...paths(), backup_path: restore, expected_hash: check.hash })); if (result) { setRestore(undefined); setPreview(undefined); success('已从备份恢复配置。'); await scanModels(); } } setBusy(''); };

  return <>
    <PageActions action={<Button busy={busy === 'backups'} disabled={!connected} onClick={listBackups}><History size={16} />配置备份</Button>} />
    <div className="codex-workspace"><Section title="模型扫描" action={<Button variant="primary" disabled={!connected} busy={busy === 'scan'} onClick={scanModels}><ScanSearch size={16} />扫描模型</Button>}><Field label="Codex 主目录（可选）"><PathInput value={home} onChange={value => { setHome(value); setScan(undefined); changed(); }} placeholder="默认使用当前用户的 .codex 目录" onError={error} /></Field><details className="details"><summary>指定配置文件与缓存文件</summary><div className="form-stack top-gap"><Field label="配置文件"><PathInput value={configPath} onChange={value => { setConfigPath(value); changed(); }} file placeholder="自动查找 config.toml" onError={error} /></Field><Field label="模型缓存"><PathInput value={cachePath} onChange={value => { setCachePath(value); changed(); }} file placeholder="自动查找 models_cache.json" onError={error} /></Field></div></details>{scan?.warnings?.map(warning => <Notice key={warning} tone="warning">{warning}</Notice>)}</Section>
      {scan ? <Section title="模型与上下文" description={`发现 ${scan.models.length} 个模型`}><div className="current-config"><FileCode2 size={19} /><div><span>当前配置</span><strong>{scan.current.model || '未指定模型'}<em>{scan.current.context ? `${scan.current.context.toLocaleString()} tokens` : '默认上下文'}</em></strong></div></div><Field label="使用模型"><select value={modelId} onChange={e => { setModelId(e.target.value); changed(); }}><option value="" disabled>选择模型</option>{scan.models.map(item => <option key={item.id} value={item.id}>{item.name || item.id}{item.name && item.name !== item.id ? ` · ${item.id}` : ''}</option>)}</select></Field><div className="context-options">{[['default', '默认上下文', '使用模型的默认容量'], ['max', '最大上下文', selected?.max_context ? `${selected.max_context.toLocaleString()} tokens` : '缓存未提供可确认上限'], ['custom', '自定义', '输入所需 token 数']].map(([value, label, hint]) => <button key={value} disabled={value === 'max' && !selected?.max_context} className={cx(mode === value && 'selected')} onClick={() => { setMode(value); changed(); }}><span className="radio-dot">{mode === value && <span />}</span><strong>{label}</strong><small>{hint}</small></button>)}</div>{mode === 'custom' && <Field label="上下文 token 数" hint={selected?.max_context ? `本地缓存上限：${selected.max_context.toLocaleString()}` : '缓存未提供上限，应用前后端会校验输入。'}><input type="number" min="1" step="1000" value={custom} onChange={e => { setCustom(e.target.value); changed(); }} /></Field>}<div className="section-footer"><span className="muted">先预览，再应用。每次应用都会备份。</span><Button disabled={!modelId || (mode === 'custom' && !(Number(custom) > 0))} busy={busy === 'preview'} onClick={getPreview}>预览修改<ArrowRight size={15} /></Button></div></Section> : null}
      {preview && <Section title="配置修改预览" description="仅在点击“应用配置”后写入文件。"><div className="diff-grid"><div><span className="diff-label">修改前</span><pre className="code-box before">{preview.before || '（空配置）'}</pre></div><div><span className="diff-label">修改后</span><pre className="code-box after">{preview.after || '（空配置）'}</pre></div></div><div className="section-footer"><span className="muted">{preview.config_path || scan?.config_path}</span><Button variant="primary" busy={busy === 'apply'} onClick={apply}><Check size={16} />应用配置</Button></div></Section>}
      {applied && <Notice tone="success">配置已更新，新会话生效。原配置备份：<button className="inline-link" onClick={() => void run(() => native.open(applied))}>{basename(applied)}</button></Notice>}
    </div>
    {backups && <Modal title="配置备份" onClose={() => setBackups(undefined)}><div className="modal-body">{backups.length ? <div className="backup-list">{backups.map((item, index) => { const path = item.path || item.backup_path || ''; return <div key={path || index}><FileClock size={20} /><span><strong>{item.name || basename(path)}</strong><small>{dateText(item.created_at)}</small></span><Button disabled={!path} onClick={() => setRestore(path)}>恢复</Button></div>; })}</div> : <Empty title="还没有备份">应用配置时会自动创建备份。</Empty>}</div></Modal>}
    {restore && <Modal title="恢复此备份" onClose={() => setRestore(undefined)}><div className="modal-body"><p>将用 <strong>{basename(restore)}</strong> 替换当前配置。当前配置会在恢复前备份。</p><div className="modal-footer"><Button onClick={() => setRestore(undefined)}>取消</Button><Button variant="primary" busy={busy === 'restore'} onClick={restoreBackup}>恢复配置</Button></div></div></Modal>}
  </>;
}
