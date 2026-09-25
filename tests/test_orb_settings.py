import json
from types import SimpleNamespace
import pytest
from toolbox.features.orb_settings import OrbSettings, DEFAULT_ACTIONS


def make_service(tmp_path):
    events = []
    return OrbSettings(SimpleNamespace(data_dir=tmp_path, emit=lambda event, **data: events.append((event, data)))), events


def test_shortcuts_persist_order_and_notify_all_windows(tmp_path):
    service, events = make_service(tmp_path)
    assert service.get()['actions'] == DEFAULT_ACTIONS
    chosen = ['filesync', 'clean', 'ram', 'subtitle-toggle', 'media', 'home']
    assert service.save({'actions': chosen}) == {'actions': chosen}
    chosen.reverse()  # The caller cannot mutate the stored value or event.
    assert make_service(tmp_path)[0].get()['actions'][0] == 'filesync'
    assert events == [('orb.settings.changed', service.get())]


@pytest.mark.parametrize('actions', [[], ['home'] * 2, ['home'] * 7, ['unknown'], [None], 'home', None])
def test_bad_shortcuts_do_not_overwrite_saved_settings(tmp_path, actions):
    service, events = make_service(tmp_path)
    service.save({'actions': ['relay', 'home']})
    before = service.path.read_bytes()
    with pytest.raises(ValueError): service.save({'actions': actions})
    assert service.path.read_bytes() == before
    assert len(events) == 1


@pytest.mark.parametrize('content', ['not json', '[]', '{"actions":["removed-tool"]}'])
def test_damaged_or_unsupported_configuration_keeps_orb_usable(tmp_path, content):
    service, _ = make_service(tmp_path)
    service.path.write_text(content, encoding='utf-8')
    assert service.get() == {'actions': DEFAULT_ACTIONS}


def test_default_result_does_not_mutate_defaults(tmp_path):
    service, _ = make_service(tmp_path)
    service.get()['actions'].clear()
    assert service.get()['actions'] == ['clean', 'relay', 'subtitle-toggle', 'home']
