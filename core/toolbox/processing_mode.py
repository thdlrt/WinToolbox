"""Resolve the selected model mode at the actual processing boundary."""


def processing_params(app, params, *, live=False):
    result = dict(params)
    preferences = app.settings.get().get("preferences", {})
    mode = preferences.get("model_mode")
    if mode == "local":
        result.update(
            model_mode="local",
            engine=preferences.get("asr_engine", "faster-whisper"),
            model=preferences.get("asr_model", "faster-whisper-small"),
            device=preferences.get("asr_device", "cpu"),
            compute_type=preferences.get("asr_compute_type", "int8"),
            tts_provider="cosyvoice",
        )
        if live and result["engine"] != "faster-whisper":
            raise ValueError("当前本地预设不支持实时识别，请在设置中选择本地预设。")
    elif mode == "bailian":
        result.update(model_mode="bailian", engine="api")
        # Speech role assignments live in settings, not in old page/form state.
        for key in ("model", "device", "compute_type"):
            result.pop(key, None)
        result.setdefault("tts_provider", "qwen")
    return result
