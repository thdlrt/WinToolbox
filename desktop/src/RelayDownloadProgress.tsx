import type { Job } from './api';
import { activeJob, Button, statusLabel } from './ui';

const bytes = (value: number) => {
  const n = Math.max(0, Number.isFinite(value) ? value : 0);
  if (n < 1024) return `${Math.round(n)} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
};

export default function RelayDownloadProgress({ job, onCancel }: { job: Job; onCancel: () => void }) {
  const active = activeJob(job.status);
  const transfer = job.transfer;
  const percent = job.status === 'completed' ? 100 : Math.max(0, Math.min(99, job.progress || 0));
  const stale = job.updated_at && Date.now() - Date.parse(job.updated_at) > 2500;
  const speed = active && transfer?.phase === 'downloading' && !stale ? transfer.bytes_per_second : 0;
  const title = job.status === 'running' ? (transfer?.phase === 'verifying' ? '校验并保存中' : '下载中') : statusLabel(job.status);
  return <div className="relay-download" aria-label="下载状态">
    <div className="relay-download-heading"><strong>{title}</strong><span>{percent.toFixed(1)}%</span>{active && <Button disabled={job.status === 'cancelling'} onClick={onCancel}>取消</Button>}</div>
    <progress aria-label="下载进度" max={100} value={percent} />
    {transfer ? <>
      <div className="relay-download-stats"><span>{bytes(transfer.received_bytes)} / {bytes(transfer.total_bytes)}</span>{active && <span>{bytes(speed)}/s</span>}<span>文件 {transfer.file_index} / {transfer.file_count}</span></div>
      <div className="muted relay-download-name">{transfer.file_name}</div>
    </> : <div className="muted">{job.message || '等待开始…'}</div>}
    {job.status === 'failed' && <div className="relay-download-error">{job.message}</div>}
  </div>;
}
