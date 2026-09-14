"""Course progress persists, rejects corruption, and follows portable backups."""
import copy
import json
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from toolbox.app import App
from toolbox.features.phonetics import DEFAULT_STATE


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'source', register_live=False)
    yield instance
    instance.close()
    instance.jobs.pool.shutdown(wait=True)


def example():
    return {'done': [0, 2], 'custom': 'Think about the sound.\nθ / ð — 自定义练习',
            'checks': {'vowels:1': True, 'day-2/read': False}, 'best': 4}


def finish(app, job):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        value = app.jobs.get(job['id'])
        if value['status'] not in ('queued', 'running', 'cancelling') and job['id'] not in app.jobs.active:
            assert value['status'] == 'completed', value
            return value['result']
        time.sleep(.01)
    raise AssertionError('Backup fixture timed out')


def test_default_is_read_only_and_update_replaces_entire_state(app):
    path = app.data_dir / 'practice/phonetics-state.json'
    assert app.call('phonetics.state.get') == DEFAULT_STATE
    assert not path.exists()
    state = example()
    assert app.call('phonetics.state.update', {'state': state}) == state
    assert json.loads(path.read_text('utf-8')) == state
    assert app.call('phonetics.state.get') == state
    replaced = {'done': [4], 'custom': '', 'checks': {}, 'best': 0}
    assert app.call('phonetics.state.update', {'state': replaced}) == replaced
    assert app.call('phonetics.state.get') == replaced
    reopened = App(app.data_dir, register_live=False)
    try:
        assert reopened.call('phonetics.state.get') == replaced
    finally:
        reopened.close()


@pytest.mark.parametrize('field,value', [
    ('done', [0, 0]), ('done', [5]), ('done', [-1]), ('done', [True]), ('done', [1.0]), ('done', {}),
    ('custom', 'a' * 20001), ('custom', None), ('custom', '\ud800'),
    ('checks', {'x': 1}), ('checks', {'': True}), ('checks', {'x' * 81: True}),
    ('checks', {'line\nbreak': False}), ('checks', {str(n): True for n in range(101)}),
    ('best', -1), ('best', 7), ('best', True), ('best', 2.0),
], ids=['duplicate-day', 'large-day', 'negative-day', 'bool-day', 'float-day', 'invalid-days',
        'long-custom', 'null-custom', 'invalid-unicode', 'number-check', 'empty-key', 'long-key',
        'control-key', 'too-many-checks', 'negative-best', 'large-best', 'bool-best', 'float-best'])
def test_invalid_values_do_not_overwrite_existing_progress(app, field, value):
    app.call('phonetics.state.update', {'state': example()})
    path = app.data_dir / 'practice/phonetics-state.json'
    before = path.read_bytes()
    invalid = {**example(), field: value}
    with pytest.raises(ValueError):
        app.call('phonetics.state.update', {'state': invalid})
    assert path.read_bytes() == before


@pytest.mark.parametrize('params', [{}, {'state': {}}, {'state': []}, {'state': {**DEFAULT_STATE, 'extra': 1}},
                                  {'state': DEFAULT_STATE, 'path': '../outside.json'}])
def test_update_requires_exact_complete_state_shape(app, params):
    with pytest.raises(ValueError):
        app.call('phonetics.state.update', copy.deepcopy(params))
    assert not (app.data_dir / 'practice/phonetics-state.json').exists()


@pytest.mark.parametrize('content', [b'{broken JSON', b'{}', b'\xff\xfe\x00', b' ' * (256 * 1024 + 1)],
                         ids=['bad-json', 'invalid-state', 'invalid-encoding', 'oversized'])
def test_corrupt_saved_progress_is_reported_and_never_overwritten(app, content):
    path = app.data_dir / 'practice/phonetics-state.json'
    path.write_bytes(content)
    for method, params in (('phonetics.state.get', {}), ('phonetics.state.update', {'state': example()})):
        with pytest.raises(ValueError, match='损坏或无法读取'):
            app.call(method, params)
        assert path.read_bytes() == content


def test_progress_update_shares_restore_gate_and_rejects_maintenance(app):
    state = example()
    attempted = threading.Event()
    def update():
        attempted.set()
        return app.call('phonetics.state.update', {'state': state})
    with ThreadPoolExecutor(max_workers=1) as pool:
        with app.data_lock:
            future = pool.submit(update)
            assert attempted.wait(2) and not future.done()
        assert future.result(timeout=2) == state
    path = app.data_dir / 'practice/phonetics-state.json'
    before = path.read_bytes()
    app.maintenance = True
    try:
        with pytest.raises(RuntimeError, match='正在同步或恢复'):
            app.call('phonetics.state.update', {'state': copy.deepcopy(DEFAULT_STATE)})
    finally:
        app.maintenance = False
    assert path.read_bytes() == before


def test_progress_is_included_in_existing_backup_and_available_immediately_after_restore(app, tmp_path):
    state = example()
    app.call('phonetics.state.update', {'state': state})
    target = App(tmp_path / 'target', register_live=False)
    try:
        target.call('phonetics.state.update', {'state': {'done': [1], 'custom': 'old', 'checks': {}, 'best': 1}})
        package = tmp_path / 'course-progress.wtbak'
        finish(app, app.call('backups.export', {'path': str(package), 'include_media': False, 'include_models': False}))
        with zipfile.ZipFile(package) as archive:
            assert json.loads(archive.read('practice/phonetics-state.json')) == state
        finish(target, target.call('backups.import', {'path': str(package)}))
        assert target.call('phonetics.state.get') == state
        assert target.call('phonetics.state.update', {'state': copy.deepcopy(DEFAULT_STATE)}) == DEFAULT_STATE
    finally:
        target.close()
        target.jobs.pool.shutdown(wait=True)
