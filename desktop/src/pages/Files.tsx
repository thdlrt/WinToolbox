import { useState } from 'react';
import { ArrowRight, Check, FolderInput, Languages, ScanSearch } from 'lucide-react';
import { native, rpc } from '../api';
import { useApp } from '../context';
import { basename, Button, CheckField, cx, Empty, Field, Notice, PathInput, Section } from '../ui';

interface Operation { source: string; target: string; status: string; reason?: string }
interface Plan { plan_id: string; operations: Operation[]; warnings?: string[] }
export default function FilesPage() {
  const { connected, run, error } = useApp();
  const [action, setAction] = useState<'move' | 'translate'>('move');
  const [source, setSource] = useState('');
  const [dest, setDest] = useState('');
  const [suffix, setSuffix] = useState('');
  const [recursive, setRecursive] = useState(false);
  const [prefixOnly, setPrefixOnly] = useState(false);
  const [plan, setPlan] = useState<Plan>();
  const [busy, setBusy] = useState('');
  const [done, setDone] = useState<{ moved?: number | unknown[]; journal_path?: string; [key: string]: unknown }>();
  const invalidate = () => { setPlan(undefined); setDone(undefined); };
  const preview = async () => { setBusy('preview'); setDone(undefined); const result = await run(() => rpc<Plan>('files.preview', { action, source_dir: source, ...(dest ? { dest_dir: dest } : {}), suffix, recursive, prefix_only: prefixOnly })); if (result) setPlan(result); setBusy(''); };
  const apply = async () => { if (!plan) return; setBusy('apply'); const result = await run(() => rpc<{ moved?: number | unknown[]; journal_path?: string }>('files.apply', { plan_id: plan.plan_id })); if (result) { setDone(result); setPlan(undefined); } setBusy(''); };
  const ready = plan?.operations.filter(item => item.status === 'ready').length || 0;
  const conflicts = plan?.operations.filter(item => item.status === 'conflict').length || 0;

  return <>
    
    <div className="mode-cards"><button className={cx(action === 'move' && 'selected')} onClick={() => { setAction('move'); invalidate(); }}><FolderInput size={25} /><span><strong>按后缀整理</strong></span>{action === 'move' && <Check size={18} />}</button><button className={cx(action === 'translate' && 'selected')} onClick={() => { setAction('translate'); invalidate(); }}><Languages size={25} /><span><strong>翻译文件名</strong></span>{action === 'translate' && <Check size={18} />}</button></div>
    <Section title={action === 'move' ? '选择整理范围' : '选择翻译范围'}><div className="form-grid"><Field label="源文件夹"><PathInput value={source} onChange={value => { setSource(value); invalidate(); }} placeholder="选择需要整理的文件夹" onError={error} /></Field>{action === 'move' ? <Field label="目标文件夹"><PathInput value={dest} onChange={value => { setDest(value); invalidate(); }} placeholder="选择文件移入的位置" onError={error} /></Field> : <Field label="文件名匹配后缀（可选）" hint="留空处理全部文件；例如 .mp4。"><input value={suffix} onChange={e => { setSuffix(e.target.value); invalidate(); }} placeholder="全部文件" /></Field>}</div>{action === 'move' && <Field label="匹配后缀" hint="例如 .srt；只匹配符合后缀的文件。"><input value={suffix} onChange={e => { setSuffix(e.target.value); invalidate(); }} placeholder=".srt" /></Field>}<div className="inline-checks"><CheckField checked={recursive} onChange={value => { setRecursive(value); invalidate(); }} label="包含子文件夹" />{action === 'translate' && <CheckField checked={prefixOnly} onChange={value => { setPrefixOnly(value); invalidate(); }} label="只翻译名称前缀" hint="翻译第一个下划线之前的文字，保留其余部分。" />}</div>{action === 'translate' && <Notice>使用已配置的翻译模型；预览后才会重命名。</Notice>}<div className="section-footer"><span className="muted">预览不会修改文件。</span><Button variant="primary" disabled={!connected || !source.trim() || (action === 'move' && (!dest.trim() || !suffix.trim()))} busy={busy === 'preview'} onClick={preview}><ScanSearch size={16} />生成预览</Button></div></Section>
    {plan ? <Section title="操作预览" description={`${ready} 项可执行${conflicts ? ` · ${conflicts} 项冲突` : ''}`} action={<span className="muted">共 {plan.operations.length} 项</span>}>{plan.warnings?.map(item => <Notice tone="warning" key={item}>{item}</Notice>)}{conflicts > 0 && <Notice tone="warning">存在目标重名等冲突。请调整目录或文件名后重新预览，避免覆盖已有文件。</Notice>}{plan.operations.length ? <div className="operation-table"><div className="operation-head"><span>当前路径</span><span /><span>目标路径</span><span>状态</span></div>{plan.operations.map((item, index) => <div className="operation-row" key={index}><div title={item.source}><strong>{basename(item.source)}</strong><small>{item.source}</small></div><ArrowRight size={16} /><div title={item.target}><strong>{basename(item.target)}</strong><small>{item.target}</small></div><span className={cx('operation-status', item.status)} title={item.reason}>{({ ready: '待执行', conflict: '冲突', unchanged: '不变', skipped: '跳过' }[item.status] || item.status)}</span></div>)}</div> : <Empty title="没有匹配的文件">检查文件夹、后缀和是否包含子文件夹。</Empty>}<div className="section-footer"><span className="muted">执行后会保存操作清单。</span><Button variant="primary" busy={busy === 'apply'} disabled={!ready || conflicts > 0} onClick={apply}>执行 {ready} 项操作<ArrowRight size={15} /></Button></div></Section> : done ? <Notice tone="success">文件整理已完成{done.moved !== undefined ? `，处理 ${Array.isArray(done.moved) ? done.moved.length : done.moved} 项` : ''}。{done.journal_path && <button className="inline-link" onClick={() => void run(() => native.open(done.journal_path!))}>查看操作清单</button>}</Notice> : null}
  </>;
}
