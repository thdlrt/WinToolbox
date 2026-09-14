import { useState } from 'react';
import { ArrowUpRight, RefreshCw, Square } from 'lucide-react';
import { errorText, native, rpc, type Job } from './api';
import { useApp } from './context';
import { basename, Button, dateText, IconButton, Progress, Status, toolLabel } from './ui';
import { processing, retryable, toolJobs, type ToolScope } from './toolJobsState';
import SubtitleEditor from './SubtitleEditor';
import './toolJobs.css';

/** Tool-local execution feedback; no cross-tool navigation or global overview. */
export default function ToolJobs({ scope, title, collectionId }: { scope: ToolScope; title: string; collectionId?: string }) {
  const { jobs, connected, run, track, refreshJobs } = useApp();
  const [busy, setBusy] = useState('');
  const [editing, setEditing] = useState<string>();
  const relevant = toolJobs(jobs, scope, collectionId);
  const act = async (job: Job, action: 'cancel' | 'retry') => {
    setBusy(job.id);
    const result = await run(() => rpc<Job>(`jobs.${action}`, { id: job.id }));
    if (result) { if (action === 'retry') track(result, '已开始重试。'); await run(refreshJobs); }
    setBusy('');
  };
  const row = (job: Job) => <article className="tool-job" key={job.id}>
    <div className="tool-job-heading"><strong>{toolLabel(job.tool)}{Array.isArray(job.params.paths) && typeof job.params.paths[0] === 'string' ? ` · ${basename(job.params.paths[0])}` : ''}</strong><Status value={job.status} /><span className="flex-spacer" />{processing(job.status) ? <IconButton label="取消处理" disabled={!connected || busy === job.id || job.status === 'cancelling'} onClick={() => void act(job, 'cancel')}><Square size={14} /></IconButton> : retryable(job.status) && <Button variant="ghost" disabled={!connected} busy={busy === job.id} onClick={() => void act(job, 'retry')}><RefreshCw size={14} />重试</Button>}</div>
    {processing(job.status) && <Progress value={job.progress} />}
    <p className={job.error ? 'tool-job-error' : 'muted'}>{String(job.error ? errorText(job.error) : job.message || '').split('\n')[0].slice(0, 200)}<time>{dateText(job.created_at)}</time></p>
    {job.error && (errorText(job.error).length > 200 || errorText(job.error).includes('\n')) && <details className="tool-job-results"><summary>错误详情</summary><pre className="error-detail">{errorText(job.error)}</pre></details>}
    {!!job.artifacts?.length && <details className="tool-job-results"><summary>结果文件 · {job.artifacts.length}</summary>{job.artifacts.map((artifact, index) => <div className="tool-job-artifact" key={`${artifact.path}-${index}`}><span title={artifact.path}>{artifact.label || artifact.name || basename(artifact.path)}</span>{(artifact.kind === 'subtitles' || /\.(srt|vtt)$/i.test(artifact.path) || /segments\.json$/i.test(artifact.path)) && <Button variant="ghost" onClick={() => setEditing(artifact.path)}>编辑字幕</Button>}<IconButton label={`打开 ${artifact.label || basename(artifact.path)}`} disabled={!connected} onClick={() => void run(() => native.open(artifact.path))}><ArrowUpRight size={16} /></IconButton></div>)}</details>}
  </article>;
  if (!relevant.length) return null;
  return <section className="tool-jobs" aria-label={title}><h3>{title}</h3>{relevant.slice(0, 3).map(row)}{relevant.length > 3 && <details className="tool-job-history"><summary>更早记录 · {relevant.length - 3}</summary>{relevant.slice(3).map(row)}</details>}{editing && <SubtitleEditor path={editing} onClose={() => setEditing(undefined)} />}</section>;
}
