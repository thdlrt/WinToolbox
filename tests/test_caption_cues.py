"""Stable cue assembly, exact text coverage and explicit ASR delta semantics."""
import pytest

from toolbox.caption_cues import CueAssembler, split_cues, transcript_update, units


def test_delta_appends_but_cumulative_snapshots_replace():
    event = 'conversation.item.input_audio_transcription.'
    text = transcript_update('', event + 'delta', {'delta': 'Hello'})
    text = transcript_update(text, event + 'delta', {'delta': ' world'})
    assert text == 'Hello world'
    assert transcript_update(text, event + 'text', {'text': 'Hello', 'stash': ' world!'}) == 'Hello world!'
    assert transcript_update(text, event + 'completed', {'transcript': 'Hello world.'}) == 'Hello world.'
    assert transcript_update(text, event + 'completed', {}) == text


def test_typical_english_boundaries_preserve_titles_currency_and_decimal():
    text = 'Dr. Smith paid $20.50 today. The result is clear! Yes?'
    assert split_cues(text) == ['Dr. Smith paid $20.50 today.', 'The result is clear!', 'Yes?']
    long = 'The first words must stay visible while a speaker continues with a longer explanation about subtitles and translation accuracy.'
    parts = split_cues(long)
    assert ' '.join(parts) == long and parts[0].startswith('The first words')
    assert len(parts) > 1 and all(units(part) <= 84 for part in parts)
    chinese = '这是完整的第一句话。' + '字幕需要保留开头并按照合适的长度分页显示' * 4 + '。'
    assert ''.join(split_cues(chinese)) == chinese


@pytest.mark.parametrize('committed,revised', [
    ('Go go.', 'Go. Next sentence remains.'),
    ('Thank you.', 'Thanks. Next sentence remains.'),
    ('This result is good.', 'This result is very good. Next sentence remains.'),
    ('An important result is ready.', 'Important result is ready, Next sentence remains.'),
])
def test_consumed_prefix_revision_never_swallows_or_repeats_next_sentence(committed, revised):
    assembler = CueAssembler()
    assert assembler.accept('u', committed, False, 0, 1, 0) == []
    first = assembler.accept('u', committed, False, 0, 2, 1.3)
    assert [cue['text'] for cue in first] == [committed]
    tail = assembler.accept('u', revised, True, 0, 4, 4)
    assert [cue['text'] for cue in tail] == ['Next sentence remains.']


def test_duplicate_final_is_ignored_but_repeated_speech_is_kept():
    assembler = CueAssembler()
    first = assembler.accept('one', 'Go again.', True, 0, 1, 1)
    assert assembler.accept('one', 'Go again.', True, 0, 1, 1.1) == []
    other = assembler.accept('two', 'Go again.', True, 1.5, 2.5, 2.5)
    reused_id = assembler.accept('one', 'Go again.', True, 3, 4, 4)
    assert [cue['text'] for cue in first + other + reused_id] == ['Go again.'] * 3
    assert len({cue['id'] for cue in first + other + reused_id}) == 3


def test_revised_partial_words_must_age_before_becoming_stable():
    assembler = CueAssembler()
    assert assembler.accept('u', 'Good sometimes should help.', False, 0, 1, 0) == []
    assert assembler.accept('u', 'Good subtitles should help.', False, 0, 2, 1.1) == []
    assert assembler.accept('u', 'Good subtitles should help. More words', False, 0, 3, 1.5) == []
    cue = assembler.accept('u', 'Good subtitles should help. More words arrive', False, 0, 4, 2.5)
    assert [value['text'] for value in cue] == ['Good subtitles should help.']


def test_cue_assembler_state_is_bounded_for_long_sessions():
    assembler = CueAssembler()
    for index in range(50):
        assembler.accept(str(index), 'An unfinished phrase', False, index, index + 1, index)
    assert len(assembler.states) <= 32
    for index in range(350):
        assembler.accept('done' + str(index), 'A finished sentence.', True, index, index + 1, index)
    assert len(assembler.completed) <= 300


# Selected snapshots from the real 35.52-second synthetic-speech API probe.
# Original fixture: .build/caption-quality-011/semantic.json. No credentials/audio.
def test_real_api_snapshots_display_before_server_final_without_lost_head_or_tail():
    a = 'Good subtitles should give the viewer time to read a complete thought.'
    b = 'When a speaker continues without a long pause, the words must still be divided into clear sentences.'
    c = 'For example, an English sentence and its Chinese translation should appear together, even if translation takes a little longer.'
    d = 'The first words should never disappear just because the next sentence is arriving.'
    e = 'Prices such as $20 and names such as Dr. Smith must remain accurate.'
    f = 'At the end of this example, every sentence should have been displayed in order.'
    rows = [
        (.36, 1, False, 'Good'),
        (.77, 1, False, 'Good sometimes'),
        (2.45, 1, False, 'Good subtitles should give the viewer time'),
        (3.72, 1, False, a[:-1]),
        (5.23, 1, False, a[:-1] + ' when a speaker'),
        (6.89, 1, False, a + ' When a speaker continues without a long pause'),
        (9.11, 1, False, a + ' When a speaker continues without a long pause, the words must still be divided'),
        (10.33, 1, False, a + ' ' + b[:-1]),
        (11.27, 1, False, a + ' ' + b + ' For'),
        (11.73, 1, False, a + ' ' + b + ' For example'),
        (14.72, 1, False, a + ' ' + b + ' For example, an English sentence and its Chinese translation should appear'),
        (16.23, 1, False, a + ' ' + b + ' For example, an English sentence and its Chinese translation should appear together'),
        (17.92, 1, False, a + ' ' + b + ' ' + c[:-12]),
        (19.05, 1, True, ' '.join((a, b, c))),
        (20.62, 2, False, 'The first words should never disappear'),
        (22.89, 2, False, 'The first words should never disappear just because the next sentence is'),
        (23.44, 2, False, d[:-1]),
        (24.16, 2, False, d + ' prices'),
        (25.67, 2, False, d + ' prices such as twenty dollars'),
        (27.36, 2, False, d + ' Prices such as $20 and names such as Dr'),
        (29.52, 2, False, d + ' ' + e[:-1]),
        (30.03, 2, False, d + ' ' + e + ' at the end of'),
        (30.75, 2, False, d + ' ' + e + ' at the end of this example'),
        (34.47, 2, True, ' '.join((d, e, f))),
    ]
    assembler, emitted = CueAssembler(), []
    for at, sid, final, text in rows:
        emitted.extend((at, cue) for cue in assembler.accept(str(sid), text, final, 0 if sid == 1 else 19, at, at))
    cues = [cue for _, cue in emitted]
    assert emitted[0][0] == 6.89 < 19.05
    assert len(cues) == 8
    assert ' '.join(cue['text'] for cue in cues) == ' '.join((a, b, c, d, e, f))
    assert len({cue['id'] for cue in cues}) == len(cues)
    assert all(2000 <= cue['duration_ms'] <= 4500 for cue in cues)
    assert any('Dr. Smith' in cue['text'] and '$20' in cue['text'] for cue in cues)
