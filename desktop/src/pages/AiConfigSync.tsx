import { useEffect, useRef, useState } from 'react';
import { ArrowDownToLine, ArrowUpFromLine } from 'lucide-react';
import { errorText, rpc, type Job, type Provider, type RoleConfig } from '../api';
import { useApp } from '../context';
import { activeJob, Button, dateText, Modal, Notice, Progress, Section } from '../ui';

interface AiStatus { configured: boolean; last_upload?: number; last_download?: number; error?: string }
interface Preview { confirmation_token: string; providers: Provider[]; roles: Record<string, RoleConfig>; secret_count: number }
type AiJob = Job & { result?: Partial<Preview> & { applied?: boolean; uploaded?: boolean } };

export default function AiConfigSync({ usable }: { usable: boolean }) {
  const { connected, jobs, track, refreshSettings } = useApp();
  const [status, setStatus] = useState<AiStatus>();
  const [preview, setPreview] = useState<Preview>();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const requested = useRef(new Map<string, 'upload' | 'preview' | 'apply'>());
  const handled = useRef(new Set<string>());
  const running = jobs.find(job => job.tool.startsWith('ai.config.') && activeJob(job.status));
  const disabled = !usable || !status?.configured || busy || !!running;
  useEffect(() => {
    if (!connected) return;
    let disposed = false;
    void rpc<AiStatus>('ai.config.status').then(value => { if (!disposed) setStatus(value); }).catch(reason => { if (!disposed) setError(errorText(reason)); });
    return () => { disposed = true; };
  }, [connected, usable]);
  useEffect(() => {
    for (const job of jobs as AiJob[]) {
      const action = requested.current.get(job.id);
      if (!action || activeJob(job.status) || handled.current.has(job.id)) continue;
      handled.current.add(job.id);
      if (job.status !== 'completed') { setError(errorText(job.error || job.message || 'AI 配置同步未完成')); continue; }
      if (action === 'preview') {
        if (!job.result?.confirmation_token || !job.result.providers || !job.result.roles) { setError('配置预览不完整，请重新下载。'); continue; }
        setPreview(job.result as Preview);
      } else if (action === 'apply') {
        setPreview(undefined); setMessage('AI 模型和密钥已替换，其他设置保持完整。');
        void refreshSettings().catch(reason => setError(errorText(reason)));
      } else setMessage('AI 配置已加密上传，可在另一台设备下载。');
      void rpc<AiStatus>('ai.config.status').then(setStatus).catch(reason => setError(errorText(reason)));
    }
  }, [jobs, refreshSettings]);
  const start = async (action: 'upload' | 'preview' | 'apply') => {
    if (disabled || action === 'apply' && !preview) return;
    setBusy(true); setError(''); setMessage('');
    try {
      const method = action === 'upload' ? 'ai.config.upload' : 'ai.config.download';
      const params = action === 'apply' ? { confirmation_token: preview!.confirmation_token, confirm_replace: true } : {};
      const job = await rpc<Job>(method, params);
      requested.current.set(job.id, action);
      track(job, action === 'upload' ? '正在加密上传 AI 配置。' : action === 'preview' ? '正在下载并预览 AI 配置。' : '正在应用 AI 配置。');
    } catch (reason) { setError(errorText(reason)); }
    finally { setBusy(false); }
  };
  return <Section title="跨设备 AI 配置">
    <p className="small-note">在电脑和 Android 间共享模型服务与 API 密钥，使用已保存的 WebDAV 密码加密。更换 WebDAV 密码后，请重新上传。</p>
    <div className="webdav-service-actions"><Button disabled={disabled} onClick={() => void start('upload')}><ArrowUpFromLine size={15} />上传本机 AI 配置</Button><Button disabled={disabled} onClick={() => void start('preview')}><ArrowDownToLine size={15} />下载并预览</Button></div>
    {(status?.last_upload || status?.last_download) && <p className="small-note">上传：{dateText(status.last_upload)}　应用：{dateText(status.last_download)}</p>}
    {running && <><p className="small-note">{running.message || '正在同步 AI 配置…'}</p><Progress value={running.progress || 0} /></>}
    {error && <Notice tone="warning">{error}</Notice>}{message && <Notice tone="success">{message}</Notice>}
    {preview && <Modal title="替换本机 AI 配置" onClose={() => { if (!busy && !running) setPreview(undefined); }}><div className="modal-body">
      <p>云端包含 {preview.providers.length} 个模型服务、{preview.secret_count} 个 API 密钥：</p>
      <ul>{preview.providers.map(provider => <li key={provider.id}>{provider.name || provider.id} · {provider.kind} · {provider.has_key ? '已配置密钥' : '未配置密钥'}</li>)}</ul>
      <details><summary>查看模型分工</summary><ul>{Object.entries(preview.roles).map(([role, value]) => <li key={role}>{role}：{value.model}</li>)}</ul></details>
      <Notice tone="warning">确认后将替换本机的模型服务、模型分工和 API 密钥。其他应用设置、记账与文件保持完整。</Notice>
      <div className="modal-footer"><Button disabled={busy || !!running} onClick={() => setPreview(undefined)}>取消</Button><Button variant="danger" disabled={disabled} onClick={() => void start('apply')}>确认替换 AI 配置</Button></div>
    </div></Modal>}
  </Section>;
}
