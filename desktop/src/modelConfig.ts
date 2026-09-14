import type { Settings } from './api';

export const presetNames: Record<string, string> = { light: '轻量', balanced: '均衡', quality: '高质量' };

export function modelConfig(settings?: Settings) {
  const preferences = settings?.preferences || {};
  const mode = typeof preferences.model_mode === 'string' ? preferences.model_mode : '';
  const local = mode === 'local';
  return {
    mode,
    local,
    label: local ? `本地 · ${presetNames[String(preferences.local_preset)] || '自定义'}` : mode === 'bailian' ? '阿里云百炼 API' : '自定义模型',
    engine: local ? String(preferences.asr_engine || 'faster-whisper') : 'api',
    model: local ? String(preferences.asr_model || 'faster-whisper-small') : '',
    device: local ? String(preferences.asr_device || 'cpu') : 'auto',
    computeType: local ? String(preferences.asr_compute_type || 'int8') : 'default',
  };
}
