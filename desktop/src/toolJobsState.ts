import type { Job } from './api';

export type ToolScope = 'media' | 'live' | 'presets' | 'models' | 'backups' | 'plugins' | 'knowledge' | 'expenses' | 'network';
export const processing = (status: string) => ['queued', 'pending', 'running', 'cancelling'].includes(status);
export const retryable = (status: string) => ['failed', 'cancelled', 'canceled', 'interrupted'].includes(status);
const liveMedia = (job: Job) => job.params.origin === 'live' || (Array.isArray(job.params.paths) && job.params.paths.some(path => typeof path === 'string' && /[\\/]sessions[\\/]/i.test(path)));
export function belongsToTool(job: Job, scope: ToolScope, collectionId?: string): boolean {
  switch (scope) {
    case 'expenses': return job.tool === 'expenses.export' || job.tool === 'expenses.sync';
    case 'network': return job.tool === 'network.run';
    case 'media': return job.tool === 'media' && !liveMedia(job);
    case 'live': return (job.tool === 'media' && liveMedia(job)) || job.tool === 'live.backfill';
    case 'presets': return job.tool === 'setup.install';
    case 'models': return ['models.install', 'model-install', 'model_install', 'models.import', 'models.export'].includes(job.tool);
    case 'backups': return ['backup-export', 'backup-import', 'backups.export', 'backups.import', 'backup_export', 'backup_import'].includes(job.tool);
    case 'plugins': return job.tool === 'plugin';
    case 'knowledge': return job.tool === 'documents.runtime.install' || (!!collectionId && job.tool.startsWith('knowledge') && job.params.collection_id === collectionId);
  }
}
export function toolJobs(jobs: Job[], scope: ToolScope, collectionId?: string) {
  return jobs.filter(job => belongsToTool(job, scope, collectionId)).sort((a, b) => Number(processing(b.status)) - Number(processing(a.status)) || b.created_at.localeCompare(a.created_at));
}
