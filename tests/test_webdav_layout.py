"""Unified roots, configuration isolation and non-destructive inbox migration."""
import hashlib
import json
import time
import zipfile
from pathlib import Path

import pytest

from toolbox.config_backup import export_config, import_config
from toolbox.features import relay, webdav
from toolbox.settings import atomic_json
from toolbox.webdav_layout import service_paths, migration_config
from test_features_relay import fixture, Job
from test_webdav import pair, server, finish


def stop_auto(*apps):
    for app in apps:
        app.ledger_close()
    deadline = time.monotonic() + 5
    while any(app.jobs.active for app in apps) and time.monotonic() < deadline:
        time.sleep(.01)
    assert not any(app.jobs.active for app in apps)


def test_shared_layout_fixed_paths_and_private_override_ignored(fixture):
    app, service, _ = fixture
    paths = service_paths({'remote_path': '团队/工具箱'})
    assert paths == {'config_backups': '团队/工具箱/config-backups/windows', 'ledger': '团队/工具箱/ledger-v1',
                     'relay': '团队/工具箱/file-relay', 'project_memory': '团队/工具箱/project-memory'}
    current = service.config()
    service.save({'url': 'https://ignored.invalid/', 'remote_path': 'ignored', 'password': 'ignored', 'use_shared': False})
    assert service.config()['url'] == current['url'] and service.config()['remote_path'] == current['remote_path']
    old = {**current, 'password_dpapi': 'expired', 'remote_path': 'old-root'}
    assert migration_config(old, current)['password_dpapi'] == current['password_dpapi']
    assert migration_config({**old, 'username': 'other-account'}, current)['password_dpapi'] == 'expired'


def test_root_change_pulls_remote_only_ledger_ops_without_writing_old_root(pair):
    source, target, state = pair
    stop_auto(source, target)
    entry = source.call('expenses.save', {'title': 'only on old remote', 'date': '2026-09-26', 'amount': '3'})
    finish(source, source.call('expenses.sync'))
    assert target.ledger.get('entry', entry['id']) is None
    before = len(state['methods'])
    target.call('webdav.save', {'remote_path': 'NewLedgerRoot'})
    finish(target, target.call('expenses.sync'))
    assert target.call('expenses.get', {'id': entry['id']})['title'] == 'only on old remote'
    assert target.call('expenses.sync.status')['migration_pending'] == 0
    old_prefix = '/dav/重要文件/sync/WinToolbox/ledger-v1/'
    assert not any(method in ('PUT', 'MKCOL', 'DELETE', 'MOVE') and path.startswith(old_prefix) for method, path in state['methods'][before:])
    assert any(path.startswith('/dav/NewLedgerRoot/ledger-v1/ops/') for path in state['files'])


def test_old_root_migration_error_does_not_block_new_root_sync_and_retries(pair):
    source, target, state = pair
    stop_auto(source, target)
    old_entry = source.call('expenses.save', {'title': 'old remote', 'date': '2026-09-26', 'amount': '2'})
    finish(source, source.call('expenses.sync'))
    remote_op_path = next(path for path in state['files'] if '/ledger-v1/ops/' in path)
    original = state['files'][remote_op_path]
    state['files'][remote_op_path] = b'{"schema":99}'
    new_entry = target.call('expenses.save', {'title': 'local new root', 'date': '2026-09-26', 'amount': '4'})
    target.call('webdav.save', {'remote_path': 'NewRoot'})
    result = finish(target, target.call('expenses.sync'))['result']
    assert result['migration_pending'] == 1
    assert target.call('expenses.sync.status')['migration_error']
    assert any(path.startswith('/dav/NewRoot/ledger-v1/ops/') for path in state['files'])
    assert target.call('expenses.get', {'id': new_entry['id']})['amount'] == '4.00'
    state['files'][remote_op_path] = original
    finish(target, target.call('expenses.sync'))
    assert target.call('expenses.sync.status')['migration_pending'] == 0
    assert target.call('expenses.get', {'id': old_entry['id']})['amount'] == '2.00'


def test_legacy_copy_collision_retry_and_source_retention(fixture):
    app, service, dav = fixture
    shared = json.loads((app.data_dir / 'webdav.json').read_text('utf-8'))
    legacy = {**shared, 'remote_path': 'old-inbox', 'use_shared': False}
    atomic_json(service.path, legacy)
    dav.dirs.add('/dav/old-inbox/')
    dav.add('/dav/old-inbox/receipt.txt', b'old source')
    dav.add('/dav/WinToolbox/file-relay/receipt.txt', b'new destination')
    service.config()
    result = service.run('list', Job(service))
    assert not result['migration']['pending']
    fingerprint = hashlib.sha256((relay.endpoint(shared['url']) + '\nold-inbox').encode()).hexdigest()[:12]
    copied = '/dav/WinToolbox/file-relay/legacy-' + fingerprint + '/receipt.txt'
    assert dav.files[copied] == b'old source'
    assert dav.files['/dav/old-inbox/receipt.txt'] == b'old source'
    assert dav.files['/dav/WinToolbox/file-relay/receipt.txt'] == b'new destination'
    puts = sum(request[0] == 'PUT' for request in dav.requests)
    service.run('list', Job(service))
    assert sum(request[0] == 'PUT' for request in dav.requests) == puts
    assert not any(request[0] == 'DELETE' and request[1].startswith('/dav/old-inbox/') for request in dav.requests)


def test_migration_retry_after_copy_failure_and_nested_destination_no_recursion(fixture):
    app, service, dav = fixture
    shared = json.loads((app.data_dir / 'webdav.json').read_text('utf-8'))
    atomic_json(service.path, {**shared, 'remote_path': 'WinToolbox', 'use_shared': False})
    dav.add('/dav/WinToolbox/old.txt', b'original')
    dav.add('/dav/WinToolbox/file-relay/existing.txt', b'canonical')
    dav.dirs.add('/dav/WinToolbox/ledger-v1/')
    dav.add('/dav/WinToolbox/ledger-v1/internal.json', b'not an inbox file')
    service.config()
    dav.move_fail = True
    result = service.run('list', Job(service))
    assert result['migration']['pending'] and result['migration']['warning']
    dav.move_fail = False
    result = service.run('list', Job(service))
    assert not result['migration']['pending']
    assert dav.files['/dav/WinToolbox/file-relay/old.txt'] == b'original'
    assert not any('/file-relay/file-relay/' in name for name in dav.files)
    assert not any('/file-relay/ledger-v1/' in name for name in dav.files)


def test_legacy_empty_directory_and_directory_file_collision_preserved(fixture):
    app, service, dav = fixture
    shared = json.loads((app.data_dir / 'webdav.json').read_text('utf-8'))
    atomic_json(service.path, {**shared, 'remote_path': 'old-folders', 'use_shared': False})
    dav.dirs.update({'/dav/old-folders/', '/dav/old-folders/empty/', '/dav/old-folders/conflict/', '/dav/old-folders/conflict/nested/'})
    dav.add('/dav/old-folders/conflict/nested/keep.txt', b'old nested')
    dav.add('/dav/WinToolbox/file-relay/conflict', b'canonical file')
    service.config()
    result = service.run('list', Job(service))
    assert not result['migration']['pending']
    fingerprint = hashlib.sha256((relay.endpoint(shared['url']) + '\nold-folders').encode()).hexdigest()[:12]
    assert '/dav/WinToolbox/file-relay/empty/' in dav.dirs
    assert dav.files['/dav/WinToolbox/file-relay/legacy-' + fingerprint + '/conflict/nested/keep.txt'] == b'old nested'
    assert dav.files['/dav/WinToolbox/file-relay/conflict'] == b'canonical file'


def test_root_change_remembers_old_canonical_inbox(fixture):
    app, service, dav = fixture
    webdav.register(app)
    previous = service.config()
    dav.add('/dav/WinToolbox/file-relay/latest.txt', b'new since legacy migration')
    app.call('webdav.save', {'remote_path': 'NewRoot'})
    assert service.config()['remote_path'] == 'NewRoot/file-relay'
    result = service.run('list', Job(service))
    assert not result['migration']['pending']
    assert dav.files['/dav/NewRoot/file-relay/latest.txt'] == b'new since legacy migration'
    assert dav.files['/dav/WinToolbox/file-relay/latest.txt'] == b'new since legacy migration'


def test_revisiting_root_rescans_new_files_after_previous_migration_completed(fixture):
    app, service, dav = fixture
    webdav.register(app)
    dav.add('/dav/WinToolbox/file-relay/first.txt', b'first visit')
    app.call('webdav.save', {'remote_path': 'RootB'})
    assert not service.run('list', Job(service))['migration']['pending']
    assert dav.files['/dav/RootB/file-relay/first.txt'] == b'first visit'
    app.call('webdav.save', {'remote_path': 'WinToolbox'})
    assert not service.run('list', Job(service))['migration']['pending']
    dav.add('/dav/WinToolbox/file-relay/second.txt', b'added after returning to A')
    app.call('webdav.save', {'remote_path': 'RootB'})
    assert not service.run('list', Job(service))['migration']['pending']
    assert dav.files['/dav/RootB/file-relay/second.txt'] == b'added after returning to A'
    assert dav.files['/dav/WinToolbox/file-relay/second.txt'] == b'added after returning to A'


def test_configuration_restore_never_replaces_ledger_or_connection(pair):
    source, target, state = pair
    stop_auto(source, target)
    source.call('settings.update', {'preferences': {'theme': 'dark'}})
    entry = target.call('expenses.save', {'title': 'keep receipt', 'date': '2026-09-26', 'amount': '12'})
    files_before = {p.relative_to(target.data_dir / 'expenses').as_posix(): p.read_bytes() for p in (target.data_dir / 'expenses').rglob('*') if p.is_file()}
    connection = (target.data_dir / 'webdav.json').read_bytes()
    uploaded = finish(source, source.call('webdav.upload'))['result']
    body = state['files']['/dav/重要文件/sync/WinToolbox/config-backups/windows/' + uploaded['name']]
    assert uploaded['scope'] == 'config'
    import io
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert not any(name.startswith(('expenses/', 'project-memory/', 'media/')) for name in archive.namelist())
    finish(target, target.call('webdav.restore', {'name': uploaded['name']}))
    assert target.settings.get()['preferences']['theme'] == 'dark'
    assert (target.data_dir / 'webdav.json').read_bytes() == connection
    assert target.call('expenses.get', {'id': entry['id']})['title'] == 'keep receipt'
    for name, body in files_before.items():
        if name != 'ledger/sync-state.json':
            assert (target.data_dir / 'expenses' / name).read_bytes() == body


def test_legacy_full_snapshot_list_and_config_only_restore(pair, tmp_path):
    source, target, state = pair
    stop_auto(source, target)
    source.call('settings.update', {'preferences': {'fixture': 'legacy setting'}})
    source.call('expenses.save', {'title': 'never import', 'date': '2026-09-26', 'amount': '1'})
    package = tmp_path / 'full.wtbak'
    finish(source, source.call('backups.export', {'path': str(package), 'include_secrets': False}))
    from test_webdav import filename
    name = filename()
    state['files']['/dav/重要文件/sync/WinToolbox/' + name] = package.read_bytes()
    row = next(row for row in target.call('webdav.list')['snapshots'] if row['name'] == name)
    assert row['scope'] == 'legacy_full' and row['location'] == 'legacy_root'
    # Disable background data sync: this test isolates explicit config restore.
    target.ledger_close()
    before = set(target.ledger.operations)
    finish(target, target.call('webdav.restore', {'name': name, 'location': 'legacy_root'}))
    assert target.settings.get()['preferences']['fixture'] == 'legacy setting'
    assert set(target.ledger.operations) == before


def test_malformed_configuration_rejected_before_any_write(pair, tmp_path):
    _, target, _ = pair
    before = target.settings.get()
    package = tmp_path / 'malformed.wtbak'
    body = json.dumps({'providers': [{'id': 'bad', 'kind': 'unsupported'}]}).encode()
    manifest = {'format': 'WinToolbox configuration', 'version': 1, 'files': {'settings.json': {'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}}}
    with zipfile.ZipFile(package, 'w') as archive:
        archive.writestr('backup-manifest.json', json.dumps(manifest))
        archive.writestr('settings.json', body)
    with pytest.raises(ValueError, match='供应商'):
        import_config(target, package, Job(target.relay))
    assert target.settings.get() == before
