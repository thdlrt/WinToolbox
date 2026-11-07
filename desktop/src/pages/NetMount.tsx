import { useCallback, useEffect, useRef, useState } from 'react';
import { Download, HardDrive, Pencil, Plus, RefreshCw, Trash2, Upload } from 'lucide-react';
import { errorText, native, rpc, subscribe, type BackendEvent, type Job } from '../api';
import { useApp } from '../context';
import { activeJob, Button, CheckField, Field, Notice, Section, Switch } from '../ui';
import '../netmount.css';

interface Drive {
  id: string; kind: 'sftp' | 'webdav'; name: string; host: string; port: number;
  username: string; remote_path: string; drive_letter: string; read_only: boolean;
  auto_mount: boolean; volname: string; cache_mode: string; tls: boolean; insecure_tls: boolean;
  webdav_vendor: string; has_password: boolean; needs_password: boolean;
  state: string; message: string; mounted: boolean; restarts: number;
}
interface Status {
  engine: { available: boolean; path: string; version: string | null };
  winfsp: { available: boolean; download_url: string };
  taken_letters: string[];
  drives: Drive[];
}
interface Candidate {
  key: string; kind: 'sftp' | 'webdav'; name: string; host: string; port: number;
  tls: boolean; remote_path: string; drive_letter: string; read_only: boolean; exists: boolean;
}
interface WebdavCandidate {
  kind: 'webdav'; name: string; host: string; port: number; tls: boolean;
  username: string; remote_path: string; drive_letter: string; has_password: boolean;
}

const emptyForm = {
  id: '', kind: 'sftp' as 'sftp' | 'webdav', name: '', host: '', port: '',
  username: '', password: '', remote_path: '/', drive_letter: '', read_only: false,
  auto_mount: false, tls: false, insecure_tls: false, cache_mode: 'writes', reuse_webdav_password: false,
};
type Form = typeof emptyForm;

const stateLabel = (drive: Drive) => {
  if (drive.state === 'mounted') return `已挂载 ${drive.drive_letter}:`;
  if (drive.state === 'reconnecting') return '重连中';
  if (drive.state === 'error') return '出错';
  if (drive.state === 'mounting') return '挂载中';
  return '未挂载';
};

export default function NetMountPage() {
  const { connected, jobs, track } = useApp();
  const [status, setStatus] = useState<Status>();
  const [failure, setFailure] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');
  const [form, setForm] = useState<Form>();
  const [importOpen, setImportOpen] = useState(false);
  const [candidates, setCandidates] = useState<Candidate[]>();
  const [chosen, setChosen] = useState<Record<string, boolean>>({});
  const [passwords, setPasswords] = useState<Record<string, string>>({});
  const [usernames, setUsernames] = useState<Record<string, string>>({});
  const alive = useRef(true);
  const reading = useRef(false);
  const mutation = useRef(false);
  const revision = useRef(0);
  const handled = useRef(new Set<string>());
  const submitted = useRef(new Set<string>());
  const relevant = jobs.filter(job => job.tool.startsWith('netmount.')) as (Job & { result?: unknown })[];

  const read = useCallback(async () => {
    if (reading.current || mutation.current) return;
    reading.current = true;
    const version = revision.current;
    try {
      const result = await rpc<Status>('netmount.status');
      if (alive.current && version === revision.current) { setStatus(result); setFailure(''); }
    } catch (reason) { if (alive.current && version === revision.current) setFailure(errorText(reason)); }
    finally { reading.current = false; }
  }, []);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (!connected) return;
    void read();
    const timer = setInterval(() => { if (!document.hidden) void read(); }, 5000);
    const off = subscribe((event: BackendEvent) => { if (event.type === 'netmount.state') void read(); });
    return () => { clearInterval(timer); off.then(fn => fn()); };
  }, [connected, read]);
  useEffect(() => {
    for (const job of relevant) {
      if (!submitted.current.has(job.id) || handled.current.has(job.id) || activeJob(job.status)) continue;
      handled.current.add(job.id);
      if (job.status === 'completed') void read();
      else setFailure(errorText(job.error || job.message || '操作未完成'));
    }
  }, [relevant, read]);

  const change = async (method: string, params: Record<string, unknown>, message = '') => {
    if (mutation.current) return;
    mutation.current = true; revision.current++; setBusy(method); setFailure(''); setNotice('');
    try {
      if (method === 'netmount.mount' || method === 'netmount.test') {
        const job = await rpc<Job>(method, params);
        submitted.current.add(job.id);
        track(job, message || '正在处理挂载任务。');
      } else {
        const result = await rpc<{ drives: Drive[] }>(method, params);
        if (alive.current) {
          setStatus(previous => previous && { ...previous, drives: result.drives });
          if (message) setNotice(message);
        }
      }
    } catch (reason) { if (alive.current) setFailure(errorText(reason)); }
    finally { mutation.current = false; if (alive.current) setBusy(''); }
  };
  const startImport = async () => {
    setBusy('import'); setFailure('');
    try {
      const result = await rpc<{ candidates: Candidate[] }>('netmount.import_raidrive');
      setCandidates(result.candidates);
      setChosen(Object.fromEntries(result.candidates.map(item => [item.key, !item.exists])));
      setPasswords({});
      setUsernames({});
      setImportOpen(true);
      if (!result.candidates.length) setNotice('没有在 RaiDrive 日志里找到挂载记录。');
    } catch (reason) { setFailure(errorText(reason)); }
    finally { if (alive.current) setBusy(''); }
  };
  const applyImport = async () => {
    const picked = (candidates || []).filter(item => chosen[item.key]).map(item => ({ ...item, username: usernames[item.key] || '' }));
    if (!picked.length) return;
    const missingUser = picked.find(item => !item.username?.trim());
    if (missingUser) { setFailure(`「${missingUser.name}」还没有填用户名；服务器会拒绝匿名访问（401）。`); return; }
    mutation.current = true; setBusy('import-apply'); setFailure(''); setNotice('');
    try {
      const result = await rpc<{ imported: number; error: string; drives: Drive[]; results: { key: string; ok: boolean; error?: string }[] }>(
        'netmount.import_raidrive.apply', { candidates: picked, passwords });
      if (alive.current) {
        setStatus(previous => previous && { ...previous, drives: result.drives });
        setNotice(result.error
          ? `已导入 ${result.imported} 条；有条目失败：${result.error}`
          : `已导入 ${result.imported} 条挂载，密码为空的条目请在编辑里补填。`);
        setImportOpen(false);
      }
    } catch (reason) { if (alive.current) setFailure(errorText(reason)); }
    finally { mutation.current = false; if (alive.current) setBusy(''); }
  };
  const fillFromWebdav = async () => {
    setFailure(''); setNotice('');
    try {
      const result = await rpc<{ candidate: WebdavCandidate | null }>('netmount.import_webdav_sync');
      if (!alive.current) return;
      if (!result.candidate) { setNotice('尚未配置 WebDAV 同步连接；请先在「设置 → WebDAV 备份」里保存。'); return; }
      const candidate = result.candidate;
      setForm({
        ...emptyForm, id: '', kind: 'webdav', name: candidate.name || 'WebDAV 同步',
        host: candidate.host || '', port: String(candidate.port || ''), username: candidate.username || '',
        remote_path: candidate.remote_path || '/', tls: !!candidate.tls,
        reuse_webdav_password: !!candidate.has_password,
      });
      setNotice('已带入 WebDAV 同步连接。' + (candidate.has_password ? '挂载将复用已保存的密码。' : '请填写密码。'));
    } catch (reason) { setFailure(errorText(reason)); }
  };
  const editDrive = (drive: Drive) => {
    setForm({
      id: drive.id, kind: drive.kind, name: drive.name, host: drive.host, port: String(drive.port),
      username: drive.username, password: '', remote_path: drive.remote_path, drive_letter: drive.drive_letter,
      read_only: drive.read_only, auto_mount: drive.auto_mount, tls: drive.tls, insecure_tls: drive.insecure_tls,
      cache_mode: drive.cache_mode, reuse_webdav_password: false,
    });
    setImportOpen(false);
  };
  const submitForm = async () => {
    if (!form) return;
    await change('netmount.save', {
      id: form.id || undefined, kind: form.kind, name: form.name, host: form.host,
      port: form.port || undefined, username: form.username,
      password: form.password || undefined, clear_password: false,
      remote_path: form.remote_path, drive_letter: form.drive_letter,
      read_only: form.read_only, auto_mount: form.auto_mount, tls: form.tls,
      insecure_tls: form.insecure_tls, cache_mode: form.cache_mode,
      reuse_webdav_password: form.reuse_webdav_password || undefined,
    }, form.id ? '已保存修改。' : '已添加挂载。');
    if (alive.current) setForm(undefined);
  };
  const patch = (update: Partial<Form>) => setForm(previous => previous && { ...previous, ...update });
  const mountedCount = status?.drives.filter(drive => drive.mounted).length ?? 0;

  return <div className="netmount-page">
    {!connected && <Notice>请在 WinToolbox 桌面程序中使用硬盘挂载。</Notice>}
    {failure && <Notice tone="warning">{failure}</Notice>}
    {notice && <Notice>{notice}</Notice>}
    {status && !status.engine.available && <Notice tone="warning">
      缺少挂载引擎（tools/netmount/rclone.exe）。请通过 `scripts/package.ps1 -PortableOnly -SkipInstall` 重新打包，或手动放置 rclone.exe。
    </Notice>}
    {status && !status.winfsp.available && <Notice tone="warning">
      尚未安装 WinFsp 用户态文件系统驱动，挂载前需要安装一次。
      <Button variant="secondary" onClick={() => void native.openExternal(status.winfsp.download_url)}><Download size={14} />打开 WinFsp 下载页</Button>
    </Notice>}
    <Section title="挂载的硬盘" description={status ? `${status.drives.length} 条配置，${mountedCount} 个已挂载。挂载随工具箱退出而卸载。` : ''} action={
      <div className="netmount-actions">
        <Button variant="ghost" disabled={!connected || !!busy} onClick={() => void read()}><RefreshCw size={15} />刷新</Button>
        <Button disabled={!connected || !!busy} onClick={() => { setForm({ ...emptyForm }); setImportOpen(false); }}><Plus size={15} />添加挂载</Button>
      </div>
    }>
      {status?.drives.length
        ? <div className="netmount-list">{status.drives.map(drive => <div key={drive.id} className="netmount-row">
          <HardDrive size={17} className={drive.mounted ? 'on' : ''} />
          <div className="netmount-title">
            <strong>{drive.name}</strong>
            <small>{drive.kind.toUpperCase()} · {drive.username ? `${drive.username}@` : ''}{drive.host}:{drive.port}{drive.remote_path === '/' ? '' : drive.remote_path} → {drive.drive_letter}:</small>
            {drive.message && <small className="netmount-message">{drive.message}</small>}
          </div>
          <span className={`netmount-badge ${drive.state}`}>{stateLabel(drive)}</span>
          <label className="netmount-auto" title="工具箱启动时自动挂载">
            自动挂载
            <Switch label="自动挂载" checked={drive.auto_mount} disabled={!connected || !!busy || drive.mounted} onChange={value => {
              mutation.current = true; revision.current++;
              rpc<{ drives: Drive[] }>('netmount.save', { ...drive, auto_mount: value })
                .then(result => { if (alive.current) setStatus(previous => previous && { ...previous, drives: result.drives }); })
                .catch(reason => { if (alive.current) setFailure(errorText(reason)); })
                .finally(() => { mutation.current = false; });
            }} />
          </label>
          {drive.mounted
            ? <Button variant="secondary" disabled={!connected || !!busy} busy={busy === 'netmount.unmount' && !!relevant.find(job => job.tool === 'netmount.unmount' && activeJob(job.status))} onClick={() => void change('netmount.unmount', { id: drive.id }, '正在卸载。')}>卸载</Button>
            : <Button disabled={!connected || !!busy} busy={busy === 'netmount.mount'} onClick={() => void change('netmount.mount', { id: drive.id }, '正在挂载，等待盘符出现。')}>挂载</Button>}
          <Button variant="ghost" disabled={!connected || !!busy} busy={busy === 'netmount.test'} onClick={() => void change('netmount.test', { id: drive.id }, '正在测试连接。')}>测试</Button>
          <Button variant="ghost" aria-label={`编辑 ${drive.name}`} disabled={!connected || !!busy || drive.mounted} onClick={() => editDrive(drive)}><Pencil size={15} /></Button>
          <Button variant="ghost" aria-label={`删除 ${drive.name}`} disabled={!connected || !!busy} onClick={() => {
            if (window.confirm(`删除挂载「${drive.name}」？挂载中的盘会先卸载。`)) void change('netmount.remove', { id: drive.id }, '已删除。');
          }}><Trash2 size={15} /></Button>
        </div>)}</div>
        : <p className="small-note">还没有挂载。点击「添加挂载」，或从 RaiDrive 迁移已有配置。</p>}
    </Section>

    <Section title="迁移与导入" action={
      <div className="netmount-actions">
        <Button variant="secondary" disabled={!connected || !!busy} busy={busy === 'import'} onClick={() => void startImport()}><Upload size={15} />从 RaiDrive 导入</Button>
        <Button variant="secondary" disabled={!connected || !!busy} onClick={() => void fillFromWebdav()}>从 WebDAV 同步导入</Button>
      </div>
    }>
      {importOpen && candidates && (candidates.length
        ? <div className="netmount-import">
          <p className="small-note">从 RaiDrive 服务日志恢复的挂载配置。RaiDrive 的密码和用户名无法读出，导入前请逐条填写；只填密码不填用户名会被服务器拒绝（401）。已存在的配置默认不勾选。</p>
          {candidates.map(item => <div key={item.key} className="netmount-import-row">
            <CheckField checked={!!chosen[item.key]} onChange={value => setChosen(previous => ({ ...previous, [item.key]: value }))}
              label={`${item.name}（${item.kind.toUpperCase()} ${item.host}:${item.port}${item.remote_path} → ${item.drive_letter || '?'}:）${item.exists ? ' · 已存在' : ''}`} />
            <input placeholder="用户名" value={usernames[item.key] || ''}
              onChange={event => setUsernames(previous => ({ ...previous, [item.key]: event.target.value }))} />
            <input type="password" placeholder="密码" value={passwords[item.key] || ''}
              onChange={event => setPasswords(previous => ({ ...previous, [item.key]: event.target.value }))} />
          </div>)}
          <div className="netmount-import-actions">
            <Button disabled={!!busy} busy={busy === 'import-apply'} onClick={() => void applyImport()}>导入选中的挂载</Button>
            <Button variant="ghost" onClick={() => setImportOpen(false)}>取消</Button>
          </div>
        </div>
        : <p className="small-note">RaiDrive 日志中没有可导入的挂载记录。</p>)}
      {!importOpen && <p className="small-note">「从 RaiDrive 导入」读取本机 RaiDrive 的服务日志，恢复名称、类型、地址、盘符和只读设置；RaiDrive 的密码无法导出，需要补填。「从 WebDAV 同步导入」把 WebDAV 备份使用的连接直接填进添加表单，可复用已保存的密码。</p>}
    </Section>

    {form && <Section title={form.id ? `编辑挂载：${form.name}` : '添加挂载'} action={<Button variant="ghost" onClick={() => setForm(undefined)}>取消</Button>}>
      <div className="netmount-form">
        <Field label="类型">
          <select value={form.kind} onChange={event => patch({ kind: event.target.value as 'sftp' | 'webdav' })}>
            <option value="sftp">SFTP（SSH 文件传输）</option>
            <option value="webdav">WebDAV</option>
          </select>
        </Field>
        <Field label="名称"><input value={form.name} onChange={event => patch({ name: event.target.value })} placeholder="例如 NAS" /></Field>
        <Field label={form.kind === 'sftp' ? '主机名或 IP' : '主机名、IP 或完整地址'} hint={form.kind === 'webdav' ? '可直接粘贴 http://host:port 形式的地址' : undefined}>
          <input value={form.host} onChange={event => patch({ host: event.target.value })} placeholder="192.168.1.10" />
        </Field>
        <Field label="端口" hint={form.kind === 'sftp' ? '默认 22' : '默认 80，HTTPS 默认 443'}>
          <input value={form.port} onChange={event => patch({ port: event.target.value })} placeholder={form.kind === 'sftp' ? '22' : '80'} inputMode="numeric" />
        </Field>
        <Field label="用户名" hint={!form.username.trim() && (form.password || form.reuse_webdav_password) ? '已填写密码但用户名为空，服务器通常需要用户名' : undefined}>
          <input value={form.username} onChange={event => patch({ username: event.target.value })} autoComplete="off" />
        </Field>
        <Field label="密码" hint={form.id ? '留空保留已保存的密码' : form.reuse_webdav_password ? '将复用 WebDAV 同步的密码' : undefined}>
          <input type="password" value={form.password} disabled={form.reuse_webdav_password} onChange={event => patch({ password: event.target.value })} autoComplete="new-password" />
        </Field>
        <Field label="远端路径" hint="服务器上作为盘符根目录的路径">
          <input value={form.remote_path} onChange={event => patch({ remote_path: event.target.value })} placeholder="/home/user" />
        </Field>
        <Field label="盘符" hint={status?.taken_letters.length ? `已被占用：${status.taken_letters.join(' ')}` : undefined}>
          <input value={form.drive_letter} maxLength={2} onChange={event => patch({ drive_letter: event.target.value.toUpperCase() })} placeholder="X" />
        </Field>
        {form.kind === 'webdav' && <>
          <CheckField checked={form.tls} label="使用 HTTPS" onChange={value => patch({ tls: value })} />
          <CheckField checked={form.insecure_tls} label="跳过证书校验" hint="自签名证书的 NAS 需要勾选" onChange={value => patch({ insecure_tls: value })} />
        </>}
        <CheckField checked={form.read_only} label="只读挂载" onChange={value => patch({ read_only: value })} />
        <CheckField checked={form.auto_mount} label="自动挂载" hint="工具箱启动时自动挂载此盘" onChange={value => patch({ auto_mount: value })} />
        <Field label="缓存模式" hint="writes 可保证 Office 等软件正常保存；off 最省磁盘">
          <select value={form.cache_mode} onChange={event => patch({ cache_mode: event.target.value })}>
            <option value="off">off（关闭缓存）</option>
            <option value="minimal">minimal（最小）</option>
            <option value="writes">writes（推荐）</option>
            <option value="full">full（全部）</option>
          </select>
        </Field>
      </div>
      <div className="netmount-form-actions">
        <Button disabled={!connected || !!busy || !form.name || !form.host || !form.drive_letter} busy={busy === 'netmount.save'} onClick={() => void submitForm()}>保存</Button>
      </div>
    </Section>}

    <details className="netmount-help"><summary>设置与说明</summary>
      <p className="small-note">挂载使用随包的 rclone 与系统安装的 WinFsp，把 SFTP/WebDAV 目录以网络盘符呈现，行为对齐 RaiDrive：网络盘模式、盘符固定、断线自动重连（最多 10 次）、可只读。缓存写在工具箱数据目录。</p>
      <p className="small-note">密码以当前用户 DPAPI 加密保存，只存本机；任务参数与日志中不出现明文。挂载随工具箱退出而卸载，勾选「自动挂载」的条目在工具箱下次启动时自动重新挂载。</p>
      <p className="small-note">同一盘符只能给一条挂载；系统已占用的盘符在挂载时会明确报错。删除挂载会先卸载正在运行的盘。</p>
    </details>
  </div>;
}
