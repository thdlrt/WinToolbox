export interface NetworkProfile { id: string; name: string; target: string; port: number | null; timeout_ms: number; attempts: number; revision?: string; sync_heads?: string[] }
export interface NetworkStep { name: 'dns' | 'tcp' | 'tls' | 'http'; status: 'ok' | 'error' | 'skipped'; duration_ms: number; detail: string; addresses?: string[]; samples_ms?: number[]; success_count?: number; attempt_count?: number; http_status?: number }
export interface NetworkReport { id: string; profile_id?: string; target: string; host: string; port: number; started_at: string; steps: NetworkStep[]; summary: string; route?: string }
export interface NetworkDraft { name: string; target: string; port: string; timeout_ms: string; attempts: string }
export const newNetworkDraft = (): NetworkDraft => ({ name: '', target: '', port: '', timeout_ms: '5000', attempts: '3' });
export function networkDraft(profile: NetworkProfile): NetworkDraft { return { name: profile.name, target: profile.target, port: profile.port ? String(profile.port) : '', timeout_ms: String(profile.timeout_ms), attempts: String(profile.attempts) }; }
export function networkValidation(draft: NetworkDraft): string {
  if (!draft.target.trim()) return '请输入主机、IP 或 HTTP(S) 地址。';
  if (draft.port && (!/^\d+$/.test(draft.port) || Number(draft.port) < 1 || Number(draft.port) > 65535)) return '端口范围为 1–65535。';
  if (!/^\d+$/.test(draft.timeout_ms) || Number(draft.timeout_ms) < 1000 || Number(draft.timeout_ms) > 15000) return '超时范围为 1000–15000 毫秒。';
  if (!/^\d+$/.test(draft.attempts) || Number(draft.attempts) < 1 || Number(draft.attempts) > 5) return '测试次数范围为 1–5。';
  if (/^https?:\/\//i.test(draft.target.trim())) {
    try { const url = new URL(draft.target.trim()); if (url.username || url.password || url.search || url.hash) return '请移除地址中的账号、密码、查询参数和片段。'; }
    catch { return '请输入有效的 HTTP(S) 地址。'; }
  } else if (/[\/\s?#@]/.test(draft.target.trim())) return '主机请填写域名或 IP，网页地址需以 http:// 或 https:// 开头。';
  return '';
}
export function networkParams(draft: NetworkDraft) { return { target: draft.target.trim(), port: draft.port ? Number(draft.port) : null, timeout_ms: Number(draft.timeout_ms), attempts: Number(draft.attempts) }; }
