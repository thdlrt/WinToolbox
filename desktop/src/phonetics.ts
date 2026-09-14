export interface PhoneticsState { done: number[]; custom: string; checks: Record<string, boolean>; best: number }
export const PHONETICS_CHANNEL = 'wintoolbox.phonetics.v1';
export function phoneticsState(value: unknown): PhoneticsState {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('音标学习进度格式无效。');
  const state = value as Record<string, unknown>;
  if (Object.keys(state).sort().join(',') !== 'best,checks,custom,done' || !Array.isArray(state.done) || state.done.some(n => !Number.isInteger(n) || n < 0 || n > 4) || new Set(state.done).size !== state.done.length || typeof state.custom !== 'string' || state.custom.length > 20000 || !Number.isInteger(state.best) || Number(state.best) < 0 || Number(state.best) > 6 || !state.checks || typeof state.checks !== 'object' || Array.isArray(state.checks)) throw new Error('音标学习进度格式无效。');
  const checks = Object.entries(state.checks);
  if (checks.length > 100 || checks.some(([key, checked]) => !key || key.length > 80 || /[\x00-\x1f\x7f]/.test(key) || typeof checked !== 'boolean')) throw new Error('音标自查记录格式无效。');
  return { done: [...state.done] as number[], custom: state.custom, checks: Object.fromEntries(checks) as Record<string, boolean>, best: state.best as number };
}
export function trustedPhoneticsMessage(event: Pick<MessageEvent, 'source' | 'origin' | 'data'>, source: Window | null, origin: string, token: string) {
  return !!source && event.source === source && event.origin === origin && !!event.data && typeof event.data === 'object' && event.data.channel === PHONETICS_CHANNEL && event.data.token === token && ['ready', 'change', 'external', 'export', 'stopped'].includes(event.data.type);
}
export function courseExport(name: unknown, bytes: unknown): { name: string; bytes: number[] } {
  if (typeof name !== 'string' || !name || name.length > 150 || /[\\/:*?"<>|\x00-\x1f]/.test(name) || !/\.(md|webm|m4a)$/i.test(name) || !Array.isArray(bytes) || bytes.length > 16 * 1024 * 1024 || bytes.some(byte => !Number.isInteger(byte) || byte < 0 || byte > 255)) throw new Error('导出文件无效，或超过 16 MiB。');
  return { name, bytes };
}
// Writes are serialized; inputs arriving during a save replace only the next
// pending snapshot. A failed snapshot remains available for explicit retry.
export class PhoneticsWriter {
  private pending?: PhoneticsState;
  private running?: Promise<void>;
  constructor(private readonly persist: (state: PhoneticsState) => Promise<unknown>) {}
  enqueue(state: PhoneticsState) { this.pending = phoneticsState(state); return this.flush(); }
  flush(): Promise<void> {
    if (this.running) return this.running;
    const pump = async () => {
      while (this.pending) { const state = this.pending; await this.persist(state); if (this.pending === state) this.pending = undefined; }
    };
    this.running = pump().finally(() => { this.running = undefined; });
    return this.running;
  }
}
