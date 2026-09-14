export type ExpenseType = 'expense' | 'income';
export type ExpenseStatus = 'waiting' | 'reimbursed' | 'non_reimbursable' | 'not_applicable';
export type AttachmentKind = 'invoice' | 'receipt' | 'payment' | 'other';
export interface ExpenseAttachment { id: string; original_name: string; kind: AttachmentKind; size?: number; mime?: string }
export interface ExpenseDraft { title: string; date: string; amount: string; currency: string; entry_type: ExpenseType; status: ExpenseStatus; category: string; notes: string }
export interface ExpenseEntry extends ExpenseDraft { id: string; revision: number; attachments: ExpenseAttachment[]; created_at?: string; updated_at?: string }
export interface ExpenseSummary { currency: string; expense_total: string; pending: string; reimbursed: string; share_due: string; transfer_income: string; settlement_remaining: string; net: string; count: number }
export interface ExpensePeriod { start_month: string; end_month: string }
export interface ExpenseFilters { query: string; entry_type: string; status: string; category: string; currency: string }
export const expenseStatus = { waiting: '等待报销', reimbursed: '已报销', non_reimbursable: '不可报销' };
export const emptyExpenseFilters = (): ExpenseFilters => ({ query: '', entry_type: '', status: '', category: '', currency: '' });
export const attachmentKinds = { invoice: '发票', receipt: '收据', payment: '支付记录', other: '其他' };
export function localDate(value = new Date()) { return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`; }
export function newExpense(month: string, today = localDate()): ExpenseDraft {
  return { title: '', date: today.startsWith(`${month}-`) ? today : `${month}-01`, amount: '', currency: 'CNY', entry_type: 'expense', status: 'waiting', category: 'AI订阅', notes: '' };
}
export function normalizedExpenseStatus(status: string, type: ExpenseType): ExpenseStatus {
  if (type === 'income') return 'not_applicable';
  if (status === 'reimbursed') return 'reimbursed';
  if (status === 'personal' || status === 'non_reimbursable') return 'non_reimbursable';
  return 'waiting';
}
export function expenseDraft(item: Omit<ExpenseDraft, 'entry_type' | 'status'> & { entry_type?: ExpenseType; status: string }): ExpenseDraft {
  const { title, date, amount, currency, category, notes } = item;
  const entry_type = item.entry_type === 'income' ? 'income' : 'expense';
  return { title, date, amount, currency, entry_type, status: normalizedExpenseStatus(item.status, entry_type), category, notes };
}
export function normalizeExpenseEntry(item: ExpenseEntry): ExpenseEntry { return { ...item, ...expenseDraft(item) }; }
export function changeExpenseType(draft: ExpenseDraft, entry_type: ExpenseType, defaultCategory = true): ExpenseDraft {
  return { ...draft, entry_type, status: entry_type === 'income' ? 'not_applicable' : draft.entry_type === 'income' ? 'waiting' : draft.status,
    category: !defaultCategory ? draft.category : entry_type === 'income' && ['', 'AI订阅'].includes(draft.category) ? '转账收入' : entry_type === 'expense' && draft.category === '转账收入' ? 'AI订阅' : draft.category };
}
export function expensePeriodValid(period: ExpensePeriod) { return /^\d{4}-(0[1-9]|1[0-2])$/.test(period.start_month) && /^\d{4}-(0[1-9]|1[0-2])$/.test(period.end_month) && period.start_month <= period.end_month; }
export function expenseQueryParams(period: ExpensePeriod, filters: ExpenseFilters) {
  return { ...period, ...Object.fromEntries(Object.entries(filters).filter(([, value]) => !!value.trim()).map(([key, value]) => [key, value.trim()])) };
}
export function newExpenseInPeriod(period: ExpensePeriod, today = localDate()) {
  const month = today.slice(0, 7);
  return newExpense(month >= period.start_month && month <= period.end_month ? month : period.start_month, today);
}
export function expenseDirty(draft: ExpenseDraft, base: ExpenseDraft) { return (Object.keys(base) as (keyof ExpenseDraft)[]).some(key => draft[key] !== base[key]); }
export function expenseAmountValid(value: string) { return /^(?:0|[1-9]\d{0,8})(?:\.\d{1,2})?$/.test(value.trim()); }
export function expenseValidation(draft: ExpenseDraft): string {
  if (!draft.title.trim()) return '请填写名称。';
  if (!/^\d{4}-\d{2}-\d{2}$/.test(draft.date)) return '请选择日期。';
  if (!expenseAmountValid(draft.amount)) return '金额需为 0 至 999999999.99，最多两位小数。';
  if (draft.entry_type === 'income' && !/[1-9]/.test(draft.amount)) return '转账收入金额需大于 0。';
  return '';
}
// Group a decimal string without converting money to floating-point numbers.
export function expenseMoney(value: string) { const [whole, fraction = ''] = value.split('.'); return `${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}.${fraction.padEnd(2, '0')}`; }
export function expenseSettlement(value: string) {
  const clean = value.trim();
  if (/^[+-]?0+(?:\.0+)?$/.test(clean)) return { kind: 'settled', label: '已结清', amount: '0.00' } as const;
  if (clean.startsWith('-')) return { kind: 'extra', label: '多转金额', amount: expenseMoney(clean.slice(1)) } as const;
  return { kind: 'due', label: '还需转入', amount: expenseMoney(clean.replace(/^\+/, '')) } as const;
}
export function acceptExpenseUpdate(current: ExpenseEntry | undefined, update: ExpenseEntry) { return !!current && current.id === update.id && update.revision >= current.revision; }
