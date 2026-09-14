export interface ShizukuDevice { address: string; service?: string; label?: string; kind: 'connect' | 'pairing'; state?: string }
export interface ShizukuResult { target?: string; running?: boolean; paired?: boolean; message?: string; devices?: ShizukuDevice[]; notice?: string }
export function shizukuEndpoint(value: string): { address: string; ip: string; port: number } | undefined {
  const address = value.trim();
  const match = /^(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})$/.exec(address);
  if (!match) return;
  const parts = match[1].split('.').map(Number), port = Number(match[2]);
  if (parts.some((part, index) => part > 255 || String(part) !== match[1].split('.')[index]) || port < 1 || port > 65535) return;
  if (parts[3] === 0 || parts[3] === 255) return;
  const local = parts[0] === 10 || parts[0] === 192 && parts[1] === 168 || parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31;
  return local ? { address: `${match[1]}:${port}`, ip: match[1], port } : undefined;
}
export function shizukuPairError(pairTarget: string, code: string, target: string) {
  const pair = shizukuEndpoint(pairTarget), connect = target.trim() ? shizukuEndpoint(target) : undefined;
  if (!pair) return '请填写手机配对窗口中的局域网 IPv4 地址和配对端口。';
  if (!/^\d{6}$/.test(code)) return '请输入 6 位配对码。';
  if (target.trim() && !connect) return '连接地址需为局域网 IPv4:端口。';
  if (connect && connect.ip !== pair.ip) return '配对地址和连接地址必须属于同一部手机（相同 IP）。';
  if (connect && connect.port === pair.port) return '连接端口与配对端口不同，请查看无线调试主界面的连接端口。';
  return '';
}
export function shizukuOutcome(result: ShizukuResult, currentTask = true) {
  if (result.running === true) return { tone: 'success' as const, title: currentTask ? '本次启动已确认' : '上次启动已确认', message: result.message || '这是该次任务完成时的结果；当前状态可在手机的 Shizuku 应用中查看。' };
  if (result.paired === true) return { tone: 'warning' as const, title: '已配对，尚未启动', message: result.message || '请填写无线调试主界面的连接地址，再点击一键启动。' };
  return { tone: 'warning' as const, title: '尚未确认启动', message: result.message || '请检查手机上的无线调试与 Shizuku 状态后重试。' };
}
