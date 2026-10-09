import { useEffect, useRef, useState } from 'react';
import { Download, FolderOpen, QrCode, RefreshCw } from 'lucide-react';
import { errorText, native, rpc, type Job } from '../api';
import { useApp } from '../context';
import { Button, Field, Notice, Section } from '../ui';
import ToolJobs from '../ToolJobs';
import '../netease.css';

type Account = { uid: number; nickname: string };
type Status = { account: Account | null; local_uid: number | null; authenticated: boolean; message: string; ncmdump_available: boolean };
type Playlist = { id: number; name: string; track_count: number; creator_uid: number; special_type: number };
type Track = { id: number; name: string; artists: string[]; album: string; resolved: boolean };
type Library = { id: number; name: string; tracks: Track[]; complete: boolean; track_count: number };
type QR = { key: string; qr_image: string };

export default function NetEasePage() {
  const { connected, track } = useApp();
  const [status, setStatus] = useState<Status>();
  const [paths, setPaths] = useState<string[]>([]);
  const [output, setOutput] = useState('');
  const [format, setFormat] = useState('mp3');
  const [bitrate, setBitrate] = useState(320);
  const [uid, setUid] = useState('');
  const [playlists, setPlaylists] = useState<Playlist[]>([]);
  const [playlistId, setPlaylistId] = useState('');
  const [library, setLibrary] = useState<Library>();
  const [selected, setSelected] = useState<number[]>([]);
  const [query, setQuery] = useState('');
  const [qr, setQr] = useState<QR>();
  const [qrMessage, setQrMessage] = useState('');
  const [busy, setBusy] = useState('');
  const [failure, setFailure] = useState('');
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const action = async (name: string, work: () => Promise<void>) => {
    setBusy(name); setFailure('');
    try { await work(); } catch (error) { if (alive.current) setFailure(errorText(error)); }
    finally { if (alive.current) setBusy(''); }
  };
  useEffect(() => {
    if (connected) void rpc<Status>('netease.status').then(value => { if (alive.current) { setStatus(value); setUid(String(value.account?.uid || value.local_uid || '')); } }).catch(error => { if (alive.current) setFailure(errorText(error)); });
  }, [connected]);
  useEffect(() => {
    if (!qr || !connected) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = await rpc<{ code: number; message: string; account?: Account }>('netease.qr.check', { key: qr.key });
        if (stopped) return;
        setQrMessage(result.message);
        if (result.code === 803) {
          setQr(undefined);
          setStatus(await rpc<Status>('netease.status'));
          setUid(String(result.account?.uid || ''));
          setPlaylists([]); setLibrary(undefined); setSelected([]);
          return;
        }
        if (result.code === 800) { setQr(undefined); return; }
        if (result.code === 8821) { setFailure(result.message); setQr(undefined); return; }
        timer = setTimeout(poll, 2500);
      } catch (error) { if (!stopped) { setFailure(errorText(error)); setQr(undefined); } }
    };
    timer = setTimeout(poll, 2500);
    return () => { stopped = true; clearTimeout(timer); };
  }, [qr, connected]);
  const disabled = !connected || !!busy;
  const common = { output_dir: output, format, bitrate };
  const user = uid ? { uid: Number(uid) } : {};
  const visible = library?.tracks.filter(item => `${item.name} ${item.artists.join(' ')} ${item.album} ${item.id}`.toLowerCase().includes(query.toLowerCase())) || [];
  const loadLibrary = async (likes: boolean) => {
    const value = await rpc<Library>(likes ? 'netease.likes' : 'netease.tracks', likes ? user : { id: Number(playlistId) });
    setLibrary(value); setSelected([]); setQuery('');
    if (!value.complete) setFailure(value.tracks.length !== value.track_count ? `歌单标称 ${value.track_count} 首，接口实际返回 ${value.tracks.length} 个歌曲 ID；请登录后重试。` : '部分歌曲详情缺失，已保留对应歌曲 ID；请登录后重试。');
  };
  return <div className="netease-page">
    <Section title="NCM 转换"><div className="netease-actions">
      <Button disabled={disabled} onClick={() => void action('pick', async () => { const value = await native.files(true, [{ name: '网易云 NCM', extensions: ['ncm'] }]); if (value.length) setPaths(value); })}><FolderOpen size={15} />选择 NCM 文件</Button>
      <Button disabled={disabled} onClick={() => void action('pick', async () => { const value = await native.directory(); if (value) setPaths([value]); })}>选择文件夹</Button>
      <span className="muted">{paths.length ? `${paths.length} 个输入` : '未选择文件'}</span>
    </div>{paths.length > 0 && <details><summary>输入路径</summary>{paths.map(path => <div key={path} className="small-note">{path}</div>)}</details>}
      <div className="form-grid"><Field label="输出目录"><div className="netease-path"><input value={output} onChange={e => setOutput(e.target.value)} placeholder="选择或填写保存目录" /><Button disabled={disabled} onClick={() => void action('folder', async () => { const path = await native.directory(); if (path) setOutput(path); })}>选择</Button></div></Field><Field label="输出格式"><select value={format} onChange={e => setFormat(e.target.value)}><option value="mp3">统一转 MP3</option><option value="original">还原原始格式（MP3 / FLAC）</option></select></Field>{format === 'mp3' && <Field label="MP3 码率"><select value={bitrate} onChange={e => setBitrate(Number(e.target.value))}>{[128, 192, 256, 320].map(value => <option key={value} value={value}>{value} kbps</option>)}</select></Field>}</div>
      <Button variant="primary" disabled={disabled || !paths.length || !output} busy={busy === 'convert'} onClick={() => void action('convert', async () => { track(await rpc<Job>('netease.convert', { ...common, paths }), '已开始 NCM 转换'); })}>开始转换</Button>
      <p className="small-note">使用 ncmdump 1.5.1，保留原文件，同名输出自动编号。原音频为 MP3 时直接保留；FLAC 转 MP3 使用所选码率。</p>
    </Section>
    <Section title="网易云账号与歌单"><div className="netease-actions"><span>{status?.authenticated ? `${status.account?.nickname} · ${status.account?.uid}` : '尚未扫码登录'}{!status?.authenticated && status?.local_uid ? ` · 本机用户 ${status.local_uid}` : ''}</span>
      <Button disabled={disabled} busy={busy === 'qr'} onClick={() => void action('qr', async () => { setQr(await rpc<QR>('netease.qr.start')); setQrMessage('用网易云音乐手机 App 扫码并确认登录'); })}><QrCode size={15} />扫码登录</Button>
      {status?.authenticated && <Button disabled={disabled} onClick={() => void action('disconnect', async () => { await rpc('netease.disconnect'); setStatus(await rpc<Status>('netease.status')); setLibrary(undefined); setPlaylists([]); setSelected([]); })}>断开连接</Button>}
    </div>{status?.message && <p className="small-note">{status.message}</p>}{qr && <div className="netease-qr"><img src={qr.qr_image} alt="网易云音乐登录二维码" /><div><p>{qrMessage}</p><Button onClick={() => setQr(undefined)}>取消扫码</Button></div></div>}{!qr && qrMessage === '已登录' && <p className="small-note">已登录</p>}
      <div className="form-grid"><Field label="用户 ID" hint="可读取公开歌单；私密列表和下载权限使用扫码登录的账号。"><input inputMode="numeric" value={uid} onChange={e => { setUid(e.target.value.replace(/\D/g, '')); setPlaylists([]); setLibrary(undefined); setSelected([]); }} /></Field><Field label="歌单"><select value={playlistId} onChange={e => { setPlaylistId(e.target.value); setLibrary(undefined); setSelected([]); }}><option value="">请选择歌单</option>{playlists.map(item => <option key={item.id} value={item.id}>{item.name} · {item.track_count} 首</option>)}</select></Field></div>
      <div className="netease-actions"><Button disabled={disabled || !uid} onClick={() => void action('playlists', async () => { const value = await rpc<{ playlists: Playlist[] }>('netease.playlists', user); setPlaylists(value.playlists); setPlaylistId(String(value.playlists[0]?.id || '')); setLibrary(undefined); setSelected([]); })}><RefreshCw size={15} />获取歌单</Button>
        <Button disabled={disabled || !uid} busy={busy === 'likes'} onClick={() => void action('likes', () => loadLibrary(true))}>我喜欢的音乐</Button>
        <Button disabled={disabled || !playlistId} busy={busy === 'tracks'} onClick={() => void action('tracks', () => loadLibrary(false))}>读取所选歌单</Button>
        <Button disabled={disabled || !uid} onClick={() => void action('export', async () => { track(await rpc<Job>('netease.export', { ...user, kind: 'playlists', output_dir: output }), '已开始导出歌单列表'); })}>导出歌单列表</Button>
      </div>
      {library && <><div className="netease-actions"><strong>{library.name} · {library.tracks.length}/{library.track_count} 首</strong><Button disabled={disabled} onClick={() => void action('export', async () => { track(await rpc<Job>('netease.export', { kind: 'playlist', id: library.id, output_dir: output }), '已开始导出歌曲列表'); })}>导出 JSON / CSV</Button></div>
        <div className="netease-actions"><input aria-label="搜索歌曲" placeholder="搜索歌名、歌手或 ID" value={query} onChange={e => setQuery(e.target.value)} /><Button onClick={() => setSelected(visible.map(x => x.id))} disabled={disabled}>选择筛选结果</Button><Button onClick={() => setSelected([])}>清空选择</Button><Button variant="primary" disabled={disabled || !output || !selected.length} onClick={() => void action('download', async () => { track(await rpc<Job>('netease.download', { ...common, ids: selected }), '已开始下载所选歌曲'); })}><Download size={15} />下载所选 {selected.length} 首</Button></div>
        <div className="netease-table"><table><thead><tr><th>选择</th><th>歌曲</th><th>歌手</th><th>专辑</th></tr></thead><tbody>{visible.map(item => <tr key={item.id}><td><input type="checkbox" aria-label={`选择 ${item.name || item.id}`} checked={selected.includes(item.id)} onChange={e => setSelected(old => e.target.checked ? [...old, item.id] : old.filter(id => id !== item.id))} /></td><td>{item.name || `详情缺失 · ${item.id}`}</td><td>{item.artists.join(' / ')}</td><td>{item.album}</td></tr>)}</tbody></table></div></>}
      <p className="small-note">下载使用网易云返回的账号可用下载地址。无权限、下架和试听会列入失败报告。扫码凭据由 Windows 加密保存在本机。</p>
    </Section>
    {failure && <Notice tone="warning">{failure}</Notice>}
    <ToolJobs scope="netease" title="音乐任务" />
  </div>;
}
