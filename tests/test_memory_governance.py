import json
from types import SimpleNamespace

import pytest

from toolbox.project_memory import MemoryStore
from toolbox.project_memory.cli import parser, run
from toolbox.project_memory.governance import exclusion_reason, recall_rows, sweep_rows
from toolbox.project_memory_curation import Curation


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / 'db', device_id='fixture')
    s.create_project('实验', 'p')
    yield s
    s.close()


def save(s, id='k', **kw):
    return s.save_entry({'id':id, 'kind':'knowledge', 'scope':'project', 'project_id':'p',
                        'title':'经验治理', 'body':'条件和来源', **kw})


def test_recall_ranks_title_and_preserves_multiple_hits_with_budget(store):
    save(store, 'task', title='科研恢复点', body='a'*1000+'经验治理'+'b'*500)
    save(store, 'actual', title='经验治理：适用条件', body='原文前提'*400)
    result = run(store, parser().parse_args(['recall', '--project', 'p', '--query', '经验治理', '--max-chars', '120', '--limit', '2']))
    assert [r['id'] for r in result['entries']] == ['actual', 'task']
    assert sum(len(r['body']) for r in result['entries']) <= 120
    assert '经验治理' in result['entries'][1]['body']
    assert all(r['retrieval']['truncated'] for r in result['entries'])
    assert result['entries'][0]['retrieval']['evidence'] == 'unclassified'


def test_recall_excludes_invalid_but_candidates_are_explicitly_discoverable(store):
    save(store, 'live')
    save(store, 'old', archived=True)
    save(store, 'rejected', promotion={'state': 'rejected'})
    save(store, 'candidate', governance={'lifecycle': 'candidate'})
    save(store, 'retired', governance={'lifecycle': 'retired', 'reason': '已失效'})
    save(store, 'replaced', governance={'superseded_by': 'live'})
    rows = store.entries(include_done=True)
    assert [r['id'] for r in recall_rows(rows)['entries']] == ['live']
    assert {r['id'] for r in recall_rows(rows, include_candidates=True)['entries']} == {'live', 'candidate'}
    assert exclusion_reason({**rows[0], 'conflict': True}) == 'conflict'


@pytest.mark.parametrize('g', [{'usage': 'constraint', 'origin': 'paper', 'evidence': 'reported'},
                             {'lifecycle': 'retired'}, {'review_after': 'bad-date'}, {'evidence': []}])
def test_bad_governance_is_rejected_without_write(store, g):
    before = store.info()['operation_count']
    with pytest.raises(ValueError): save(store, governance=g)
    assert store.info()['operation_count'] == before


def test_current_read_omits_duplicate_history_but_full_read_retains_it(store):
    item = save(store)
    assert 'versions' not in run(store, parser().parse_args(['read', item['id'], '--current']))
    assert 'versions' in run(store, parser().parse_args(['read', item['id']]))
    assert recall_rows(store.entries(), max_chars=0)['entries'] == []


def test_sweep_is_readonly_incremental_and_never_deletes_duplicates(store, tmp_path):
    a = save(store, 'one')
    save(store, 'two')
    before = store.info()['operation_count']
    args = parser().parse_args(['sweep', '--project', 'p', '--checkpoint', str(tmp_path/'checkpoint.json')])
    first, second = run(store, args), run(store, args)
    assert first['notify'] and not second['notify']
    assert second['pending_count'] == 2 and second['findings'] == []
    assert 'exact_duplicate_candidate' in first['findings'][0]['reasons']
    assert store.info()['operation_count'] == before
    store.save_entry({**a, 'body': '新正文'}, a['heads'])
    third = run(store, args)
    assert third['notify'] and third['changed'] == 2  # Related duplicate changed too.
    assert len(store.entries()) == 2


def test_sweep_checks_review_date_without_disabling_or_repeating(store, tmp_path):
    item = save(store, governance={'origin':'paper','evidence':'reported','sources':['paper:1'], 'review_after':'2026-10-26'})
    args = (store.entries(), store.library_id, 'p', tmp_path/'state.json')
    assert not sweep_rows(*args, today='2026-10-25')['notify']
    assert sweep_rows(*args, today='2026-10-26')['notify']
    assert not sweep_rows(*args, today='2026-10-27')['notify']
    assert exclusion_reason(item) is None
    with pytest.raises(ValueError): sweep_rows(store.entries(), 'other', 'p', tmp_path/'state.json')


def test_quarantine_and_restore_keep_history(store):
    original = save(store)
    quarantined = store.save_entry({**original,'governance':{'lifecycle':'quarantined','reason':'待检查'}}, original['heads'])
    assert recall_rows(store.entries())['entries'] == []
    restored = store.save_entry({**quarantined,'governance':{'lifecycle':'active','reason':'检查后恢复'}}, quarantined['heads'])
    assert recall_rows(store.entries())['entries'][0]['id'] == original['id']
    assert len([op for op in store.export_operations() if op['entity_id'] == original['id']]) == 3
    assert not restored['conflict']


def test_auto_curation_does_not_consume_retired_or_rejected_records(store):
    source = save(store, 'source')
    save(store, 'bad1', scope='global', project_id=None, archived=True)
    save(store, 'bad2', scope='global', project_id=None, promotion={'state':'rejected'})
    save(store, 'bad3', scope='global', project_id=None, governance={'lifecycle':'quarantined','reason':'错'})
    save(store, 'good', scope='global', project_id=None)
    seen=[]
    def chat(messages, **kwargs):
        seen.extend(json.loads(messages[1]['content'])['existing_global'])
        return json.dumps({'action':'defer','reason':'证据不足'})
    curator=Curation(SimpleNamespace(providers=SimpleNamespace(chat=chat)), store)
    candidate=curator.promote(source['id'])
    job=SimpleNamespace(params={'id':candidate['id']},progress=lambda *a:None, check_cancelled=lambda:None,mark_committed=lambda:None)
    curator.curate(job)
    assert [r['id'] for r in seen] == ['good']
