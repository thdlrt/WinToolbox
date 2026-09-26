export interface LedgerMigrationStatus { migration_pending?: number; migration_error?: string | null }
export function ledgerMigrationMessage(status?: LedgerMigrationStatus) {
  if (status?.migration_error) return '旧目录数据尚未合并完成';
  if ((status?.migration_pending || 0) > 0) return `还有 ${status!.migration_pending} 个旧目录等待合并`;
  return '';
}
