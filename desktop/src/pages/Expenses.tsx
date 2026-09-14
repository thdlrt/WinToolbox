import { useCallback, useEffect, useRef, useState } from 'react';
import { FilePlus2, Filter, Paperclip, Plus, RefreshCw, Save, Search, Trash2 } from 'lucide-react';
import { errorText, native, rpc, type Job } from '../api';
import { useApp } from '../context';
import { activeJob, Button, Empty, Field, IconButton, Modal, Notice, Progress, Section } from '../ui';
import { acceptExpenseUpdate, attachmentKinds, changeExpenseType, emptyExpenseFilters, expenseDirty, expenseDraft, expenseMoney, expensePeriodValid, expenseQueryParams, expenseSettlement, expenseStatus, expenseValidation, localDate, newExpenseInPeriod, normalizeExpenseEntry, type AttachmentKind, type ExpenseAttachment, type ExpenseDraft, type ExpenseEntry, type ExpenseSummary, type ExpensePeriod, type ExpenseFilters, type ExpenseType } from '../expensesState';
import '../expenses.css';

interface MonthResult { month: string | null; start_month: string; end_month: string; summary_scope: string; items: ExpenseEntry[]; summary: ExpenseSummary[]; categories: string[]; queryKey?: string }
interface Transition { proceed: () => void; cancel?: () => void }
type AttachJob = Job & { result?: { id: string; entry: ExpenseEntry } };
const completed = (status: string) => ['completed', 'succeeded', 'success'].includes(status);

export default function ExpensesPage() {
  const { connected, jobs, track, refreshJobs, setBeforeNavigate } = useApp();
  const [period, setPeriod] = useState<ExpensePeriod>(() => ({ start_month: localDate().slice(0, 7), end_month: localDate().slice(0, 7) }));
  const [filters, setFilters] = useState<ExpenseFilters>(emptyExpenseFilters);
  const [moreFilters, setMoreFilters] = useState(false);
  const [data, setData] = useState<MonthResult>();
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [item, setItem] = useState<ExpenseEntry>();
  const [draft, setDraft] = useState<ExpenseDraft>(() => newExpenseInPeriod(period));
  const [base, setBase] = useState<ExpenseDraft>(() => newExpenseInPeriod(period));
  const [kind, setKind] = useState<AttachmentKind>('invoice');
  const [busy, setBusy] = useState('');
  const [cancelling, setCancelling] = useState(false);
  const [failure, setFailure] = useState('');
  const [transition, setTransition] = useState<Transition>();
  const [deleting, setDeleting] = useState<ExpenseAttachment | 'entry'>();
  const mounted = useRef(true);
  const listToken = useRef(0);
  const view = useRef(0);
  const requests = useRef(new Map<string, string>());
  const handled = useRef(new Set<string>());
  const dirty = editing && expenseDirty(draft, base);
  const current = useRef({ period, filters, item, draft, dirty, busy, editing }); current.current = { period, filters, item, draft, dirty, busy, editing };
  const attachJobs = jobs.filter(job => job.tool === 'expenses.attach') as AttachJob[];
  const running = item && attachJobs.find(job => activeJob(job.status) && (job.params.id === item.id || requests.current.get(job.id) === item.id));
  const locked = !!busy || !!running;
  const report = useCallback((reason: unknown) => { if (mounted.current) setFailure(errorText(reason)); }, []);
  const cancelAttachment = async () => {
    if (!running || cancelling) return;
    setCancelling(true);
    try { await rpc('jobs.cancel', { id: running.id }); await refreshJobs(); }
    catch (reason) { report(reason); }
    finally { if (mounted.current) setCancelling(false); }
  };
  const load = useCallback(async () => {
    const token = ++listToken.current; const params = expenseQueryParams(current.current.period, current.current.filters); const queryKey = JSON.stringify(params);
    setLoading(true); setFailure('');
    try { const result = await rpc<MonthResult>('expenses.list', params); if (mounted.current && token === listToken.current && JSON.stringify(expenseQueryParams(current.current.period, current.current.filters)) === queryKey) setData({ ...result, items: result.items.map(normalizeExpenseEntry), queryKey }); }
    catch (reason) { if (mounted.current && token === listToken.current) report(reason); }
    finally { if (mounted.current && token === listToken.current) setLoading(false); }
  }, [report]);
  const adopt = useCallback((value: ExpenseEntry) => {
    value = normalizeExpenseEntry(value); const next = expenseDraft(value); setItem(value); setDraft(next); setBase(next);
    current.current = { ...current.current, item: value, draft: next, dirty: false };
  }, []);
  const closeEditor = useCallback(() => {
    if (current.current.busy) return;
    const close = () => { view.current++; setEditing(false); setItem(undefined); setFailure(''); current.current = { ...current.current, dirty: false, editing: false, item: undefined }; };
    if (current.current.dirty) setTransition({ proceed: close }); else close();
  }, []);
  const closeTransition = useCallback(() => { transition?.cancel?.(); setTransition(undefined); }, [transition]);
  const closeDelete = useCallback(() => { if (!current.current.busy) setDeleting(undefined); }, []);
  const open = async (value?: ExpenseEntry) => {
    view.current++; setFailure(''); setKind(value?.entry_type === 'income' ? 'payment' : 'invoice'); setItem(value); const next = value ? expenseDraft(value) : newExpenseInPeriod(period);
    setDraft(next); setBase(next); setEditing(true); current.current = { ...current.current, item: value, draft: next, dirty: false, editing: true };
    if (!value) return;
    const token = view.current; setBusy('open');
    try { const fresh = await rpc<ExpenseEntry>('expenses.get', { id: value.id }); if (mounted.current && token === view.current) adopt(fresh); }
    catch (reason) { report(reason); }
    finally { if (mounted.current && token === view.current) setBusy(''); }
  };
  const saveDraft = useCallback(async () => {
    const snapshot = current.current;
    if (snapshot.item && !snapshot.dirty) return snapshot.item;
    const invalid = expenseValidation(snapshot.draft); if (invalid) { report(invalid); return; }
    const token = view.current;
    const value = await rpc<ExpenseEntry>('expenses.save', { ...snapshot.draft, amount: snapshot.draft.amount.trim(), ...(snapshot.item ? { id: snapshot.item.id, revision: snapshot.item.revision } : {}) });
    if (mounted.current && token === view.current) { adopt(value); await load(); }
    return value;
  }, [adopt, load, report]);
  const save = async () => { setBusy('save'); setFailure(''); try { await saveDraft(); } catch (reason) { report(reason); } finally { if (mounted.current) setBusy(''); } };
  const saveAndContinue = async () => {
    setBusy('save'); setFailure('');
    try { const value = await saveDraft(); if (value) { const next = transition; setTransition(undefined); next?.proceed(); } }
    catch (reason) { report(reason); }
    finally { if (mounted.current) setBusy(''); }
  };
  const discard = () => { const next = transition; setTransition(undefined); current.current.dirty = false; next?.proceed(); };
  const attach = async () => {
    const invalid = expenseValidation(current.current.draft); if (invalid) { report(invalid); return; }
    setBusy('attach'); setFailure('');
    try {
      const paths = await native.files(true, [{ name: '发票与凭证', extensions: ['pdf', 'png', 'jpg', 'jpeg', 'webp', 'ofd'] }]);
      if (!paths.length) return;
      const saved = await saveDraft(); if (!saved) return;
      const job = await rpc<AttachJob>('expenses.attach', { id: saved.id, revision: saved.revision, paths, kind });
      if (!mounted.current) return;
      requests.current.set(job.id, saved.id); track(job, '正在保存附件副本。');
    } catch (reason) { report(reason); }
    finally { if (mounted.current) setBusy(''); }
  };
  const remove = async () => {
    if (!item || !deleting) return;
    setBusy('delete'); setFailure('');
    try {
      if (deleting === 'entry') {
        await rpc('expenses.delete', { id: item.id, revision: item.revision });
        setEditing(false); setItem(undefined); current.current = { ...current.current, dirty: false, editing: false, item: undefined };
      } else {
        const result = await rpc<{ entry: ExpenseEntry }>('expenses.attachment.delete', { id: item.id, revision: item.revision, attachment_id: deleting.id });
        const fresh = normalizeExpenseEntry(result.entry); setItem(fresh); current.current.item = fresh;
      }
      setDeleting(undefined); await load();
    } catch (reason) { report(reason); }
    finally { if (mounted.current) setBusy(''); }
  };
  const openAttachment = async (attachment: ExpenseAttachment) => {
    if (!item) return;
    try { const result = await rpc<{ path: string }>('expenses.attachment', { id: item.id, attachment_id: attachment.id }); await native.open(result.path); }
    catch (reason) { report(reason); }
  };
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; listToken.current++; view.current++; }; }, []);
  useEffect(() => {
    if (!connected || !expensePeriodValid(period)) return;
    setFailure(''); const timer = setTimeout(() => void load().catch(report), filters.query ? 180 : 0);
    return () => clearTimeout(timer);
  }, [connected, period, filters, load, report]);
  useEffect(() => {
    setBeforeNavigate?.(() => {
      if (current.current.busy) return false;
      if (!current.current.dirty) return true;
      return new Promise<boolean>(resolve => setTransition({ proceed: () => resolve(true), cancel: () => resolve(false) }));
    });
    return () => setBeforeNavigate?.(undefined);
  }, [setBeforeNavigate]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (current.current.dirty) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, []);
  useEffect(() => {
    for (const job of attachJobs) {
      if (activeJob(job.status)) { if (typeof job.params.id === 'string') requests.current.set(job.id, job.params.id); continue; }
      const parentId = requests.current.get(job.id);
      if (!parentId || handled.current.has(job.id)) continue;
      handled.current.add(job.id);
      if (!completed(job.status)) { report(job.error || job.message || '附件保存失败，请重试。'); continue; }
      void load().catch(report);
      const fresh = job.result?.entry && normalizeExpenseEntry(job.result.entry);
      if (fresh && acceptExpenseUpdate(current.current.item, fresh)) { setItem(fresh); current.current.item = fresh; }
    }
  }, [attachJobs, load, report]);
  const changePeriod = (patch: Partial<ExpensePeriod>) => { const next = { ...period, ...patch }; current.current.period = next; setPeriod(next); listToken.current++; };
  const changeFilter = (patch: Partial<ExpenseFilters>) => { const next = { ...filters, ...patch }; if (patch.entry_type === 'income') next.status = ''; current.current.filters = next; setFilters(next); listToken.current++; };
  const change = (patch: Partial<ExpenseDraft>) => setDraft(value => ({ ...value, ...patch }));
  const setType = (entry_type: ExpenseType) => { setDraft(value => changeExpenseType(value, entry_type, !item)); setKind(entry_type === 'income' ? 'payment' : 'invoice'); };
  const periodValid = expensePeriodValid(period);
  const queryKey = JSON.stringify(expenseQueryParams(period, filters));
  const visibleData = data?.start_month === period.start_month && data?.end_month === period.end_month && periodValid ? data : undefined;
  const visibleItems = visibleData?.queryKey === queryKey ? visibleData.items : [];
  const filtering = Object.values(filters).some(Boolean);
  const categories = [...new Set(['AI订阅', '其他报销', '其他', '转账收入', ...(visibleData?.categories || [])])];
  return <div className="expenses-page">
    <div className="expenses-toolbar"><div className="expenses-period"><Field label="起始月份"><input type="month" aria-label="起始月份" value={period.start_month} onChange={event => changePeriod({ start_month: event.target.value })} /></Field><span>至</span><Field label="结束月份"><input type="month" aria-label="结束月份" value={period.end_month} onChange={event => changePeriod({ end_month: event.target.value })} /></Field><Button variant="ghost" onClick={() => { const month = localDate().slice(0, 7); changePeriod({ start_month: month, end_month: month }); }}>本月</Button></div><div><IconButton label="刷新记账" disabled={!connected || loading || !periodValid} onClick={() => void load().catch(report)}><RefreshCw size={16} /></IconButton><Button variant="primary" disabled={!connected || !periodValid} onClick={() => void open()}><Plus size={16} />新增条目</Button></div></div>
    {!periodValid && <Notice tone="warning">请选择有效的起止月份，结束月份不能早于起始月份。</Notice>}
    {failure && !editing && !transition && !deleting && <Notice tone="warning">{failure}</Notice>}
    <p className="small-note expenses-summary-hint">应均摊金额 =（消费总额 − 已报销总额）÷ 2，按分四舍五入；等待报销暂不扣除。区间汇总不受列表筛选影响，按条目日期归月。</p>
    {!!visibleData?.summary.length && <div className="expenses-totals">{visibleData.summary.map(value => {
      const settlement = expenseSettlement(value.settlement_remaining);
      return <Section key={value.currency} className="expenses-currency"><div className="expenses-currency-heading"><span className="expenses-currency-label">{value.currency}</span><small>等待报销 {expenseMoney(value.pending)}</small></div><dl><div><dt>消费总额</dt><dd>{expenseMoney(value.expense_total)}</dd></div><div><dt>已报销总额</dt><dd>{expenseMoney(value.reimbursed)}</dd></div><div title="（消费总额 − 已报销总额）÷ 2，按分四舍五入；等待报销暂不扣除"><dt>应均摊金额</dt><dd>{expenseMoney(value.share_due)}</dd></div><div><dt>转账收入</dt><dd>{expenseMoney(value.transfer_income)}</dd></div><div className={`expenses-settlement ${settlement.kind}`} title="应均摊金额 − 转账收入"><dt>{settlement.label}</dt><dd>{settlement.amount}</dd></div></dl></Section>;
    })}</div>}
    <div className="expenses-filters"><div className="search-input"><Search size={15} /><input aria-label="搜索条目" placeholder="搜索名称、分类或备注" value={filters.query} onChange={event => changeFilter({ query: event.target.value })} /></div><select aria-label="筛选类型" value={filters.entry_type} onChange={event => changeFilter({ entry_type: event.target.value })}><option value="">全部类型</option><option value="expense">支出</option><option value="income">转账收入</option></select><Button variant="ghost" aria-expanded={moreFilters} onClick={() => setMoreFilters(!moreFilters)}><Filter size={14} />更多筛选{[filters.status, filters.category, filters.currency].filter(Boolean).length > 0 && ` · ${[filters.status, filters.category, filters.currency].filter(Boolean).length}`}</Button>{filtering && <Button variant="ghost" onClick={() => { current.current.filters = emptyExpenseFilters(); setFilters(emptyExpenseFilters()); listToken.current++; }}>清除筛选</Button>}</div>
    {moreFilters && <div className="expenses-more-filters"><Field label="报销状态"><select aria-label="筛选报销状态" disabled={filters.entry_type === 'income'} value={filters.status} onChange={event => changeFilter({ status: event.target.value })}><option value="">全部状态</option>{Object.entries(expenseStatus).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field><Field label="分类"><select aria-label="筛选分类" value={filters.category} onChange={event => changeFilter({ category: event.target.value })}><option value="">全部分类</option>{categories.map(value => <option key={value}>{value}</option>)}</select></Field><Field label="币种"><select aria-label="筛选币种" value={filters.currency} onChange={event => changeFilter({ currency: event.target.value })}><option value="">全部币种</option>{['CNY', 'USD', 'EUR', 'HKD'].map(value => <option key={value}>{value}</option>)}</select></Field></div>}
    <Section className="expenses-list">{visibleItems.length ? <><div className="expenses-list-heading"><span>日期</span><span>名称 / 类型 / 分类</span><span>金额</span><span>报销状态</span><span>附件</span></div>{visibleItems.map(value => <button className="expenses-row" key={value.id} disabled={loading} onClick={() => void open(value)}><time>{value.date}</time><span className="expenses-row-title"><strong>{value.title}</strong><small>{value.entry_type === 'income' ? '转账收入' : '支出'}{value.category && value.category !== '转账收入' ? ` · ${value.category}` : ''}</small></span><span className={`expenses-amount ${value.entry_type}`}>{value.entry_type === 'income' ? '+' : '−'}{expenseMoney(value.amount)} <small>{value.currency}</small></span><span className={`expenses-status ${value.status}`}>{value.entry_type === 'income' ? '—' : expenseStatus[value.status as keyof typeof expenseStatus]}</span><span className="expenses-attachment-count"><Paperclip size={13} />{value.attachments.length}</span></button>)}</> : <Empty title={!periodValid ? '请选择有效月份区间' : loading ? '正在加载…' : failure ? '读取失败，请重试' : filtering ? '没有符合筛选条件的条目' : '区间内暂无条目'} />}</Section>    {editing && !transition && !deleting && <Modal title={item ? '编辑条目' : '新增条目'} onClose={closeEditor} wide><div className="modal-body expenses-editor">
      <fieldset className="plain-fieldset" disabled={!!busy || !connected}><Field label="条目类型"><select aria-label="条目类型" value={draft.entry_type} onChange={event => setType(event.target.value as ExpenseType)}><option value="expense">支出</option><option value="income">转账收入</option></select></Field><Field label="名称"><input value={draft.title} maxLength={200} placeholder="如 ChatGPT 订阅" onChange={event => change({ title: event.target.value })} /></Field><div className="form-grid"><Field label="日期"><input type="date" value={draft.date} onChange={event => change({ date: event.target.value })} /></Field><Field label="分类"><input list="expense-categories" value={draft.category} maxLength={80} onChange={event => change({ category: event.target.value })} /><datalist id="expense-categories"><option value="AI订阅" /><option value="其他报销" /><option value="其他" /><option value="转账收入" /></datalist></Field></div><div className="expenses-money-fields"><Field label="金额"><input inputMode="decimal" value={draft.amount} placeholder="0.00" onChange={event => change({ amount: event.target.value })} /></Field><Field label="币种"><select value={draft.currency} onChange={event => change({ currency: event.target.value })}>{['CNY', 'USD', 'EUR', 'HKD'].map(value => <option key={value}>{value}</option>)}</select></Field>{draft.entry_type === 'expense' && <Field label="报销状态"><select aria-label="报销状态" value={draft.status} onChange={event => change({ status: event.target.value as ExpenseDraft['status'] })}>{Object.entries(expenseStatus).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>}</div><Field label="备注"><textarea rows={2} maxLength={5000} value={draft.notes} onChange={event => change({ notes: event.target.value })} /></Field></fieldset>
      <div className="expenses-attachments"><div className="expenses-attachments-heading"><h3>附件{item?.attachments.length ? ` · ${item.attachments.length}` : ''}</h3><select aria-label="附件类型" disabled={locked} value={kind} onChange={event => setKind(event.target.value as AttachmentKind)}>{Object.entries(attachmentKinds).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><Button disabled={!connected || locked} busy={busy === 'attach'} onClick={attach}><FilePlus2 size={14} />{dirty || !item ? '保存并添加附件' : '添加附件'}</Button></div><p className="small-note">保留本机副本，支持 PDF、图片和 OFD；单个最多 50 MiB，每批最多 200 MiB。</p>{item?.attachments.map(attachment => <div className="expenses-attachment" key={attachment.id}><button onClick={() => void openAttachment(attachment)} title="打开附件副本"><Paperclip size={14} /><span>{attachment.original_name}</span></button><small>{attachmentKinds[attachment.kind]}</small><IconButton label={`删除附件 ${attachment.original_name}`} disabled={locked} onClick={() => setDeleting(attachment)}><Trash2 size={14} /></IconButton></div>)}{running && <div className="expenses-progress"><span>{running.message || '正在保存附件副本…'}</span><Button variant="ghost" busy={cancelling} disabled={!connected || running.status === 'cancelling'} onClick={cancelAttachment}>取消</Button><Progress value={running.progress} /></div>}</div>
      {failure && <Notice tone="warning">{failure}</Notice>}
      <div className="modal-footer expenses-editor-footer">{item && <Button variant="ghost" disabled={locked} onClick={() => setDeleting('entry')}><Trash2 size={14} />删除条目</Button>}<span className="flex-spacer" />{dirty && <small>未保存</small>}<Button disabled={!!busy} onClick={closeEditor}>关闭</Button><Button variant="primary" disabled={!connected || locked || (!dirty && !!item)} busy={busy === 'save'} onClick={save}><Save size={14} />保存</Button></div>
    </div></Modal>}
    {transition && <Modal title="保存修改？" onClose={closeTransition}><div className="modal-body"><p>当前条目有未保存的修改。</p>{failure && <Notice tone="warning">{failure}</Notice>}<div className="modal-footer"><Button disabled={!!busy} onClick={closeTransition}>继续编辑</Button><Button disabled={!!busy} onClick={discard}>不保存</Button><Button variant="primary" disabled={locked} busy={busy === 'save'} onClick={saveAndContinue}>保存并继续</Button></div></div></Modal>}
    {deleting && <Modal title={deleting === 'entry' ? '删除条目？' : '删除附件？'} onClose={closeDelete}><div className="modal-body"><p className="break-word">{deleting === 'entry' ? `删除“${item?.title}”及其附件。` : `从此条目移除“${deleting.original_name}”，原始文件不受影响。`}</p>{failure && <Notice tone="warning">{failure}</Notice>}<div className="modal-footer"><Button disabled={!!busy} onClick={closeDelete}>取消</Button><Button variant="danger" busy={busy === 'delete'} onClick={remove}>确认删除</Button></div></div></Modal>}
  </div>;
}
