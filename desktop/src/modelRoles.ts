import type { LocalModel, Provider, Settings } from './api';

export function localRoleModels(role: string, models: LocalModel[]) {
  return models.filter(model => {
    if (role === 'live_asr') return model.engine === 'faster-whisper';
    if (role === 'transcribe') return ['faster-whisper', 'qwen-asr', 'sensevoice'].includes(model.engine);
    if (role === 'tts') return model.engine === 'cosyvoice';
    if (role === 'embedding') return model.id === 'local-embedding';
    return model.engine === 'ollama' && model.id !== 'local-embedding';
  }).map(model => ({ id: model.ollama_model || model.id, name: `${model.name}${model.installed ? '' : '（需安装）'}` }));
}

export interface ModelPreset {
  id: string; name: string; kind: Provider['kind'];
  provider: Omit<Provider, 'id' | 'api_key' | 'has_key'>;
  models: Record<string, string>;
}

export function modelPresetPatch(settings: Settings, preset: ModelPreset, providerId: string, newId: string): Partial<Settings> {
  const existing = settings.providers.find(provider => provider.id === providerId && provider.kind === preset.kind);
  if (providerId && !existing) throw new Error('所选供应商已变化，请重新选择。');
  if (!existing && settings.providers.some(provider => provider.kind === preset.kind)) throw new Error('请选择要使用的供应商。');
  if (!existing && settings.providers.some(provider => provider.id === newId)) throw new Error('供应商 ID 已存在，请重新应用。');
  const id = existing?.id || newId;
  const roles = { ...settings.roles };
  for (const [role, model] of Object.entries(preset.models)) roles[role] = { ...roles[role], provider_id: id, model };
  return { roles, ...(!existing ? { providers: [...settings.providers, { ...preset.provider, id }] } : {}) };
}
