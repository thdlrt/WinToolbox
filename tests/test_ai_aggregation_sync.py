"""Two independent stores and actual DAV HTTP; synthetic credentials only."""
import json
import threading
from types import SimpleNamespace

import pytest

from toolbox import ai_aggregation_sync as aggregation
from toolbox.features import ai_quota, ai_calls
from toolbox.settings import Settings, atomic_json
from toolbox.storage import Storage
from test_features_project_memory_sync import dav, Job


@pytest.fixture
def apps(tmp_path, monkeypatch, dav):
    def protected(value, decrypt=False):
        if decrypt:
            assert value.startswith('protected:')
            return value[len('protected:'):]
        return 'protected:' + value
    monkeypatch.setattr(aggregation, 'protect', protected)
    monkeypatch.setattr(ai_quota, 'protect', protected)
    monkeypatch.setattr(ai_calls, 'protect', protected)
    monkeypatch.setattr('toolbox.settings.protect', protected)
    result = []
    for name in ('a', 'b'):
        directory = tmp_path / name
        app = SimpleNamespace(data_dir=directory, storage=Storage(directory), settings=Settings(directory),
                              data_lock=threading.RLock(), project_memory=SimpleNamespace(device_id=name), emit=lambda *a, **k: None)
        quota = ai_quota.AiQuota(app)
        calls = ai_calls.AiCalls(app, quota)
        app.ai_proxy = SimpleNamespace(calls=calls)
        atomic_json(directory / 'webdav.json', dict(dav[0].config, password_dpapi=protected('dav-password')))
        result.append(app)
    return (*result, dav[1])


def test_accounts_routes_keys_merge_delete_and_retry(apps):
    a, b, state = apps
    qa, ca = aggregation.components(a)
    qb, cb = aggregation.components(b)
    row = qa.save(dict(provider='kimi_code', name='测试账户', credential='synthetic-quota-key'))
    ca.save(dict(account_id=row['id'], api_key='synthetic-calling-key', model='test-model'))
    aggregation.sync(a, Job())
    assert aggregation.sync(b, Job())['applied'] == 1
    assert qb.listing()['accounts'][0]['name'] == '测试账户'
    assert cb.key(cb.route(row['id'])) == 'synthetic-calling-key'
    assert cb.route(row['id'])['default_model'] == 'test-model'
    assert qb._get(row['id'])['credential'] == 'protected:synthetic-quota-key'
    assert all(b'synthetic-' not in body and b'protected:' not in body for body in state['files'].values())
    extra = qb.save(dict(provider='deepseek', name='乙已有账户', credential='other-synthetic-key'))
    aggregation.sync(b, Job()); aggregation.sync(a, Job())
    assert len(qa.listing()['accounts']) == 2
    count = len(state['files'])
    aggregation.sync(a, Job()); aggregation.sync(b, Job())
    assert len(state['files']) == count
    qb.remove({'id': row['id']})
    aggregation.sync(b, Job()); aggregation.sync(a, Job())
    assert qa._get(row['id']) is None and qa._get(extra['id']) is not None


def test_model_provider_reference_becomes_portable_credential(apps):
    a, b, _ = apps
    qa, _ = aggregation.components(a)
    qb, cb = aggregation.components(b)
    a.settings.update({'providers': [{'id': 'kimi', 'kind': 'openai', 'name': '测试模型服务',
                                    'base_url': 'https://api.kimi.com/coding/v1', 'api_key': 'provider-fixture-key'}]})
    row = qa.save(dict(provider='kimi_code', source_provider_id='kimi'))
    aggregation.sync(a, Job()); aggregation.sync(b, Job())
    assert qb._get(row['id'])['source_provider_id'] == ''
    assert cb.key(cb.route(row['id'])) == 'provider-fixture-key'


def test_conflicts_keep_both_devices_until_explicit_save(apps):
    a, b, _ = apps
    qa, _ = aggregation.components(a)
    qb, _ = aggregation.components(b)
    row = qa.save(dict(provider='deepseek', name='初始', credential='synthetic-key'))
    aggregation.sync(a, Job()); aggregation.sync(b, Job())
    qa.save(dict(id=row['id'], provider='deepseek', name='甲修改'))
    qb.save(dict(id=row['id'], provider='deepseek', name='乙修改'))
    aggregation.sync(a, Job())
    assert aggregation.sync(b, Job())['warnings']
    assert aggregation.sync(a, Job())['warnings']
    assert qa._get(row['id'])['name'] == '甲修改' and qb._get(row['id'])['name'] == '乙修改'
    assert aggregation.sync(b, Job())['warnings']
    qb.save(dict(id=row['id'], provider='deepseek', name='乙修改'))
    assert aggregation.sync(b, Job())['warnings'] == []
    assert aggregation.sync(a, Job())['warnings'] == []
    assert qa._get(row['id'])['name'] == '乙修改'


def test_lost_response_retries_same_operation_and_wrong_password_preserves_local(apps, monkeypatch):
    a, b, state = apps
    qa, _ = aggregation.components(a)
    qb, _ = aggregation.components(b)
    qa.save(dict(provider='deepseek', name='保留', credential='synthetic-key'))
    original = aggregation.AggregationRemote.sync
    def lost(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise TimeoutError('lost response')
    monkeypatch.setattr(aggregation.AggregationRemote, 'sync', lost)
    with pytest.raises(TimeoutError): aggregation.sync(a, Job())
    count = len(state['files'])
    monkeypatch.setattr(aggregation.AggregationRemote, 'sync', original)
    aggregation.sync(a, Job())
    assert len(state['files']) == count
    config = json.loads((b.data_dir / 'webdav.json').read_text())
    atomic_json(b.data_dir / 'webdav.json', dict(config, password_dpapi='protected:wrong-password'))
    with pytest.raises(ValueError, match='解密'): aggregation.sync(b, Job())
    assert qb.listing()['accounts'] == []
    atomic_json(b.data_dir / 'webdav.json', config)
    aggregation.sync(b, Job())
    assert qb.listing()['accounts'][0]['name'] == '保留'


def test_independent_fields_merge_automatically(apps):
    a, b, _ = apps
    qa, ca = aggregation.components(a)
    qb, cb = aggregation.components(b)
    row = qa.save(dict(provider='kimi_code', name='原名称', credential='synthetic-key'))
    ca.save(dict(account_id=row['id'], model='original-model'))
    aggregation.sync(a, Job()); aggregation.sync(b, Job())
    qa.save(dict(id=row['id'], provider='kimi_code', name='甲改名称'))
    cb.save(dict(account_id=row['id'], model='b-new-model'))
    aggregation.sync(a, Job())
    assert aggregation.sync(b, Job())['warnings'] == []
    aggregation.sync(b, Job()); aggregation.sync(a, Job())
    assert qa._get(row['id'])['name'] == qb._get(row['id'])['name'] == '甲改名称'
    assert ca.route(row['id'])['default_model'] == cb.route(row['id'])['default_model'] == 'b-new-model'


def test_explicit_remote_version_can_restore_delete_conflict(apps):
    a, b, _ = apps
    qa, ca = aggregation.components(a)
    qb, cb = aggregation.components(b)
    row = qa.save(dict(provider='deepseek', name='初始', credential='synthetic-key'))
    aggregation.sync(a, Job()); aggregation.sync(b, Job())
    qa.remove({'id': row['id']})
    qb.save(dict(id=row['id'], provider='deepseek', name='远端保留'))
    aggregation.sync(a, Job()); aggregation.sync(b, Job()); aggregation.sync(a, Job())
    a.data_sync = SimpleNamespace(services={'ai_aggregation': {'syncing': False}}, request=lambda *a, **k: None)
    item = aggregation.conflicts(a)['items'][0]
    assert 'synthetic-key' not in json.dumps(item)
    selected = next(version for version in item['versions'] if not version['deleted'])
    result = aggregation.resolve_conflict(a, dict(id=item['id'], parents=item['parents'], op_id=selected['op_id']))
    assert result['resolved'] and qa._get(row['id'])['name'] == '远端保留'
    assert aggregation.conflicts(a)['items'] == []
    aggregation.sync(a, Job()); aggregation.sync(b, Job())
    assert qb._get(row['id'])['name'] == '远端保留'


def test_remote_override_recovers_foreign_device_credentials_and_keeps_backup(apps):
    from pathlib import Path
    a, b, state = apps
    qa, _ = aggregation.components(a)
    qb, _ = aggregation.components(b)
    row = qa.save(dict(provider='deepseek', name='远端账户', credential='remote-synthetic-key'))
    aggregation.sync(a, Job())
    old = qb.save(dict(provider='deepseek', name='旧设备', credential='unused-key'))
    raw = qb._get(old['id']); raw['credential'] = 'foreign-device-ciphertext'; qb._put(raw)
    with pytest.raises(ValueError, match='使用远端覆盖'): aggregation.sync(b, Job())
    before = dict(state['files'])
    result = aggregation.replace_remote(b, Job())
    assert state['files'] == before
    assert qb._get(old['id']) is None and qb._get(row['id'])['name'] == '远端账户'
    backup = json.loads((Path(result['recovery_path']) / 'accounts.json').read_text('utf-8'))
    assert old['id'] in json.dumps(backup) and 'foreign-device-ciphertext' in json.dumps(backup)
    assert aggregation.sync(b, Job())['warnings'] == []


def test_remote_override_preflight_and_apply_failure_preserve_local(apps, monkeypatch):
    a, b, _ = apps
    qa, _ = aggregation.components(a)
    qb, _ = aggregation.components(b)
    local = qb.save(dict(provider='deepseek', name='保留本机', credential='local-synthetic-key'))
    with pytest.raises(ValueError, match='原设备同步'): aggregation.replace_remote(b, Job())
    assert qb._get(local['id'])['name'] == '保留本机'
    qa.save(dict(provider='deepseek', name='远端账户', credential='remote-synthetic-key'))
    aggregation.sync(a, Job())
    original = qb._get(local['id'])
    monkeypatch.setattr(aggregation, 'apply_account', lambda *a: (_ for _ in ()).throw(OSError('fixture protected write failed')))
    with pytest.raises(OSError): aggregation.replace_remote(b, Job())
    assert qb._get(local['id']) == original
