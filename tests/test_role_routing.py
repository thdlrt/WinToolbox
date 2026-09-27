"""Role assignments outrank obsolete global mode and stale page form fields."""
import copy
from types import SimpleNamespace

import httpx
import pytest

from toolbox.app import App
from toolbox.processing_mode import processing_params
from toolbox.providers import ProviderError
from toolbox.live import _role


@pytest.fixture
def app(tmp_path, monkeypatch):
    instance = App(tmp_path / 'data', register_features=False, register_live=False)
    monkeypatch.setattr(instance.settings, 'secret', lambda _: 'fixture-key')
    instance.settings.update({'preferences': {'model_mode': 'local'},
                              'providers': [{'id': 'cloud', 'kind': 'openai', 'name': 'Cloud', 'base_url': 'https://fixture.invalid/v1'}],
                              'roles': {role: {'provider_id': 'cloud', 'model': 'cloud-model'} for role in instance.settings.value['roles']}})
    yield instance
    instance.close()
    instance.jobs.pool.shutdown(wait=True)


def test_old_local_mode_does_not_block_cloud_role_or_explicit_provider(app):
    requests = []
    def cloud(request):
        requests.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'content': 'cloud answer'}}]})
    app.providers.client.close()
    app.providers.client = httpx.Client(transport=httpx.MockTransport(cloud))
    assert app.providers.chat([{'role': 'user', 'content': 'fixture'}]) == 'cloud answer'
    assert len(requests) == 1 and requests[0].url.path == '/v1/chat/completions'
    app.settings.update({'roles': {'chat': {'provider_id': 'local', 'model': 'qwen3.5:0.8b'}}})
    assert app.providers.chat([], provider_id='cloud', model='cloud-model') == 'cloud answer'


def test_old_cloud_mode_does_not_override_local_role_or_fall_back_on_failure(app, monkeypatch):
    app.settings.update({'preferences': {'model_mode': 'bailian'}, 'roles': {'chat': {'provider_id': 'local', 'model': 'qwen3.5:0.8b'}}})
    app.providers.client.close()
    app.providers.client = httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('Cloud fallback is forbidden')))
    monkeypatch.setattr(app.local_llm, 'chat', lambda *args, **kwargs: 'local answer')
    assert app.providers.chat([]) == 'local answer'
    def missing(*args, **kwargs): raise RuntimeError('本地模型尚未安装')
    monkeypatch.setattr(app.local_llm, 'chat', missing)
    with pytest.raises(RuntimeError, match='尚未安装'):
        app.providers.chat([])


def test_asr_tts_and_live_roles_are_independent(app):
    app.settings.update({'roles': {'transcribe': {'provider_id': 'local', 'model': 'qwen-asr-0.6b'},
                                  'live_asr': {'provider_id': 'cloud', 'model': 'cloud-realtime'},
                                  'tts': {'provider_id': 'cloud', 'model': 'cloud-voice'}},
                         'preferences': {'asr_model': 'stale-whisper', 'asr_engine': 'faster-whisper'}})
    file = processing_params(app, {'engine': 'api', 'model': 'old-cloud', 'tts_provider': 'cosyvoice'})
    assert file['engine'] == 'qwen-asr' and file['model'] == 'qwen-asr-0.6b'
    assert file['tts_provider'] == 'qwen'
    live = processing_params(app, {'engine': 'faster-whisper', 'model': 'old-local'}, live=True)
    assert live['engine'] == 'api' and 'model' not in live
    assert _role(app, 'live_asr')[0]['id'] == 'cloud'
    app.settings.update({'roles': {'transcribe': {'provider_id': 'cloud', 'model': 'cloud-file'},
                                  'tts': {'provider_id': 'local', 'model': 'cosyvoice'}}})
    swapped = processing_params(app, {'engine': 'qwen-asr', 'tts_provider': 'qwen'})
    assert swapped['engine'] == 'api' and swapped['tts_provider'] == 'cosyvoice'


def test_unsupported_local_roles_and_models_never_route_cloud(app):
    app.settings.update({'roles': {'parcel': {'provider_id': 'local', 'model': 'qwen3.5:0.8b'},
                                  'parcel_vision': {'provider_id': 'local', 'model': 'qwen3.5:0.8b'},
                                  'live_asr': {'provider_id': 'local', 'model': 'qwen-asr-0.6b'}}})
    for role in ('parcel', 'parcel_vision'):
        with pytest.raises(ProviderError, match='不能'):
            app.providers.resolve(role)
    with pytest.raises(ValueError, match='Whisper'):
        processing_params(app, {}, live=True)
    app.settings.update({'roles': {'transcribe': {'provider_id': 'local', 'model': 'uninstalled-unknown'}}})
    with pytest.raises(ValueError, match='未知本地模型'):
        processing_params(app, {})
