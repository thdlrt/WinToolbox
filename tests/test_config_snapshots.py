"""Cross-platform backup format and restore safety; synthetic data only."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from toolbox.app import App
from toolbox import config_snapshots as snapshots
from test_webdav import pair, server, finish

class Job:
    def check_cancelled(self): pass
    def mark_committed(self): pass

@pytest.mark.parametrize('name', ['config_snapshot_v2.json', 'config_snapshot_android_v2.json'])
def test_cross_platform_fixed_vectors(name):
    vector = json.loads((Path(__file__).parent / 'fixtures' / name).read_text('utf-8'))
    assert snapshots.decrypt(vector['envelope'], vector['password']) == vector['payload']
    with pytest.raises(ValueError): snapshots.decrypt(vector['envelope'], 'wrong-password')
    bad = copy.deepcopy(vector['envelope']);bad['version'] = 1
    with pytest.raises(ValueError): snapshots.decrypt(bad, vector['password'])


def test_preview_stale_and_connection_change_refuse_overwrite(pair):
    source, target, _ = pair
    uploaded = finish(source, source.call('webdav.upload'))['result']
    args = {'name': uploaded['name'], 'location': 'unified'}
    preview = target.call('webdav.preview', args)
    target.call('settings.update', {'preferences': {'changed': 'after preview'}})
    finish(target, target.call('webdav.restore', {**args, 'preview_token': preview['token']}), 'failed')
    assert target.settings.value['preferences']['changed'] == 'after preview'
    preview = target.call('webdav.preview', args)
    target.call('webdav.save', {'remote_path': 'ChangedRoot'})
    with pytest.raises(ValueError, match='预览'):
        target.call('webdav.restore', {**args, 'preview_token': preview['token']})


def test_android_restore_only_changes_ai_and_retains_missing_pc_roles(pair):
    _, target, state = pair
    vector = json.loads((Path(__file__).parent / 'fixtures/config_snapshot_android_v2.json').read_text('utf-8'))
    value = vector['payload']
    target.settings.update({'preferences': {'theme': 'light'}})
    original_roles = copy.deepcopy(target.settings.value['roles'])
    name = 'config-android-20260927-123456-' + 'a' * 32 + '.wtconfig.json'
    directory = '/dav/重要文件/sync/WinToolbox/config-backups/shared/'
    target.call('webdav.list')
    state['files'][directory + name] = snapshots.encode(snapshots.encrypt(value, 'fixture-dav-password'))
    listing = target.call('webdav.list')['snapshots']
    assert any(row['name'] == name and row['source_platform'] == 'android' for row in listing)
    args = {'name': name, 'location': 'unified'}
    preview = target.call('webdav.preview', args)
    assert not preview['same_platform']
    finish(target, target.call('webdav.restore', {**args, 'preview_token': preview['token']}))
    assert target.settings.value['preferences']['theme'] == 'light'
    for key, role in original_roles.items():
        if key not in value['ai']['roles']:
            assert target.settings.value['roles'][key] == role
    assert not (target.data_dir / 'profile').exists()


def test_preserved_role_provider_collision_does_not_change_credential():
    base = {'format': 'wintoolbox-ai-config', 'version': 1,
            'providers': [{'id': 'same', 'kind': 'openai', 'base_url': 'https://old.invalid/v1'}],
            'roles': {'chat': {'provider_id': 'same', 'model': 'pc-model'}}, 'secrets': {'same': 'old-key'}}
    incoming = copy.deepcopy(base)
    incoming['providers'][0]['base_url'] = 'https://new.invalid/v1'
    incoming['secrets']['same'] = 'new-key'
    incoming['roles'] = {'parcel': {'provider_id': 'same', 'model': 'mobile-model'}}
    result = snapshots.merge_ai(base, incoming)
    retained = result['roles']['chat']['provider_id']
    assert retained != 'same' and result['secrets'][retained] == 'old-key'
    assert next(p for p in result['providers'] if p['id'] == retained)['base_url'] == 'https://old.invalid/v1'
    assert result['roles']['parcel']['provider_id'] == 'same'


def test_restore_write_failure_rolls_back_configuration_and_secrets(pair, monkeypatch):
    source, target, _ = pair
    target.settings.update({'preferences': {'theme': 'original'}})
    original = {p.name: p.read_bytes() for p in (target.settings.path, target.settings.secret_path)}
    payload = snapshots.snapshot(source)
    revision = snapshots.revision(target)
    monkeypatch.setattr(target.settings, 'import_secrets', lambda values: (_ for _ in ()).throw(OSError('fixture write failure')))
    with pytest.raises(OSError): snapshots.restore(target, payload, Job(), revision)
    assert all((target.data_dir / name).read_bytes() == body for name, body in original.items())


def test_legacy_ai_configuration_is_available_in_backup_list(pair):
    from toolbox import ai_config
    source, target, state = pair
    root = '/dav/重要文件/sync/WinToolbox/ai-config/'
    state['dirs'].add(root)
    state['files'][root + 'config-v1.json'] = ai_config.encode(ai_config.encrypt(ai_config.local_payload(source.settings), 'fixture-dav-password'))
    assert any(row['location'] == 'legacy_ai' for row in target.call('webdav.list')['snapshots'])
    args = {'name': 'config-v1.json', 'location': 'legacy_ai'}
    preview = target.call('webdav.preview', args)
    finish(target, target.call('webdav.restore', {**args, 'preview_token': preview['token']}))

def test_connection_cannot_change_between_preview_check_and_restore(pair, monkeypatch):
    import threading
    source, target, _ = pair
    source.settings.update({'preferences': {'restore_fixture': 'applied'}})
    uploaded = finish(source, source.call('webdav.upload'))['result']
    args = {'name': uploaded['name'], 'location': 'unified'}
    preview = target.call('webdav.preview', args)
    original = snapshots.restore
    attempted, acquired = threading.Event(), threading.Event()
    workers = []
    def wrapped(app, payload, job, revision):
        def change_connection():
            attempted.set()
            with app.data_lock:
                acquired.set()
                app.call('webdav.save', {'remote_path': 'AfterRestore'})
        worker = threading.Thread(target=change_connection)
        workers.append(worker);worker.start()
        assert attempted.wait(1)
        assert not acquired.wait(.1), 'Connection changed after validation but before applying the preview'
        return original(app, payload, job, revision)
    monkeypatch.setattr(snapshots, 'restore', wrapped)
    finish(target, target.call('webdav.restore', {**args, 'preview_token': preview['token']}))
    for worker in workers: worker.join(2)
    assert acquired.is_set()
    assert target.settings.value['preferences']['restore_fixture'] == 'applied'
    assert target.call('webdav.get')['remote_path'] == 'AfterRestore'
