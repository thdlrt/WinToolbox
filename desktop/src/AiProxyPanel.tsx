import { useCallback, useEffect, useRef, useState } from 'react';
import { Copy, KeyRound, Plus } from 'lucide-react';
import { errorText, rpc, type Job } from './api';
import { useApp } from './context';
import { activeJob, Button, Field, Modal, Notice } from './ui';

interface AccessKey { id: string; name: string; prefix: string; revoked: boolean }
interface ProxyStatus { enabled: boolean; running: boolean; port: number; base_url: string; error?: string; keys: AccessKey[]; protocol: string; models: { id: string; account_name?: string }[]; models_refreshed_at?: number; model_errors?: string[] }

export default function AiProxyPanel() {
  const { connected, jobs, track } = useApp();
  const [status, setStatus] = useState<ProxyStatus>();
  const [failure, setFailure] = useState('');
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<{ api_key: string; name: string }>();
  const [revoke, setRevoke] = useState<AccessKey>();
  const [port, setPort] = useState('18791');
  const [query, setQuery] = useState('');
  const [modelsOpen, setModelsOpen] = useState(false);
  const [fetching, setFetching] = useState(false);
  const handled = useRef(new Set<string>());
  const fetchingModels = fetching || jobs.some(j => j.tool === 'ai_proxy.models.refresh' && activeJob(j.status));
  const load = useCallback(async () => { const value = await rpc<ProxyStatus>('ai_proxy.status'); setStatus(value); setPort(String(value.port)); }, []);
  useEffect(() => { if (connected) void load().catch(e => setFailure(errorText(e))); }, [connected, load]);
  useEffect(() => {
    for (const job of jobs) if (job.tool === 'ai_proxy.models.refresh' && !activeJob(job.status) && !handled.current.has(job.id)) {
      handled.current.add(job.id);
      if (job.status === 'completed') { setStatus((job as Job & { result: ProxyStatus }).result); setModelsOpen(true); }
      else setFailure(errorText(job.error || job.message));
    }
  }, [jobs]);
  const fetchModels = async () => {
    setFetching(true); setFailure('');
    try { track(await rpc<Job>('ai_proxy.models.refresh'), '正在获取各平台完整模型列表。'); setModelsOpen(true); }
    catch (e) { setFailure(errorText(e)); }
    finally { setFetching(false); }
  };
  const copy = (text: string) => { void navigator.clipboard.writeText(text).catch(e => setFailure(errorText(e))); };
  const configure = async (change: { enabled?: boolean; port?: number }) => {
    setBusy(true); setFailure('');
    try { setStatus(await rpc<ProxyStatus>('ai_proxy.configure', change)); }
    catch (e) { setFailure(errorText(e)); }
    finally { setBusy(false); }
  };
  const create = async () => {
    setBusy(true); setFailure('');
    try { setCreated(await rpc('ai_proxy.keys.create', { name: name.trim() || '本机应用' })); setCreating(false); setName(''); await load(); }
    catch (e) { setFailure(errorText(e)); }
    finally { setBusy(false); }
  };
  return <section className="ai-proxy-panel">
    <div className="quota-platform-heading"><h2>应用接入</h2><label className="ai-proxy-enable"><input type="checkbox" checked={status?.enabled || false} disabled={!connected || busy} onChange={e => void configure({ enabled: e.target.checked })} />{status?.running ? '本地代理已启动' : '本地代理已停止'}</label></div>
    <div className="ai-proxy-url"><Field label="API Base URL"><input aria-label="本地代理 URL" readOnly value={status?.base_url || ''} /></Field><Button disabled={!status} onClick={() => copy(status!.base_url)}><Copy size={15} />复制地址</Button></div>
    <small>在应用中选择 OpenAI 兼容接口，填写此地址、接入 Key 和对应模型 ID。</small>
    {status?.error && <Notice tone="warning">{status.error}</Notice>}
    <div className="ai-proxy-keys-heading"><b>接入 Key</b><Button disabled={!connected || busy} onClick={() => setCreating(true)}><Plus size={15} />创建 Key</Button></div>
    {status?.keys.filter(k => !k.revoked).map(k => <div className="ai-proxy-key-row" key={k.id}><KeyRound size={15} /><b>{k.name}</b><code>{k.prefix}••••••</code><Button disabled={busy} onClick={() => setRevoke(k)}>撤销</Button></div>)}
    {status && !status.keys.some(k => !k.revoked) && <small>尚未创建接入 Key</small>}
    <div className="ai-proxy-keys-heading"><b>模型库 <small>{status?.models.length || 0} 个{!status?.models_refreshed_at && '默认模型'}</small></b><Button disabled={!connected || fetchingModels} onClick={() => void fetchModels()}>{fetchingModels ? '获取中…' : '一键获取模型'}</Button></div>
    <details className="ai-proxy-details" open={modelsOpen} onToggle={e => setModelsOpen(e.currentTarget.open)}><summary>查看模型 ID</summary><input className="ai-proxy-search" aria-label="搜索模型" placeholder="搜索模型或平台" value={query} onChange={e => setQuery(e.target.value)} /><div className="ai-proxy-models ai-proxy-model-library">{status?.models.filter(m => `${m.id} ${m.account_name || ''}`.toLowerCase().includes(query.toLowerCase())).map(m => <div key={m.id}><span><small>{m.account_name}</small><code>{m.id}</code></span><Button onClick={() => copy(m.id)}>复制</Button></div>)}</div><small>外部应用通过 /v1/models 获取同一份模型列表。获取目录不发起生成请求。</small>{status?.model_errors?.map(e => <Notice key={e} tone="warning">{e}</Notice>)}</details>
    <details className="ai-proxy-details"><summary>本地端口</summary><div className="button-row"><Field label="端口"><input type="number" min={1024} max={65535} value={port} onChange={e => setPort(e.target.value)} /></Field><Button disabled={busy || !connected} onClick={() => void configure({ port: Number(port) })}>保存端口</Button></div></details>
    {failure && <Notice tone="warning">{failure}</Notice>}
    {creating && <Modal title="创建应用接入 Key" onClose={() => { if (!busy) setCreating(false); }}><Field label="应用名称"><input autoFocus value={name} onChange={e => setName(e.target.value)} maxLength={80} placeholder="例如 Agent 项目、Pi、OpenCode" /></Field><div className="quota-form-actions"><Button disabled={busy} onClick={() => setCreating(false)}>取消</Button><Button variant="primary" disabled={busy} onClick={() => void create()}>创建</Button></div></Modal>}
    {created && <Modal title={`${created.name} · 接入 Key`} onClose={() => setCreated(undefined)}><Field label="API Key" hint="仅在创建时显示，请复制到要接入的应用。"><input readOnly value={created.api_key} /></Field><div className="quota-form-actions"><Button onClick={() => copy(created.api_key)}>复制 Key</Button><Button variant="primary" onClick={() => setCreated(undefined)}>完成</Button></div></Modal>}
    {revoke && <Modal title="撤销接入 Key" onClose={() => setRevoke(undefined)}><p>撤销后，「{revoke.name}」将无法继续调用本地代理。</p><div className="quota-form-actions"><Button disabled={busy} onClick={() => setRevoke(undefined)}>取消</Button><Button variant="danger" disabled={busy} onClick={async () => { setBusy(true); try { setStatus(await rpc('ai_proxy.keys.revoke', { id: revoke.id })); setRevoke(undefined); } catch (e) { setFailure(errorText(e)); } finally { setBusy(false); } }}>撤销</Button></div></Modal>}
  </section>;
}
