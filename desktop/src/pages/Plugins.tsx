import { useCallback, useEffect, useState } from 'react';
import { ArrowRight, Package, Play, Plus, RefreshCw, Trash2 } from 'lucide-react';
import ToolJobs from '../ToolJobs';
import { native, rpc, type Job, type Plugin, type PluginField } from '../api';
import { useApp } from '../context';
import { Button, CheckField, Empty, Field, IconButton, Modal, Notice, PageActions, PathInput, Switch } from '../ui';

const manifestOf = (plugin: Plugin): Plugin => ({ ...plugin, ...plugin.manifest, enabled: plugin.enabled });
export default function PluginsPage() {
  const { connected, run, error, track } = useApp();
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [selected, setSelected] = useState<Plugin>();
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [busy, setBusy] = useState('');
  const [removing, setRemoving] = useState<Plugin>();
  const load = useCallback(async () => { const result = await run(() => rpc<{ plugins: Plugin[] }>('plugins.list')); if (result) setPlugins(result.plugins.map(manifestOf)); }, [run]);
  useEffect(() => { if (connected) void load(); }, [connected, load]);
  const install = async () => { const files = await run(() => native.files(false, [{ name: 'WinToolbox 工具包', extensions: ['toolpkg'] }])); if (!files?.length) return; setBusy('install'); const result = await run(() => rpc('plugins.install', { path: files[0] }), '工具包已安装。'); if (result) await load(); setBusy(''); };
  const enable = async (plugin: Plugin, enabled: boolean) => { setBusy(plugin.id); const result = await run(() => rpc('plugins.enable', { id: plugin.id, enabled })); if (result) await load(); setBusy(''); };
  const openTool = (plugin: Plugin) => { setSelected(plugin); const fields = plugin.ui?.fields || []; setParams(Object.fromEntries(fields.map(field => [field.name, field.default ?? (['bool', 'boolean'].includes(field.type || '') ? false : '')]))); };
  const execute = async () => { if (!selected) return; setBusy('run'); const job = await run(() => rpc<Job>('plugins.run', { id: selected.id, params })); if (job) { track(job); setSelected(undefined); } setBusy(''); };
  const uninstall = async () => { if (!removing) return; setBusy('uninstall'); const result = await run(() => rpc('plugins.uninstall', { id: removing.id }), '工具包已卸载。'); if (result) { setRemoving(undefined); await load(); } setBusy(''); };
  const fields = selected?.ui?.fields || [];
  const missing = fields.some(field => field.required && (params[field.name] === '' || params[field.name] === undefined));

  return <>
    <PageActions action={<Button variant="primary" busy={busy === 'install'} disabled={!connected} onClick={install}><Plus size={16} />安装工具包</Button>} />
    <Notice>工具包可运行本机代码，仅安装可信来源的 .toolpkg 文件。</Notice>
    <div className="list-toolbar"><h2>已安装工具 <span className="count-label">{plugins.length}</span></h2><Button variant="ghost" disabled={!connected} onClick={() => void load()}><RefreshCw size={15} />刷新</Button></div>
    {plugins.length ? <div className="plugin-grid">{plugins.map(plugin => <article className="plugin-card" key={plugin.id}><div className="plugin-card-top"><span className="tool-icon blue"><Package size={25} strokeWidth={1.6} /></span><Switch checked={plugin.enabled !== false} disabled={busy === plugin.id} label={`启用 ${plugin.name}`} onChange={value => void enable(plugin, value)} /></div><h3>{plugin.name}</h3><span className="plugin-version" title={plugin.id}>v{plugin.version}</span>{plugin.description && <p>{plugin.description}</p>}{plugin.permissions?.length ? <div className="plugin-permissions">声明能力：{plugin.permissions.join('、')}</div> : null}<div className="plugin-card-footer"><Button disabled={plugin.enabled === false} onClick={() => openTool(plugin)}><Play size={14} />打开工具<ArrowRight size={14} /></Button><IconButton label={`卸载 ${plugin.name}`} onClick={() => setRemoving(plugin)}><Trash2 size={16} /></IconButton></div></article>)}</div> : <Empty title="暂无扩展工具" />}
    <ToolJobs scope="plugins" title="运行结果" />
    {selected && <Modal title={selected.name} onClose={() => setSelected(undefined)}><div className="modal-body"><p className="muted">{selected.description}</p>{fields.length ? <div className="form-stack">{fields.map(field => <PluginInput key={field.name} field={field} value={params[field.name]} onChange={value => setParams(old => ({ ...old, [field.name]: value }))} onError={error} />)}</div> : <Notice>此工具无需额外参数，点击运行即可。</Notice>}<div className="modal-footer"><Button onClick={() => setSelected(undefined)}>关闭</Button><Button variant="primary" disabled={missing} busy={busy === 'run'} onClick={execute}><Play size={15} />运行工具</Button></div></div></Modal>}
    {removing && <Modal title="卸载工具包" onClose={() => setRemoving(undefined)}><div className="modal-body"><p>将从工具箱移除“{removing.name}”。已生成的结果文件会保留。</p><div className="modal-footer"><Button onClick={() => setRemoving(undefined)}>取消</Button><Button variant="danger" busy={busy === 'uninstall'} onClick={uninstall}>卸载</Button></div></div></Modal>}
  </>;
}
function PluginInput({ field, value, onChange, onError }: { field: PluginField; value: unknown; onChange: (value: unknown) => void; onError: (error: unknown) => void }) {
  if (['bool', 'boolean'].includes(field.type || '')) return <CheckField label={field.label || field.name} hint={field.description} checked={Boolean(value)} onChange={onChange} />;
  return <Field label={`${field.label || field.name}${field.required ? ' *' : ''}`} hint={field.description}>{field.type === 'file' || field.type === 'directory' ? <PathInput file={field.type === 'file'} value={String(value || '')} onChange={onChange} onError={onError} /> : field.type === 'select' || field.options ? <select value={String(value ?? '')} onChange={e => onChange(e.target.value)}><option value="">请选择</option>{field.options?.map(option => typeof option === 'string' ? <option key={option} value={option}>{option}</option> : <option key={option.value} value={option.value}>{option.label || option.value}</option>)}</select> : field.type === 'textarea' ? <textarea rows={4} value={String(value ?? '')} onChange={e => onChange(e.target.value)} /> : <input type={field.type === 'number' ? 'number' : 'text'} value={String(value ?? '')} onChange={e => onChange(field.type === 'number' ? (e.target.value === '' ? '' : Number(e.target.value)) : e.target.value)} />}</Field>;
}
