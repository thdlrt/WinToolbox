import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import httpx
import pytest

from toolbox import ai_config
from toolbox.app import App
from toolbox.features import ai_config_sync
from toolbox.settings import atomic_json
from test_features_relay import Dav


VECTOR = Path(__file__).parent / 'fixtures/ai-config-vector-v1.json'


def finish(app, record, expected='completed'):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        job = app.jobs.get(record['id'])
        if job['status'] not in ('queued', 'running', 'cancelling') and record['id'] not in app.jobs.active:
            assert job['status'] == expected, job
            return job.get('result', job)
        time.sleep(.01)
    raise AssertionError('AI sync timed out')


def test_fixed_unicode_password_vector_matches_and_decrypts():
    vector = json.loads(VECTOR.read_text('utf-8'))
    assert ai_config.encode(vector['plaintext']).decode('utf-8') == vector['plaintext_utf8']
    assert ai_config.encrypt(vector['plaintext'], vector['password'], salt=bytes(range(16)), nonce=bytes(range(16, 28))) == vector['envelope']
    assert ai_config.decrypt(vector['envelope'], vector['password']) == vector['plaintext']


def test_wrong_password_tampering_and_oversize_fail_without_secret_in_error():
    vector = json.loads(VECTOR.read_text('utf-8'))
    for envelope, password in ((vector['envelope'], 'wrong'), ({**vector['envelope'], 'ciphertext': vector['envelope']['ciphertext'][:-8] + 'AAAAAAAA'}, vector['password'])):
        with pytest.raises(ValueError, match='解密失败') as error:
            ai_config.decrypt(envelope, password)
        assert vector['plaintext']['secrets']['fixture'] not in str(error.value)
    with pytest.raises(ValueError, match='1 MiB'):
        ai_config.parse(b'x' * (ai_config.MAX_BYTES + 1))
    with pytest.raises(ValueError):
        ai_config.decrypt({**vector['envelope'], 'version': True}, vector['password'])


@pytest.mark.parametrize('edit', [lambda p: p['providers'][0].update(base_url='https://name:key@example.invalid/v1'),
                                  lambda p: p['providers'].append(copy.deepcopy(p['providers'][0])),
                                  lambda p: p['roles'].update(chat={'provider_id': 'missing', 'model': 'x'}),
                                  lambda p: p['providers'][0].update(api_key='never-inline'),
                                  lambda p: p['secrets'].update(missing='unknown')])
def test_invalid_payload_schema_rejected(edit):
    payload = json.loads(VECTOR.read_text('utf-8'))['plaintext']
    edit(payload)
    with pytest.raises(ValueError):
        ai_config.validate(payload)


@pytest.fixture
def pair(tmp_path, monkeypatch):
    state = {}
    class FakeRemote:
        def __init__(self, config): pass
        def close(self): pass
        def upload(self, envelope, job):
            job.check_cancelled()
            state['envelope'] = copy.deepcopy(envelope)
        def download(self, job):
            return copy.deepcopy(state['envelope'])
    monkeypatch.setattr(ai_config_sync, 'AiRemote', FakeRemote)
    source, target = [App(tmp_path / name, register_live=False) for name in ('source', 'target')]
    for app in (source, target):
        app.ledger_close()
        app.call('webdav.save', {'url': 'https://dav.example.invalid/', 'username': 'fixture', 'password': 'fixture-webdav-password'})
    source.call('settings.update', {'providers': [{'id': 'fixture', 'name': 'Fixture AI', 'kind': 'openai', 'base_url': 'https://ai.example.invalid/v1', 'api_key': 'fixture-api-secret'}],
                                  'roles': {name: {'provider_id': 'fixture', 'model': 'fixture-model'} for name in source.settings.value['roles']}})
    yield source, target, state
    for app in (source, target):
        app.close()
        app.jobs.pool.shutdown(wait=True)


def test_export_and_jobs_never_expose_secrets_download_requires_preview_confirmation(pair):
    source, target, state = pair
    target.call('settings.update', {'preferences': {'theme': 'dark', 'fixture': 'keep'}, 'presets': [{'id': 'keep'}]})
    original = copy.deepcopy(target.settings.get())
    exported = source.call('ai.config.export')
    assert exported['format'] == ai_config.ENVELOPE_FORMAT
    assert 'fixture-api-secret' not in json.dumps(exported)
    finish(source, source.call('ai.config.upload'))
    preview = finish(target, target.call('ai.config.download'))
    assert preview['secret_count'] == 1 and preview['providers'][0]['has_key']
    assert target.settings.get() == original
    with pytest.raises(ValueError, match='确认'):
        target.call('ai.config.download', {'confirmation_token': preview['confirmation_token']})
    applied = finish(target, target.call('ai.config.download', {'confirmation_token': preview['confirmation_token'], 'confirm_replace': True}))
    assert applied['applied']
    assert target.settings.secret('fixture') == 'fixture-api-secret'
    assert target.settings.get()['preferences'] == original['preferences']
    assert target.settings.get()['presets'] == original['presets']
    assert target.settings.get()['roles'] == source.settings.get()['roles']
    assert 'fixture-api-secret' not in target.settings.secret_path.read_text('utf-8')
    serialized = json.dumps([source.jobs.list(), target.jobs.list(), source.call('ai.config.status'), target.call('ai.config.status')])
    assert 'fixture-api-secret' not in serialized and 'fixture-webdav-password' not in serialized
    assert 'fixture-api-secret' not in json.dumps(state['envelope'])


def test_stale_local_ai_or_connection_rejects_apply_but_preferences_can_change(pair):
    source, target, _ = pair
    finish(source, source.call('ai.config.upload'))
    preview = finish(target, target.call('ai.config.download'))
    target.call('settings.update', {'roles': {'chat': {'provider_id': 'dashscope', 'model': 'changed'}}})
    with pytest.raises(ValueError, match='本机 AI 配置已改变'):
        target.call('ai.config.download', {'confirmation_token': preview['confirmation_token'], 'confirm_replace': True})
    preview = finish(target, target.call('ai.config.download'))
    target.call('webdav.save', {'remote_path': 'OtherRoot'})
    with pytest.raises(ValueError, match='连接已改变'):
        target.call('ai.config.download', {'confirmation_token': preview['confirmation_token'], 'confirm_replace': True})


def test_wrong_password_and_plaintext_remote_do_not_touch_local_files(pair):
    source, target, state = pair
    finish(source, source.call('ai.config.upload'))
    before = {path: path.read_bytes() if path.exists() else None for path in (target.settings.path, target.settings.secret_path)}
    target.call('webdav.save', {'password': 'different-dav-password'})
    finish(target, target.call('ai.config.download'), 'failed')
    for path, value in before.items():
        assert (path.read_bytes() if path.exists() else None) == value
    state['envelope'] = json.loads(VECTOR.read_text('utf-8'))['plaintext']
    finish(target, target.call('ai.config.download'), 'failed')
    for path, value in before.items():
        assert (path.read_bytes() if path.exists() else None) == value


def test_generic_job_api_rejects_inline_credentials_and_old_default_roles_do_not_reappear(pair):
    _, target, state = pair
    with pytest.raises(ValueError, match='不得写入任务'):
        target.jobs.submit('ai.config.upload', {'api_key': 'do-not-log'})
    payload = json.loads(VECTOR.read_text('utf-8'))['plaintext']
    state['envelope'] = ai_config.encrypt(payload, 'fixture-webdav-password')
    preview = finish(target, target.call('ai.config.download'))
    finish(target, target.call('ai.config.download', {'confirmation_token': preview['confirmation_token'], 'confirm_replace': True}))
    target.settings.reload()
    assert set(target.settings.get()['roles']) == {'chat'}


def test_apply_write_failure_restores_both_files_and_preferences_changes_survive(pair, monkeypatch):
    source, target, _ = pair
    target.call('settings.update', {'preferences': {'theme': 'dark'}})
    finish(source, source.call('ai.config.upload'))
    preview = finish(target, target.call('ai.config.download'))
    target.call('settings.update', {'preferences': {'record_audio': False}})
    before = {path: path.read_bytes() if path.exists() else None for path in (target.settings.path, target.settings.secret_path)}
    original = ai_config_sync.atomic_json
    def fail_settings(path, value):
        if path == target.settings.path:
            raise OSError('fixture disk failure')
        return original(path, value)
    monkeypatch.setattr(ai_config_sync, 'atomic_json', fail_settings)
    finish(target, target.call('ai.config.download', {'confirmation_token': preview['confirmation_token'], 'confirm_replace': True}), 'failed')
    for path, value in before.items():
        assert (path.read_bytes() if path.exists() else None) == value
    monkeypatch.setattr(ai_config_sync, 'atomic_json', original)
    finish(target, target.call('ai.config.download', {'confirmation_token': preview['confirmation_token'], 'confirm_replace': True}))
    assert target.settings.get()['preferences']['theme'] == 'dark'
    assert target.settings.get()['preferences']['record_audio'] is False


def test_local_model_roles_and_android_empty_model_names_are_portable():
    payload = json.loads(VECTOR.read_text('utf-8'))['plaintext']
    payload['roles'].update(parcel={'provider_id': 'fixture', 'model': ''}, embedding={'provider_id': 'local', 'model': 'embeddinggemma:latest'})
    payload['providers'][0].pop('name')
    assert ai_config.decrypt(ai_config.encrypt(payload, 'fixture-password'), 'fixture-password') == payload


def test_webdav_atomic_upload_overwrites_only_after_complete_move(monkeypatch):
    monkeypatch.setattr('toolbox.features.webdav.protect', lambda value, decrypt=False: value)
    dav = Dav()
    config = {'url': 'http://127.0.0.1/dav/', 'username': 'user', 'password_dpapi': 'pass', 'remote_path': 'WinToolbox'}
    remote = ai_config_sync.AiRemote(config)
    target = '/dav/WinToolbox/ai-config/config-v1.json'
    dav.dirs.add('/dav/WinToolbox/ai-config/')
    dav.add(target, b'previous encrypted file')
    move_failure = [True]
    def transport(request):
        if request.method == 'MOVE':
            assert request.headers['Overwrite'] == 'T'
            if move_failure[0]:
                return httpx.Response(500, request=request)
            source = unquote(urlsplit(str(request.url)).path)
            destination = unquote(urlsplit(request.headers['Destination']).path)
            dav.add(destination, dav.files.pop(source))
            return httpx.Response(204, request=request)
        return dav(request)
    remote.client.close()
    remote.client = httpx.Client(transport=httpx.MockTransport(transport), auth=('user', 'pass'))
    job = SimpleNamespace(check_cancelled=lambda: None)
    envelope = json.loads(VECTOR.read_text('utf-8'))['envelope']
    try:
        with pytest.raises(ValueError):
            remote.upload(envelope, job)
        assert dav.files[target] == b'previous encrypted file'
        assert not any('.part' in name for name in dav.files)
        move_failure[0] = False
        remote.upload(envelope, job)
        assert remote.download(job) == envelope
        assert not any('.part' in name for name in dav.files)
    finally:
        remote.close()
