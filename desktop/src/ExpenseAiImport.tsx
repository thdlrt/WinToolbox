import { useEffect, useRef, useState } from 'react';
import { FilePlus2, X } from 'lucide-react';
import { errorText, native, rpc, type Job } from './api';
import { Button, Field, Modal, Notice, Progress } from './ui';
import { attachmentFileName, expenseMoney, type ExpenseDraft, type ExpenseProject, type PendingExpenseAttachment } from './expensesState';

export interface AiExpenseCandidate { id: string; draft: ExpenseDraft; warnings: string[]; source_ids: string[]; confirmed_entry_id?: string }
export interface AiExpenseBatch { id: string; created_at: number; items: AiExpenseCandidate[]; sources: { id: string; name: string; path?: string; attachable: boolean }[] }
export interface AiExpenseReview { batch_id: string; item_id: string; draft: ExpenseDraft; attachments: PendingExpenseAttachment[] }
type ExtractJob = Job & { result?: AiExpenseBatch };

export default function ExpenseAiImport({ projects, projectId, hidden, saved, onReview, onClose }: {
  projects: ExpenseProject[]; projectId: string; hidden: boolean; saved?: { batch_id: string; item_id: string; entry_id: string };
  onReview: (review: AiExpenseReview) => void; onClose: () => void;
}) {
  const [text, setText] = useState('');
  const [paths, setPaths] = useState<string[]>([]);
  const [project, setProject] = useState(projects.some(p => p.id === projectId && !p.archived) ? projectId : projects.find(p => !p.archived)?.id ?? '');
  const [batches, setBatches] = useState<AiExpenseBatch[]>([]);
  const [status, setStatus] = useState<{ configured: boolean; model?: string; provider?: string; error?: string }>();
  const [job, setJob] = useState<ExtractJob>();
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState('');
  const active = useRef(true);
  const jobId = useRef('');
  useEffect(() => {
    active.current = true;
    void Promise.all([rpc<{ batches: AiExpenseBatch[] }>('expenses.ai.drafts'), rpc<typeof status>('expenses.ai.status')]).then(([value, state]) => { if (active.current) { setBatches(value.batches); setStatus(state); } }).catch(reason => { if (active.current) setFailure(errorText(reason)); });
    return () => { active.current = false; if (jobId.current) void rpc('jobs.cancel', { id: jobId.current }).catch(() => {}); };
  }, []);
  useEffect(() => {
    if (!saved) return;
    setBatches(old => old.map(batch => batch.id !== saved.batch_id ? batch : { ...batch, items: batch.items.map(item => item.id !== saved.item_id ? item : { ...item, confirmed_entry_id: saved.entry_id }) }));
    void rpc<AiExpenseBatch>('expenses.ai.confirmed', saved).then(value => { if (active.current) setBatches(old => old.map(batch => batch.id === value.id ? value : batch)); }).catch(reason => { if (active.current) setFailure(`账目已保存，但草稿标记未更新：${errorText(reason)}`); });
  }, [saved]);
  const files = async () => {
    try {
      const selected = await native.files(true, [{ name: '票据、图片与文字文件', extensions: ['pdf', 'png', 'jpg', 'jpeg', 'webp', 'txt', 'csv', 'docx', 'xlsx', 'ofd'] }]);
      if (!active.current) return;
      setPaths(old => Array.from(new Set([...old, ...selected])).slice(0, 5));
      if (selected.length + paths.length > 5) setFailure('一次最多选择5个文件，其余文件请分批识别。');
    } catch (reason) { setFailure(errorText(reason)); }
  };
  const extract = async () => {
    if (busy) return;
    setBusy(true); setFailure('');
    try {
      let value = await rpc<ExtractJob>('expenses.ai.extract', { project_id: project, text, paths });
      jobId.current = value.id;
      if (active.current) setJob(value);
      while (['queued', 'running', 'cancelling'].includes(value.status)) {
        await new Promise(resolve => setTimeout(resolve, 650));
        if (!active.current) return;
        value = await rpc<ExtractJob>('jobs.get', { id: value.id });
        setJob(value);
      }
      jobId.current = '';
      if (!active.current) return;
      if (!['completed', 'succeeded'].includes(value.status) || !value.result) throw new Error(errorText(value.error || value.message || '识别未完成'));
      if (!value.result.items.length) setFailure('没有识别到可确认的交易，请补充清晰凭证或文字。');
      else { setBatches(old => [value.result!, ...old]); setText(''); setPaths([]); }
    } catch (reason) { if (active.current) setFailure(errorText(reason)); }
    finally { if (active.current) setBusy(false); }
  };
  const review = (batch: AiExpenseBatch, item: AiExpenseCandidate) => {
    const attachments = batch.sources.filter(source => source.attachable && source.path && item.source_ids.includes(source.id)).map(source => ({ path: source.path!, kind: item.draft.entry_type === 'income' ? 'payment' as const : 'invoice' as const }));
    onReview({ batch_id: batch.id, item_id: item.id, draft: { ...item.draft }, attachments });
  };
  if (hidden) return null;
  return <Modal title="AI 记账" wide onClose={onClose}><div className="modal-body expense-ai">
    <div className="expense-ai-input">
      <Field label="归入项目"><select value={project} onChange={e => setProject(e.target.value)} disabled={busy}>{projects.filter(p => !p.archived).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
      <Field label="粘贴文字"><textarea rows={4} maxLength={32000} placeholder="粘贴账单、订单详情或付款记录，也可以只添加文件和图片" value={text} onChange={e => setText(e.target.value)} disabled={busy} /></Field>
      <div className="expense-ai-files">{paths.map(path => <div key={path}><span>{attachmentFileName(path)}</span><button aria-label={`移除 ${attachmentFileName(path)}`} disabled={busy} onClick={() => setPaths(old => old.filter(p => p !== path))}><X size={15} /></button></div>)}</div>
      <div className="expense-ai-actions"><Button disabled={busy} onClick={() => void files()}><FilePlus2 size={15} />添加文件 / 图片</Button><Button variant="primary" busy={busy} disabled={!status?.configured || !project || (!text.trim() && !paths.length)} onClick={() => void extract()}>识别为草稿</Button></div>
      <p className="small-note">{status?.configured ? `使用 ${status.provider} · ${status.model} 识别。` : status?.error || '正在读取模型配置…'} 材料会发送给所选模型；生成草稿后，仍需逐笔人工确认。</p>
      <p className="small-note">最多5个文件，单个10 MiB、合计20 MiB；每份 PDF 最多6页。PDF、图片和 OFD 可随确认保存为附件，文字文件仅用于识别。</p>
    </div>
    {busy && <div className="expenses-progress" role="status">{job?.message || '正在提交识别任务…'}<Progress value={job?.progress ?? 0} /></div>}
    {failure && <Notice tone="warning">{failure}</Notice>}
    {batches.length > 0 && <div className="expense-ai-drafts"><h3>待确认草稿</h3><p className="small-note">草稿仅保存在本机，未确认不会入账或同步。请核对金额、日期、币种与均摊选项。</p>{batches.map(batch => <div key={batch.id} className="expense-ai-batch">{batch.items.map(item => <div key={item.id} className={`expense-ai-candidate${item.confirmed_entry_id ? ' confirmed' : ''}`}>
      <div><strong>{item.draft.title || '名称待补充'}</strong><span>{item.draft.date || '日期待确认'} · {item.draft.amount ? expenseMoney(item.draft.amount) : '金额待确认'} {item.draft.currency || '币种待确认'} · {item.draft.entry_type === 'income' ? '收入' : '支出'}</span>{!item.confirmed_entry_id && item.warnings.map((warning, index) => <small key={index}>{warning}</small>)}</div>
      <Button disabled={busy || !!item.confirmed_entry_id} onClick={() => review(batch, item)}>{item.confirmed_entry_id ? '已入账' : '编辑并确认'}</Button>
    </div>)}</div>)}</div>}
    <div className="modal-footer"><span className="small-note">确认后使用原条目编辑器保存，可修改全部字段和附件。</span><Button onClick={onClose}>关闭</Button></div>
  </div></Modal>;
}
