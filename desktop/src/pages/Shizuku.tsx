import { useCallback, useEffect, useRef, useState } from 'react';
import { Play, RefreshCw, Smartphone } from 'lucide-react';
import { errorText, rpc, type Job } from '../api';
import { useApp } from '../context';
import { activeJob, Button, dateText, Field, Notice, Progress, Section, Status } from '../ui';
import { shizukuEndpoint, shizukuOutcome, shizukuPairError, type ShizukuDevice, type ShizukuResult } from '../shizuku';
import '../shizuku.css';

interface ShizukuStatus { adb_available: boolean; adb_path?: string; last_target?: string }
type ShizukuJob = Job & { result?: ShizukuResult };
const completed = (status: string) => ['completed', 'succeeded', 'success'].includes(status);
const jobNames: Record<string, string> = { 'shizuku.discover': '发现手机', 'shizuku.start': '启动 Shizuku', 'shizuku.pair': '配对并启动' };

export default function ShizukuPage() {
  const { connected, jobs, track, refreshJobs } = useApp();
  const [info, setInfo] = useState<ShizukuStatus>();
  const [target, setTarget] = useState('');
  const [pairTarget, setPairTarget] = useState('');
  const [pairConnect, setPairConnect] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState('');
  const [cancelling, setCancelling] = useState(false);
  const [failure, setFailure] = useState('');
  const [devices, setDevices] = useState<ShizukuDevice[]>([]);
  const [discoveryNotice, setDiscoveryNotice] = useState('');
  const [discovered, setDiscovered] = useState(false);
  const alive = useRef(true);
  const targetEdited = useRef(false);
  const targetRevision = useRef(0);
  const initiated = useRef(new Map<string, number>());
  const handled = useRef(new Set<string>());
  const relevant = jobs.filter(job => job.tool.startsWith('shizuku.')) as ShizukuJob[];
  const running = relevant.find(job => activeJob(job.status));
  const task = running || relevant[0];
  const usable = connected && info?.adb_available === true && !busy && !running;
  const connectDevices = devices.filter(device => device.kind === 'connect' && !!shizukuEndpoint(device.address));
  const pairingDevices = devices.filter(device => device.kind === 'pairing' && !!shizukuEndpoint(device.address));
  const report = useCallback((reason: unknown) => { if (alive.current) setFailure(errorText(reason)); }, []);
  const readStatus = useCallback(async () => {
    setBusy('status'); setFailure('');
    try { const status = await rpc<ShizukuStatus>('shizuku.status'); if (alive.current) { setInfo(status); if (!targetEdited.current && status.last_target) setTarget(status.last_target); } }
    catch (reason) { report(reason); }
    finally { if (alive.current) setBusy(''); }
  }, [report]);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { if (connected) void readStatus(); }, [connected, readStatus]);
  useEffect(() => {
    const discovery = relevant.find(job => job.tool === 'shizuku.discover' && completed(job.status));
    if (discovery && !handled.current.has(discovery.id)) {
      handled.current.add(discovery.id); setDevices(discovery.result?.devices || []); setDiscovered(true); setDiscoveryNotice(discovery.result?.notice || '');
    }
    for (const job of relevant) {
      if (!initiated.current.has(job.id) || handled.current.has(job.id) || !completed(job.status)) continue;
      handled.current.add(job.id);
      const endpoint = job.result?.target && shizukuEndpoint(job.result.target);
      if (endpoint && initiated.current.get(job.id) === targetRevision.current) { targetEdited.current = true; setTarget(endpoint.address); }
    }
  }, [relevant]);
  const submit = async (method: string, params: Record<string, unknown>) => {
    const revision = targetRevision.current;
    setBusy(method); setFailure('');
    try {
      const job = await rpc<ShizukuJob>(method, params);
      if (alive.current) { if (method !== 'shizuku.discover') initiated.current.set(job.id, revision); track(job, method === 'shizuku.discover' ? '正在发现手机。' : '任务已开始，可在本页查看进度。'); }
    } catch (reason) { report(reason); }
    finally { if (alive.current) setBusy(''); }
  };
  const start = () => {
    const endpoint = shizukuEndpoint(target);
    if (!endpoint) { report('请填写局域网 IPv4:连接端口，例如 192.168.1.20:37123。'); return; }
    void submit('shizuku.start', { target: endpoint.address });
  };
  const pair = () => {
    const invalid = shizukuPairError(pairTarget, code, pairConnect);
    if (invalid) { report(invalid); return; }
    const submittedCode = code; setCode('');
    void submit('shizuku.pair', { pair_target: shizukuEndpoint(pairTarget)!.address, code: submittedCode, ...(pairConnect.trim() ? { target: shizukuEndpoint(pairConnect)!.address } : {}) });
  };
  const cancel = async () => {
    if (!running) return;
    setCancelling(true); setFailure('');
    try { await rpc('jobs.cancel', { id: running.id }); await refreshJobs(); }
    catch (reason) { report(reason); }
    finally { if (alive.current) setCancelling(false); }
  };
  const editTarget = (value: string) => { targetEdited.current = true; targetRevision.current++; setTarget(value); };
  const outcome = task && completed(task.status) && task.tool !== 'shizuku.discover' ? shizukuOutcome(task.result || {}, initiated.current.has(task.id)) : undefined;
  return <div className="shizuku-page">
    <Section><div className="shizuku-discovery"><Field label="发现的手机"><select aria-label="发现的手机" disabled={!usable || !connectDevices.length} value={connectDevices.some(device => device.address === target) ? target : ''} onChange={event => editTarget(event.target.value)}><option value="">{discovered ? connectDevices.length ? '选择连接地址' : '未发现连接地址，可手动填写' : '点击发现手机'}</option>{connectDevices.map(device => <option key={`${device.address}-${device.service || ''}`} value={device.address}>{device.label || device.address}{device.label && device.label !== device.address ? ` · ${device.address}` : ''}</option>)}</select></Field><Button disabled={!usable} busy={busy === 'shizuku.discover'} onClick={() => void submit('shizuku.discover', {})}><RefreshCw size={15} />{discovered ? '刷新发现' : '发现手机'}</Button></div>
      {discoveryNotice && <p className="small-note shizuku-note">{discoveryNotice}</p>}
      <Field label="连接地址" hint="手机无线调试主界面的 IP 地址和端口。"><input aria-label="连接地址" value={target} disabled={!connected || !!busy || !!running} onChange={event => editTarget(event.target.value)} placeholder="192.168.1.20:37123" autoComplete="off" spellCheck={false} /></Field>
      <div className="shizuku-start"><Button variant="primary" disabled={!usable || !target.trim()} onClick={start}><Play size={15} />一键启动</Button></div>
      <details className="shizuku-pairing"><summary>首次使用：无线配对</summary><p className="small-note">手机点“使用配对码配对设备”。配对端口与上面的连接端口不同。</p>
        {pairingDevices.length > 0 && <Field label="发现的配对窗口"><select aria-label="发现的配对窗口" disabled={!usable} value={pairingDevices.some(device => device.address === pairTarget) ? pairTarget : ''} onChange={event => setPairTarget(event.target.value)}><option value="">选择配对地址</option>{pairingDevices.map(device => <option key={`${device.address}-${device.service || ''}`} value={device.address}>{device.label || device.address}{device.label && device.label !== device.address ? ` · ${device.address}` : ''}</option>)}</select></Field>}
        <div className="form-grid"><Field label="配对地址"><input aria-label="配对地址" value={pairTarget} disabled={!usable} onChange={event => setPairTarget(event.target.value)} placeholder="192.168.1.20:39817" autoComplete="off" spellCheck={false} /></Field><Field label="6 位配对码"><input aria-label="6 位配对码" type="password" inputMode="numeric" autoComplete="off" maxLength={6} value={code} disabled={!usable} onChange={event => setCode(event.target.value.replace(/\D/g, '').slice(0, 6))} placeholder="手机显示的配对码" /></Field></div>
        <Field label="配对后连接地址（可选）" hint="留空时自动查找同一手机的连接端口；无法唯一确定时，配对后再手动连接。"><input aria-label="配对后连接地址" value={pairConnect} disabled={!usable} onChange={event => setPairConnect(event.target.value)} placeholder="同一手机的 IP:连接端口" autoComplete="off" spellCheck={false} /></Field>
        <div className="shizuku-start"><Button disabled={!usable || !pairTarget.trim() || code.length !== 6} onClick={pair}>配对并启动</Button></div>
      </details>
    </Section>
    {info && !info.adb_available && <Notice tone="warning">ADB 组件未就绪，请更新完整工具箱后重试。<Button variant="ghost" disabled={!connected || !!busy} onClick={() => void readStatus()}>重新检查</Button></Notice>}
    {failure && <Notice tone="warning">{failure}{!info && <Button variant="ghost" disabled={!connected || !!busy} onClick={() => void readStatus()}>重新检查</Button>}</Notice>}
    {task && <Section className="shizuku-task"><div className="shizuku-task-heading"><span><Smartphone size={16} />最近任务 · {jobNames[task.tool] || 'Shizuku'}<small>{dateText(task.created_at)}</small></span><Status value={task.status} /></div>{activeJob(task.status) ? <><p>{task.message || '正在处理…'}</p><Progress value={task.progress} /><div className="shizuku-start"><Button variant="ghost" busy={cancelling} disabled={!connected || task.status === 'cancelling'} onClick={() => void cancel()}>取消</Button></div></> : outcome ? <Notice tone={outcome.tone}><strong>{outcome.title}</strong><p>{outcome.message}</p>{task.result?.target && <small>{task.result.target}</small>}</Notice> : completed(task.status) ? <p>{task.result?.notice || `发现 ${task.result?.devices?.filter(device => device.kind === 'connect').length || 0} 个连接地址。`}</p> : <Notice tone="warning">{task.error ? errorText(task.error) : task.message || (['cancelled', 'canceled'].includes(task.status) ? '任务已取消。' : '任务未完成，请检查后重试。')}</Notice>}</Section>}
    <p className="small-note shizuku-help">手机需 Android 11 或更新版本，已安装并打开 Shizuku。开启“开发者选项 → 无线调试”，手机与电脑连接同一局域网。无线调试重新开启后端口可能变化，请刷新发现并重新选择。</p>
  </div>;
}
