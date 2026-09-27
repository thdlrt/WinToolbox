import { useCallback, useEffect, useRef, useState } from 'react';
import { FilePlus2, Filter, Paperclip, Plus, RefreshCw, Save, Search, Trash2 } from 'lucide-react';
import { errorText, native, rpc, type Job } from '../api';
import { useApp } from '../context';
import { activeJob, Button, Empty, Field, IconButton, Modal, Notice, Progress, Section } from '../ui';
import { attachmentFileName, pendingExpenseAttachments, recentExpensePeriod, type PendingExpenseAttachment, attachmentKinds, changeExpenseType, emptyExpenseFilters, expenseDirty, expenseDraft, expenseMoney, expensePeriodValid, expenseQueryParams, expenseSettlement, expenseStatus, expenseValidation, localDate, newExpenseInPeriod, normalizeExpenseEntry, type AttachmentKind, type ExpenseAttachment, type ExpenseDraft, type ExpenseEntry, type ExpenseSummary, type ExpensePeriod, type ExpenseFilters, type ExpenseType } from '../expensesState';
import '../expenses.css';
import LedgerSync from '../LedgerSync';
import ExpenseProjects from '../ExpenseProjects';
import { legacyExpenseProject, type ExpenseProject } from '../expensesState';

interface MonthResult { month: string | null; start_month: string; end_month: string; summary_scope: string; settlement_mode?: 'general' | 'half'; items: ExpenseEntry[]; summary: ExpenseSummary[]; categories: string[]; queryKey?: string }
interface Transition { proceed: () => void; cancel?: () => void }
type CommitJob = Job & { result?: { entry?: ExpenseEntry } };
type ExportJob = Job & { result?: { path?: string; rows?: number } };
const completed = (status: string) => ['completed', 'succeeded', 'success'].includes(status);

export default function ExpensesPage() {
  const { connected, jobs, track, setBeforeNavigate } = useApp();
  const monthPeriod = (): ExpensePeriod => { const now = new Date(); const month = localDate(now).slice(0, 7); return { start_month: month, end_month: month, start_date: `${month}-01`, end_date: localDate(new Date(now.getFullYear(), now.getMonth() + 1, 0)) }; };
  const [period, setPeriod] = useState<ExpensePeriod>(monthPeriod);
  const [filters, setFilters] = useState<ExpenseFilters>(() => ({ ...emptyExpenseFilters(), project_id: legacyExpenseProject }));
  const [moreFilters, setMoreFilters] = useState(false);
  const [projects, setProjects] = useState<ExpenseProject[]>([]);
  const [manageProjects, setManageProjects] = useState(false);
  const [exportFormat, setExportFormat] = useState('xlsx');
  const [exporting, setExporting] = useState(false);
  const [data, setData] = useState<MonthResult>();
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [item, setItem] = useState<ExpenseEntry>();
  const [draft, setDraft] = useState<ExpenseDraft>(() => newExpenseInPeriod(period));
  const [base, setBase] = useState<ExpenseDraft>(() => newExpenseInPeriod(period));
  const [kind, setKind] = useState<AttachmentKind>('invoice');
  const [busy, setBusy] = useState('');
  const [commitJob, setCommitJob] = useState<CommitJob>();
  const [pendingAttachments, setPendingAttachments] = useState<PendingExpenseAttachment[]>([]);
  const [removedAttachments, setRemovedAttachments] = useState<string[]>([]);
  const [failure, setFailure] = useState('');
  const [transition, setTransition] = useState<Transition>();
  const [deleting, setDeleting] = useState(false);
  const mounted = useRef(true);
  const listToken = useRef(0);
  const view = useRef(0);
  const saveRequest = useRef<{ payload: string; id: string } | undefined>(undefined);
  const pendingCommit = !!commitJob && activeJob(commitJob.status);
  const dirty = editing && (expenseDirty(draft, base) || pendingAttachments.length > 0 || removedAttachments.length > 0);
  const current = useRef({ period, filters, item, draft, dirty, busy: busy || (pendingCommit ? 'commit' : ''), editing, pendingAttachments, removedAttachments }); current.current = { period, filters, item, draft, dirty, busy: busy || (pendingCommit ? 'commit' : ''), editing, pendingAttachments, removedAttachments };
  const latestExport = jobs.filter(job => job.tool === 'expenses.export').sort((a, b) => b.created_at.localeCompare(a.created_at))[0] as ExportJob | undefined;
  const exportPath = latestExport?.artifacts?.[0]?.path || latestExport?.result?.path;
  const exportRunning = !!latestExport && activeJob(latestExport.status);
  const locked = !!busy || pendingCommit;
  const report = useCallback((reason: unknown) => { if (mounted.current) setFailure(errorText(reason)); }, []);
  const load = useCallback(async () => {
    const token = ++listToken.current; const params = expenseQueryParams(current.current.period, current.current.filters); const queryKey = JSON.stringify(params);
    setLoading(true); if (!current.current.editing) setFailure('');
    try { const result = await rpc<MonthResult>('expenses.list', params); if (mounted.current && token === listToken.current && JSON.stringify(expenseQueryParams(current.current.period, current.current.filters)) === queryKey) setData({ ...result, items: result.items.map(normalizeExpenseEntry), queryKey }); }
    catch (reason) { if (mounted.current && token === listToken.current) report(reason); }
    finally { if (mounted.current && token === listToken.current) setLoading(false); }
  }, [report]);
  const loadProjects = useCallback(async () => { const result = await rpc<{ items: ExpenseProject[] }>('expenses.projects.list'); if (mounted.current) setProjects(result.items); }, []);
  const onSynced = useCallback(() => { void load().catch(report); void loadProjects().catch(report); }, [load, loadProjects, report]);
  const exportLedger = async () => {
    setExporting(true); setFailure('');
    try { track(await rpc<Job>('expenses.export', { project_id: filters.project_id || 'all', start_date: period.start_date, end_date: period.end_date, format: exportFormat }), '正在导出所选项目与日期区间。'); }
    catch (reason) { report(reason); } finally { if (mounted.current) setExporting(false); }
  };
  useEffect(() => { if (connected) void loadProjects().catch(report); }, [connected, loadProjects, report]);
  const adopt = useCallback((value: ExpenseEntry) => {
    value = normalizeExpenseEntry(value); const next = expenseDraft(value); setItem(value); setDraft(next); setBase(next); setPendingAttachments([]); setRemovedAttachments([]); setCommitJob(undefined); saveRequest.current = undefined;
    current.current = { ...current.current, item: value, draft: next, dirty: false, pendingAttachments: [], removedAttachments: [] };
  }, []);
  const dismissEditor = useCallback(() => {
    view.current++; setEditing(false); setItem(undefined); setFailure(''); setPendingAttachments([]); setRemovedAttachments([]); saveRequest.current = undefined; current.current = { ...current.current, dirty: false, editing: false, item: undefined };
  }, []);
  const closeEditor = useCallback(() => {
    if (current.current.busy) return;
    if (current.current.dirty) setTransition({ proceed: dismissEditor }); else dismissEditor();
  }, [dismissEditor]);
  const closeTransition = useCallback(() => { if (current.current.busy) return; transition?.cancel?.(); setTransition(undefined); }, [transition]);
  const closeDelete = useCallback(() => { if (!current.current.busy) setDeleting(false); }, []);
  const open = async (value?: ExpenseEntry) => {
    view.current++; setPendingAttachments([]); setRemovedAttachments([]); saveRequest.current = undefined; setCommitJob(undefined); setFailure(''); setKind(value?.entry_type === 'income' ? 'payment' : 'invoice'); setItem(value); const next = value ? expenseDraft(value) : { ...newExpenseInPeriod(period), project_id: filters.project_id && filters.project_id !== 'all' ? filters.project_id : projects.find(project => !project.archived)?.id || legacyExpenseProject, category: filters.project_id === legacyExpenseProject ? 'AI订阅' : '其他', status: projects.find(project => project.id === filters.project_id)?.settlement_mode === 'general' ? 'non_reimbursable' as const : 'waiting' as const };
    setDraft(next); setBase(next); setEditing(true); current.current = { ...current.current, item: value, draft: next, dirty: false, editing: true, pendingAttachments: [], removedAttachments: [] };
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
    const payload = { ...snapshot.draft, amount: snapshot.draft.amount.trim(), ...(snapshot.item ? { id: snapshot.item.id, revision: snapshot.item.revision } : {}), attachments_add: snapshot.pendingAttachments, attachments_remove: snapshot.removedAttachments };
    const signature = JSON.stringify(payload);
    if (saveRequest.current?.payload !== signature) saveRequest.current = { payload: signature, id: crypto.randomUUID() };
    let job = pendingCommit && commitJob ? commitJob : await rpc<CommitJob>('expenses.commit', { ...payload, request_id: saveRequest.current.id });
    if (!mounted.current || token !== view.current) return;
    setCommitJob(job); track(job, '正在保存条目与附件。');
    while (activeJob(job.status)) {
      await new Promise(resolve => setTimeout(resolve, 600));
      if (!mounted.current || token !== view.current) return;
      job = await rpc<CommitJob>('jobs.get', { id: job.id }); setCommitJob(job);
    }
    if (!completed(job.status)) throw new Error(errorText(job.error || job.message || '保存未完成，请重试。'));
    const value = job.result?.entry;
    if (!value) throw new Error('未能读取保存结果，请重试检查。');
    if (mounted.current && token === view.current) { adopt(value); await load(); }
    return value;
  }, [adopt, load, report, commitJob, pendingCommit, track]);
  const save = async () => {
    if (busy) return;
    current.current.busy = 'save';
    const token = view.current;
    setBusy('save'); setFailure('');
    try { const value = await saveDraft(); if (value && mounted.current && token === view.current) dismissEditor(); }
    catch (reason) { report(reason); }
    finally { if (mounted.current) setBusy(''); }
  };
  const saveAndContinue = async () => {
    if (busy) return;
    current.current.busy = 'save';
    setBusy('save'); setFailure('');
    try { const value = await saveDraft(); if (value) { const next = transition; setTransition(undefined); next?.proceed(); } }
    catch (reason) { report(reason); }
    finally { if (mounted.current) setBusy(''); }
  };
  const discard = () => { const next = transition; setTransition(undefined); current.current.dirty = false; next?.proceed(); };
  const attach = async () => {
    const token = view.current; setBusy('attach'); setFailure('');
    try {
      const paths = await native.files(true, [{ name: '发票与凭证', extensions: ['pdf', 'png', 'jpg', 'jpeg', 'webp', 'ofd'] }]);
      if (mounted.current && token === view.current) setPendingAttachments(value => pendingExpenseAttachments(value, paths, kind));
    } catch (reason) { report(reason); }
    finally { if (mounted.current) setBusy(''); }
  };
  const remove = async () => {
    if (!item || !deleting) return;
    setBusy('delete'); setFailure('');
    try { await rpc('expenses.delete', { id: item.id, revision: item.revision }); setDeleting(false); dismissEditor(); await load(); }
    catch (reason) { report(reason); }
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
  const changePeriod = (patch: Partial<ExpensePeriod>) => { const next = { ...period, ...patch }; current.current.period = next; setPeriod(next); listToken.current++; };
  const changeFilter = (patch: Partial<ExpenseFilters>) => { const next = { ...filters, ...patch }; if (patch.entry_type === 'income') next.status = ''; current.current.filters = next; setFilters(next); listToken.current++; };
  const change = (patch: Partial<ExpenseDraft>) => setDraft(value => ({ ...value, ...patch }));
  const setType = (entry_type: ExpenseType) => { setDraft(value => changeExpenseType(value, entry_type, !item)); setKind(entry_type === 'income' ? 'payment' : 'invoice'); };
  const periodValid = expensePeriodValid(period);
  const queryKey = JSON.stringify(expenseQueryParams(period, filters));
  const visibleData = periodValid && data?.queryKey === queryKey ? data : undefined;
  const visibleItems = visibleData?.queryKey === queryKey ? visibleData.items : [];
  const filtering = Object.entries(filters).some(([key, value]) => key !== 'project_id' && !!value);
  const splitSettlement = visibleData?.settlement_mode === 'half';
  const selectedProject = projects.find(project => project.id === filters.project_id);
  const categories = [...new Set(['AI订阅', '其他报销', '其他', '转账收入', ...(visibleData?.categories || [])])];
  return <div className="expenses-page">
    <LedgerSync onSynced={onSynced} />
    <div className="expenses-project-toolbar"><Field label="项目"><select aria-label="筛选项目" value={filters.project_id || 'all'} onChange={event => changeFilter({ project_id: event.target.value })}><option value="all">全部项目</option>{projects.map(project => <option key={project.id} value={project.id}>{project.name}{project.archived ? '（已归档）' : ''}</option>)}</select></Field><Button disabled={!connected} onClick={() => setManageProjects(true)}>管理项目</Button><span className="flex-spacer" /><select aria-label="导出格式" value={exportFormat} onChange={event => setExportFormat(event.target.value)}><option value="xlsx">Excel (.xlsx)</option><option value="csv">CSV</option></select><Button busy={exporting} disabled={!connected || !periodValid || exportRunning} onClick={exportLedger}>导出所选区间</Button></div>
    {latestExport && <div className="expenses-export" aria-live="polite">
      {exportRunning ? <><span>{latestExport.message || '正在导出表格…'}</span><Progress value={latestExport.progress} /></> : completed(latestExport.status) ? <><span>表格已导出{latestExport.result?.rows !== undefined ? ` · ${latestExport.result.rows} 条` : ''}</span>{exportPath && <Button variant="ghost" disabled={!connected} onClick={() => void native.open(exportPath).catch(report)}>打开表格</Button>}</> : <Notice tone="warning">{latestExport.status === 'failed' ? `导出失败：${errorText(latestExport.error || latestExport.message || '请重试。')}` : latestExport.status === 'interrupted' ? '导出已中断，可重新导出。' : '导出已取消。'}</Notice>}
    </div>}
    <div className="expenses-toolbar"><div className="expenses-period"><Field label="起始日期"><input type="date" aria-label="起始日期" value={period.start_date || ''} onChange={event => changePeriod({ start_date: event.target.value })} /></Field><span>至</span><Field label="结束日期"><input type="date" aria-label="结束日期" value={period.end_date || ''} onChange={event => changePeriod({ end_date: event.target.value })} /></Field><Button variant="ghost" onClick={() => changePeriod(monthPeriod())}>本月</Button><Button variant="ghost" title="包含本月的最近 3 个自然月，截止今天" onClick={() => changePeriod(recentExpensePeriod(3))}>近3月</Button><Button variant="ghost" title="包含本月的最近 6 个自然月，截止今天" onClick={() => changePeriod(recentExpensePeriod(6))}>近半年</Button></div><div><IconButton label="刷新记账" disabled={!connected || loading || !periodValid} onClick={() => void load().catch(report)}><RefreshCw size={16} /></IconButton><Button variant="primary" disabled={!connected || !periodValid || selectedProject?.archived} onClick={() => void open()}><Plus size={16} />新增条目</Button></div></div>
    {!periodValid && <Notice tone="warning">请选择有效的起止日期，结束日期不能早于起始日期。</Notice>}
    {failure && !editing && !transition && !deleting && <Notice tone="warning">{failure}</Notice>}
    {splitSettlement && <p className="small-note expenses-summary-hint">应均摊金额 =（消费总额 − 已报销总额）÷ 2，按分四舍五入；等待报销暂不扣除。区间汇总不受列表筛选影响，按所选项目与条目日期汇总。</p>}
    {!!visibleData?.summary.length && <div className="expenses-totals">{visibleData.summary.map(value => {
      const settlement = expenseSettlement(value.settlement_remaining);
      return <Section key={value.currency} className="expenses-currency"><div className="expenses-currency-heading"><span className="expenses-currency-label">{value.currency}</span><small>等待报销 {expenseMoney(value.pending)}</small></div><dl><div><dt>消费总额</dt><dd>{expenseMoney(value.expense_total)}</dd></div><div><dt>已报销总额</dt><dd>{expenseMoney(value.reimbursed)}</dd></div>{splitSettlement && <div title="（消费总额 − 已报销总额）÷ 2，按分四舍五入；等待报销暂不扣除"><dt>应均摊金额</dt><dd>{expenseMoney(value.share_due)}</dd></div>}<div><dt>收入</dt><dd>{expenseMoney(value.transfer_income)}</dd></div><div className={`expenses-settlement ${settlement.kind}`} title={splitSettlement ? "应均摊金额 − 转账收入" : "收入 + 已报销 − 支出"}><dt>{splitSettlement ? settlement.label : "收支结余"}</dt><dd>{splitSettlement ? settlement.amount : expenseMoney(value.net)}</dd></div></dl></Section>;
    })}</div>}
    <div className="expenses-filters"><div className="search-input"><Search size={15} /><input aria-label="搜索条目" placeholder="搜索名称、分类或备注" value={filters.query} onChange={event => changeFilter({ query: event.target.value })} /></div><select aria-label="筛选类型" value={filters.entry_type} onChange={event => changeFilter({ entry_type: event.target.value })}><option value="">全部类型</option><option value="expense">支出</option><option value="income">转账收入</option></select><Button variant="ghost" aria-expanded={moreFilters} onClick={() => setMoreFilters(!moreFilters)}><Filter size={14} />更多筛选{[filters.status, filters.category, filters.currency].filter(Boolean).length > 0 && ` · ${[filters.status, filters.category, filters.currency].filter(Boolean).length}`}</Button>{filtering && <Button variant="ghost" onClick={() => { const next = { ...emptyExpenseFilters(), project_id: filters.project_id }; current.current.filters = next; setFilters(next); listToken.current++; }}>清除筛选</Button>}</div>
    {moreFilters && <div className="expenses-more-filters"><Field label="报销状态"><select aria-label="筛选报销状态" disabled={filters.entry_type === 'income'} value={filters.status} onChange={event => changeFilter({ status: event.target.value })}><option value="">全部状态</option>{Object.entries(expenseStatus).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field><Field label="分类"><select aria-label="筛选分类" value={filters.category} onChange={event => changeFilter({ category: event.target.value })}><option value="">全部分类</option>{categories.map(value => <option key={value}>{value}</option>)}</select></Field><Field label="币种"><select aria-label="筛选币种" value={filters.currency} onChange={event => changeFilter({ currency: event.target.value })}><option value="">全部币种</option>{['CNY', 'USD', 'EUR', 'HKD'].map(value => <option key={value}>{value}</option>)}</select></Field></div>}
    <Section className="expenses-list">{visibleItems.length ? <><div className="expenses-list-heading"><span>日期</span><span>名称 / 类型 / 分类</span><span>金额</span><span>报销状态</span><span>附件</span></div>{visibleItems.map(value => <button className="expenses-row" key={value.id} disabled={loading} onClick={() => void open(value)}><time>{value.date}</time><span className="expenses-row-title"><strong>{value.title}</strong><small>{projects.find(project => project.id === value.project_id)?.name || '项目'} · {value.entry_type === 'income' ? '收入' : '支出'}{value.category && value.category !== '转账收入' ? ` · ${value.category}` : ''}</small>{value.validation_warning && <small className="expense-warning">{value.validation_warning}</small>}</span><span className={`expenses-amount ${value.entry_type}`}>{value.entry_type === 'income' ? '+' : '−'}{expenseMoney(value.amount)} <small>{value.currency}</small></span><span className={`expenses-status ${value.status}`}>{value.entry_type === 'income' ? '—' : expenseStatus[value.status as keyof typeof expenseStatus]}</span><span className="expenses-attachment-count"><Paperclip size={13} />{value.attachments.length}</span></button>)}</> : <Empty title={!periodValid ? '请选择有效日期区间' : loading ? '正在加载…' : failure ? '读取失败，请重试' : filtering ? '没有符合筛选条件的条目' : '区间内暂无条目'} />}</Section>    {editing && !transition && !deleting && <Modal title={item ? '编辑条目' : '新增条目'} onClose={closeEditor} wide><div className="modal-body expenses-editor">{item?.validation_warning && <Notice tone="warning">{item.validation_warning}</Notice>}
      <fieldset className="plain-fieldset" disabled={locked || !connected}>
        <div className="expense-editor-grid"><Field label="项目"><select aria-label="条目项目" value={draft.project_id} onChange={event => change({ project_id: event.target.value })}>{projects.filter(project => !project.archived || project.id === draft.project_id).map(project => <option key={project.id} value={project.id}>{project.name}{project.archived ? '（已归档）' : ''}</option>)}</select></Field><Field label="条目类型"><select aria-label="条目类型" value={draft.entry_type} onChange={event => setType(event.target.value as ExpenseType)}><option value="expense">支出</option><option value="income">转账收入</option></select></Field>
        <Field label="名称"><input value={draft.title} maxLength={200} placeholder="如 ChatGPT 订阅" onChange={event => change({ title: event.target.value })} /></Field><Field label="分类"><input list="expense-categories" value={draft.category} maxLength={80} onChange={event => change({ category: event.target.value })} /><datalist id="expense-categories"><option value="AI订阅" /><option value="其他报销" /><option value="其他" /><option value="转账收入" /></datalist></Field></div>
        <div className="expense-editor-details"><Field label="日期"><input type="date" value={draft.date} onChange={event => change({ date: event.target.value })} /></Field><Field label="金额"><input inputMode="decimal" value={draft.amount} placeholder="0.00" onChange={event => change({ amount: event.target.value })} /></Field><Field label="币种"><select value={draft.currency} onChange={event => change({ currency: event.target.value })}>{['CNY', 'USD', 'EUR', 'HKD'].map(value => <option key={value}>{value}</option>)}</select></Field>{draft.entry_type === 'expense' && <Field label="报销状态"><select aria-label="报销状态" value={draft.status} onChange={event => change({ status: event.target.value as ExpenseDraft['status'] })}>{Object.entries(expenseStatus).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>}</div>
        <Field label="备注"><textarea rows={2} maxLength={5000} value={draft.notes} onChange={event => change({ notes: event.target.value })} /></Field>
      </fieldset>
      <div className="expenses-attachments"><div className="expenses-attachments-heading"><h3>附件</h3><select aria-label="附件类型" disabled={locked} value={kind} onChange={event => setKind(event.target.value as AttachmentKind)}>{Object.entries(attachmentKinds).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><Button disabled={!connected || locked} busy={busy === 'attach'} onClick={attach}><FilePlus2 size={14} />添加附件</Button></div><p className="small-note">附件会在保存条目时一并提交。支持 PDF、图片和 OFD；单个最多 50 MiB，每次最多 200 MiB。</p>
        {item?.attachments.map(attachment => { const removed = removedAttachments.includes(attachment.id); return <div className={`expenses-attachment${removed ? ' pending-remove' : ''}`} key={attachment.id}><button onClick={() => void openAttachment(attachment)} title="打开附件副本"><Paperclip size={14} /><span>{attachment.original_name}</span></button><small>{removed ? '待移除' : attachmentKinds[attachment.kind]}</small>{removed ? <Button variant="ghost" disabled={locked} onClick={() => setRemovedAttachments(value => value.filter(id => id !== attachment.id))}>撤销移除</Button> : <IconButton label={`移除附件 ${attachment.original_name}`} disabled={locked} onClick={() => setRemovedAttachments(value => [...value, attachment.id])}><Trash2 size={14} /></IconButton>}</div>; })}
        {pendingAttachments.map(attachment => <div className="expenses-attachment" key={attachment.path}><button onClick={() => void native.open(attachment.path).catch(report)} title={attachment.path}><Paperclip size={14} /><span>{attachmentFileName(attachment.path)}</span></button><small>{attachmentKinds[attachment.kind]} · 待保存</small><IconButton label={`移除待保存附件 ${attachmentFileName(attachment.path)}`} disabled={locked} onClick={() => setPendingAttachments(value => value.filter(entry => entry.path !== attachment.path))}><Trash2 size={14} /></IconButton></div>)}
      </div>
      {commitJob && pendingCommit && <div className="expenses-progress"><span>{busy === 'save' ? commitJob.message || '正在保存条目与附件…' : '保存结果尚未确认，请检查结果后继续。'}</span><Progress value={commitJob.progress} /></div>}
      {failure && <Notice tone="warning">{failure}</Notice>}
      <div className="modal-footer expenses-editor-footer">{item && <Button variant="ghost" disabled={locked} onClick={() => { const reload = () => void open(item); if (dirty) setTransition({ proceed: reload }); else reload(); }}>重新载入</Button>}{item && <Button variant="ghost" disabled={locked} onClick={() => setDeleting(true)}><Trash2 size={14} />删除条目</Button>}<span className="flex-spacer" />{dirty && <small>未保存</small>}<Button disabled={locked} onClick={closeEditor}>取消</Button><Button variant="primary" disabled={!connected || !!busy || (!dirty && !!item)} busy={busy === 'save'} onClick={save}><Save size={14} />{pendingCommit && !busy ? '检查保存结果' : '保存'}</Button></div>
    </div></Modal>}
    {manageProjects && <ExpenseProjects projects={projects} onChanged={async () => { await loadProjects(); await load(); }} onClose={() => setManageProjects(false)} />}
    {transition && <Modal title="保存修改？" onClose={closeTransition}><div className="modal-body"><p>当前条目有未保存的修改。</p>{failure && <Notice tone="warning">{failure}</Notice>}<div className="modal-footer"><Button disabled={!!busy} onClick={closeTransition}>继续编辑</Button><Button disabled={locked} onClick={discard}>不保存</Button><Button variant="primary" disabled={!!busy} busy={busy === 'save'} onClick={saveAndContinue}>保存并继续</Button></div></div></Modal>}
    {deleting && <Modal title="删除条目？" onClose={closeDelete}><div className="modal-body"><p className="break-word">{`删除“${item?.title}”及其附件。`}</p>{failure && <Notice tone="warning">{failure}</Notice>}<div className="modal-footer"><Button disabled={!!busy} onClick={closeDelete}>取消</Button><Button variant="danger" busy={busy === 'delete'} onClick={remove}>确认删除</Button></div></div></Modal>}
  </div>;
}
