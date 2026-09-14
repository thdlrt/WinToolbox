from types import SimpleNamespace

from toolbox.voices import catalogue


def voices(model, kind='dashscope'):
    return catalogue(SimpleNamespace(get=lambda: {'roles': {'tts': {'model': model, 'provider_id': 'p'}},
        'providers': [{'id': 'p', 'kind': kind}]}))


def test_catalogue_matches_dated_models_without_secret_or_network():
    full = voices('qwen3-tts-flash')
    assert len(full['voices']) == 48
    assert any(row['id'] == 'Aiden' for row in full['voices'])
    assert not any(row['id'] == 'Aiden' for row in voices('qwen3-tts-flash-2025-09-18')['voices'])
    assert not any(row['id'] == 'Jennifer' for row in voices('qwen3-tts-instruct-flash')['voices'])
    assert len(voices('qwen-tts-2025-04-10')['voices']) == 4


def test_unknown_models_and_other_vendors_do_not_advertise_unverified_voices():
    for model, kind in [('future-tts', 'dashscope'), ('qwen3-tts-vc-flash', 'dashscope'), ('tts-1', 'openai')]:
        result = voices(model, kind)
        assert result['voices'] == [] and result['custom_allowed']
