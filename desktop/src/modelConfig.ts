import type { Settings } from './api';

export const presetNames: Record<string, string> = { light: '轻量', balanced: '均衡', quality: '高质量' };

export function modelConfig(settings?: Settings, role: 'transcribe' | 'live_asr' = 'transcribe') {
  const preferences = settings?.preferences || {};
  const selected = settings?.roles[role];
  const local = selected?.provider_id === 'local';
  const mode = selected?.provider_id ? (local ? 'local' : 'api') : '';
  const model = selected?.model || '';
  const provider = settings?.providers.find(value => value.id === selected?.provider_id);
  const engine = model.startsWith('faster-whisper-') ? 'faster-whisper' : model.startsWith('qwen-asr-') ? 'qwen-asr' : model === 'sensevoice-small' ? 'sensevoice' : '';
  return {
    mode,
    local,
    label: local ? `本地 · ${model || '未选择模型'}` : provider ? `${provider.name || provider.id} · ${model || '未选择模型'}` : '尚未配置模型',
    engine: local ? engine : 'api',
    model: local ? model : '',
    ttsLocal: settings?.roles.tts?.provider_id === 'local',
    ttsProvider: settings?.roles.tts?.provider_id === 'local' ? 'cosyvoice' : 'qwen',
    device: local ? String(preferences.asr_device || 'cpu') : 'auto',
    computeType: local ? String(preferences.asr_compute_type || 'int8') : 'default',
  };
}
