import { useEffect, useState } from 'react';
import { rpc, errorText } from '../api';
import { useApp } from '../context';
import { Button, Notice } from '../ui';
import { visiblePolling } from '../visiblePolling';
import '../clipboard.css';

type Config = { days: number; enabled: boolean; available: boolean; error: string };
type Item = { id: string; kind: string; preview: string; thumbnail: string; width: number; height: number; copied_at: number };
type Result = { items: Item[]; more: boolean; config: Config };
type Detail = { kind: string; text: string; image: string };
export default function ClipboardPage() {
  const { connected, success } = useApp();
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('all');
  const [offset, setOffset] = useState(0);
  const [result, setResult] = useState<Result>();
  const [days, setDays] = useState('3');
  const [failure, setFailure] = useState('');
  const [readFailure, setReadFailure] = useState('');
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<Detail>();
  const [confirmClear, setConfirmClear] = useState(false);
  useEffect(() => {
    if (!connected) return;
    let disposed = false;
    void rpc<Config>('clipboard.settings').then(c => { if (!disposed) setDays(String(c.days)); }).catch(e => { if (!disposed) setFailure(errorText(e)); });
    return () => { disposed = true; };
  }, [connected]);
  useEffect(() => {
    if (!connected) return;
    let disposed = false;
    const timer = setTimeout(() => {
      stop = visiblePolling(async () => {
        try { const r = await rpc<Result>('clipboard.list', { query, kind, offset }); if (!disposed) { setResult(r); setReadFailure(''); } }
        catch (e) { if (!disposed) setReadFailure(errorText(e)); }
      }, 2000);
    }, 180);
    let stop = () => {};
    return () => { disposed = true; clearTimeout(timer); stop(); };
  }, [connected, query, kind, offset]);
  const act = async (method: string, params: Record<string, unknown> = {}) => {
    setBusy(true); setFailure('');
    try {
      await rpc(`clipboard.${method}`, params);
      setResult(await rpc<Result>('clipboard.list', { query, kind, offset }));
      if (method === 'copy') success('已复制');
      if (method === 'clear') { setOffset(0); setConfirmClear(false); setDetail(undefined); }
    } catch (e) { setFailure(errorText(e)); }
    finally { setBusy(false); }
  };
  const view = async (id: string) => {
    setBusy(true);
    try { setDetail(await rpc<Detail>('clipboard.get', { id })); }
    catch (e) { setFailure(errorText(e)); }
    finally { setBusy(false); }
  };
  return <div className="clipboard-page">
    <div className="clipboard-controls">
      <label><input type="checkbox" checked={result?.config.enabled ?? false} disabled={!result || busy || !result.config.available} onChange={e => void act('settings', { enabled: e.target.checked })} />记录剪贴板</label>
      <label>保留 <input aria-label="保留天数" type="number" min={1} max={365} value={days} onChange={e => setDays(e.target.value)} /> 天</label>
      <Button disabled={!result || busy || !Number.isInteger(Number(days)) || Number(days)<1 || Number(days)>365 || Number(days) === result.config.days} onClick={() => void act('settings', { days: Number(days) })}>保存</Button>
      <Button disabled={!result || busy} onClick={() => setConfirmClear(true)}>清空历史</Button>
    </div>
    <span className="muted">仅保存在本机；工具箱退出后停止记录。缩短保留时间会立即清理过期记录。</span>
    {readFailure && <Notice tone="warning">{readFailure}</Notice>}
    {failure && <Notice tone="warning">{failure}</Notice>}
    {result?.config.error && <Notice tone="warning">{result.config.error}</Notice>}
    {result && !result.config.available && <Notice tone="warning">剪贴板采集仅支持 Windows 桌面。</Notice>}
    {confirmClear && <div className="clipboard-controls" role="alert"><span>清空全部历史记录？此操作无法撤销。</span><Button variant="danger" disabled={busy} onClick={() => void act('clear', { confirm: true })}>确认清空</Button><Button onClick={() => setConfirmClear(false)}>取消</Button></div>}
    <div className="clipboard-controls">
      <input className="clipboard-search" aria-label="搜索剪贴板文字" placeholder="搜索文字内容" value={query} onChange={e => { setQuery(e.target.value); setOffset(0); }} />
      <select aria-label="剪贴板分类" value={kind} onChange={e => { setKind(e.target.value); setOffset(0); }}><option value="all">全部</option><option value="text">文字</option><option value="image">图片</option></select>
    </div>
    <div className="clipboard-items">{result?.items.map(item => <article key={item.id} className="clipboard-item">
      <button className="clipboard-preview" onClick={() => void view(item.id)} disabled={busy} aria-label="查看完整内容">{item.kind === 'image' ? <img src={`data:image/png;base64,${item.thumbnail}`} alt={`图片 ${item.width} × ${item.height}`} /> : <pre>{item.preview}</pre>}</button>
      <div className="clipboard-item-footer"><span className="muted">{item.kind === 'image' ? `图片 ${item.width} × ${item.height}` : '文字'} · {new Date(item.copied_at * 1000).toLocaleString()}</span><Button disabled={busy} onClick={() => void act('copy', { id: item.id })}>复制</Button><Button disabled={busy} onClick={() => { setDetail(undefined); void act('delete', { id: item.id }); }}>删除</Button></div>
    </article>)}</div>
    {result && (offset > 0 || result.more) && <div className="clipboard-controls"><Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</Button><span>{offset / 50 + 1}</span><Button disabled={!result.more} onClick={() => setOffset(offset + 50)}>下一页</Button></div>}
    {detail && <div className="clipboard-modal" role="dialog" aria-modal="true" aria-label="剪贴板内容" onClick={() => setDetail(undefined)}><div className="clipboard-detail" onClick={e => e.stopPropagation()}><Button onClick={() => setDetail(undefined)}>关闭</Button>{detail.kind === 'image' ? <img src={`data:image/png;base64,${detail.image}`} alt="剪贴板原图" /> : <pre>{detail.text}</pre>}</div></div>}
  </div>;
}
