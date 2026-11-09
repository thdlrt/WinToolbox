"""Portable feature configuration coverage and protected rollback; fixtures only."""
import copy
import json
from pathlib import Path

import pytest

from toolbox import config_snapshots as snapshots, portable_config, ai_aggregation_sync
from toolbox.features import ai_quota, ai_calls
from toolbox.settings import atomic_json
from test_webdav import pair, server
from test_config_snapshots import Job


def protectors(monkeypatch):
    from toolbox import settings
    for module in (portable_config, ai_aggregation_sync, ai_quota, ai_calls):
        monkeypatch.setattr(module, 'protect', settings.protect)
    return settings.protect


def configured_source(source, monkeypatch, tmp_path):
    protect = protectors(monkeypatch)
    account = source.call('ai_quota.save', dict(provider='kimi_code', name='跨设备账户', credential='fixture-quota-key'))
    source.call('ai_calls.save', dict(account_id=account['id'], api_key='fixture-call-key', model='fixture-model'))
    atomic_json(source.data_dir / 'quick-menu.json', {'enabled': False, 'tools': ['translate']})
    atomic_json(source.data_dir / 'fnconnect-login.json', {'origin': 'https://nas.invalid', 'username': 'fixture', 'secret': protect('fixture-nas-password')})
    atomic_json(source.data_dir / 'stackchan.local.json', {'enabled': False, 'host': '192.0.2.1', 'token_protected': protect('fixture-robot-key')})
    rule = source.call('filesync.save', {'source': str(tmp_path / 'source-files'), 'target': str(tmp_path / 'target-files'), 'auto': True})
    return account, rule


def test_v3_includes_feature_configs_credentials_and_aggregation(pair, monkeypatch, tmp_path):
    source, target, _ = pair
    account, rule = configured_source(source, monkeypatch, tmp_path)
    connection = (target.data_dir / 'webdav.json').read_bytes()
    value = snapshots.snapshot(source)
    assert value['version'] == 3
    envelope = snapshots.encrypt(value, 'fixture-password')
    assert b'fixture-quota-key' not in snapshots.encode(envelope)
    assert b'fixture-nas-password' not in snapshots.encode(envelope)
    decoded = snapshots.decrypt(envelope, 'fixture-password')
    result = snapshots.restore(target, decoded, Job(), snapshots.revision(target))
    assert result['restart_recommended']
    calls = target.ai_proxy.calls
    assert calls.key(calls.route(account['id'])) == 'fixture-call-key'
    assert json.loads((target.data_dir / 'quick-menu.json').read_text())['enabled'] is False
    saved = json.loads((target.data_dir / 'fnconnect-login.json').read_text())
    assert portable_config.protect(saved['secret'], decrypt=True) == 'fixture-nas-password'
    assert 'fixture-nas-password' not in json.dumps(saved)
    restored_rule = next(row for row in target.filesync.rules() if row['id'] == rule['id'])
    assert restored_rule['auto'] is False and restored_rule['baseline'] == {}
    assert (target.data_dir / 'webdav.json').read_bytes() == connection
    assert not any(b'fixture-quota-key' in p.read_bytes() for p in Path(result['recovery_path']).rglob('*') if p.is_file())


def test_feature_change_after_preview_blocks_restore(pair, monkeypatch, tmp_path):
    source, target, _ = pair
    protectors(monkeypatch)
    value = snapshots.snapshot(source)
    revision = snapshots.revision(target)
    target.call('ai_quota.save', dict(provider='deepseek', name='预览后新建', credential='fixture-new-key'))
    with pytest.raises(ValueError, match='变化'):
        snapshots.restore(target, value, Job(), revision)
    assert target.call('ai_quota.list')['accounts'][0]['name'] == '预览后新建'


def test_feature_restore_failure_rolls_back_files_and_database(pair, monkeypatch, tmp_path):
    source, target, _ = pair
    configured_source(source, monkeypatch, tmp_path)
    old = target.call('ai_quota.save', dict(provider='deepseek', name='保留', credential='fixture-old-key'))
    atomic_json(target.data_dir / 'quick-menu.json', {'enabled': True})
    tables = portable_config.table_backup(target)
    file = (target.data_dir / 'quick-menu.json').read_bytes()
    original = portable_config.apply
    def fail_after_apply(app, value):
        original(app, value)
        raise OSError('fixture commit failure')
    monkeypatch.setattr(portable_config, 'apply', fail_after_apply)
    with pytest.raises(OSError):
        snapshots.restore(target, snapshots.snapshot(source), Job(), snapshots.revision(target))
    assert portable_config.table_backup(target) == tables
    assert (target.data_dir / 'quick-menu.json').read_bytes() == file
    assert target.call('ai_quota.list')['accounts'][0]['id'] == old['id']
    assert not (target.data_dir / 'fnconnect-login.json').exists()


def test_v2_restore_preserves_features_and_rejects_unregistered_paths(pair, monkeypatch):
    source, target, _ = pair
    protectors(monkeypatch)
    account = target.call('ai_quota.save', dict(provider='deepseek', name='保留', credential='fixture-key'))
    old = snapshots.snapshot(source)
    old['version'] = 2
    old.pop('features')
    snapshots.restore(target, old, Job(), snapshots.revision(target))
    assert target.call('ai_quota.list')['accounts'][0]['id'] == account['id']
    bad = snapshots.snapshot(source)
    bad['features']['files']['../escape.json'] = {}
    with pytest.raises(ValueError, match='未登记'):
        snapshots.validate(bad)
