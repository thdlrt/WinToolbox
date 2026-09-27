"""Resolve each task's assigned speech roles at the processing boundary."""


def processing_params(app, params, *, live=False):
    result = dict(params)
    settings = app.settings.get()
    preferences = settings.get("preferences", {})
    roles = settings.get('roles', {})
    role = roles.get('live_asr' if live else 'transcribe', {})
    if role.get('provider_id') == 'local':
        if not role.get('model'):
            raise ValueError('请在功能模型设置中选择本地识别模型')
        spec = app.models.identify(role['model'])
        if spec['engine'] not in ('faster-whisper', 'qwen-asr', 'sensevoice'):
            raise ValueError('所选本地模型不能用于语音识别')
        result.update(
            model_mode="local",
            engine=spec['engine'],
            model=spec['id'],
            device=preferences.get("asr_device", "cpu"),
            compute_type=preferences.get("asr_compute_type", "int8"),
        )
        if live and result["engine"] != "faster-whisper":
            raise ValueError("实时本地识别只支持 Whisper，请在功能模型中选择对应模型。")
    else:
        result.update(model_mode="api", engine="api")
        # Speech role assignments live in settings, not in old page/form state.
        for key in ("model", "device", "compute_type"):
            result.pop(key, None)
    if roles.get('tts', {}).get('provider_id') == 'local':
        result['tts_provider'] = 'cosyvoice'
    else:
        # Explicit Edge voices remain available independently of model services.
        result['tts_provider'] = 'edge' if params.get('tts_provider') == 'edge' else 'qwen'
    return result
