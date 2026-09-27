export type MemoryMode = 'default' | 'full';
export interface MemoryPreferences {
  mode: MemoryMode; auto_enabled: boolean; auto_mode: MemoryMode;
  threshold_enabled: boolean; threshold_percent: number;
  interval_enabled: boolean; interval_minutes: number; cooldown_minutes: number;
}
export interface MemoryAutomation { running?: boolean; paused_reason?: string | null; last_run?: number | null; last_trigger?: string | null; last_error?: string | null; next_interval_at?: number | null }
export interface MemorySettings extends MemoryPreferences { automation?: MemoryAutomation }
export interface MemoryDraft extends Omit<MemoryPreferences, 'threshold_percent' | 'interval_minutes' | 'cooldown_minutes'> { threshold_percent: string; interval_minutes: string; cooldown_minutes: string }
export const defaultMemoryPreferences: MemoryPreferences = { mode: 'default', auto_enabled: false, auto_mode: 'default', threshold_enabled: true, threshold_percent: 80, interval_enabled: false, interval_minutes: 30, cooldown_minutes: 5 };
export function memoryDraft(value: Partial<MemoryPreferences> = {}): MemoryDraft {
  const p = { ...defaultMemoryPreferences, ...value };
  return { mode: p.mode, auto_enabled: p.auto_enabled, auto_mode: p.auto_mode, threshold_enabled: p.threshold_enabled, threshold_percent: String(p.threshold_percent), interval_enabled: p.interval_enabled, interval_minutes: String(p.interval_minutes), cooldown_minutes: String(p.cooldown_minutes) };
}
export function memorySettingsValidation(value: MemoryDraft): string {
  if (value.auto_enabled && !value.threshold_enabled && !value.interval_enabled) return '自动清理至少需要开启一种触发方式。';
  for (const [key, min, max, label] of [['threshold_percent', 50, 99, '内存使用率阈值'], ['interval_minutes', 1, 1440, '定时间隔'], ['cooldown_minutes', 1, 1440, '清理最短间隔']] as const) {
    if (!/^\d+$/.test(value[key]) || Number(value[key]) < min || Number(value[key]) > max) return `${label}需为 ${min}–${max} 的整数。`;
  }
  return '';
}
export function memorySettingsParams(value: MemoryDraft): MemoryPreferences {
  return { mode: value.mode, auto_enabled: value.auto_enabled, auto_mode: value.auto_mode, threshold_enabled: value.threshold_enabled, threshold_percent: Number(value.threshold_percent), interval_enabled: value.interval_enabled, interval_minutes: Number(value.interval_minutes), cooldown_minutes: Number(value.cooldown_minutes) };
}
export function memorySettingsDirty(value: MemoryDraft, saved?: MemoryPreferences) { return !saved || JSON.stringify(memorySettingsParams(value)) !== JSON.stringify(memorySettingsParams(memoryDraft(saved))); }
