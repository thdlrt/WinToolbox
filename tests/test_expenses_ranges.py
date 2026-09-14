"""Period-wide reimbursement totals stay exact and separate from list filters."""
import json
import time
from pathlib import Path

import pytest

from toolbox.app import App
from toolbox.features.expenses import formatted


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'data', register_live=False)
    yield instance
    instance.close()
    instance.jobs.pool.shutdown(wait=True)


def save(app, **fields):
    return app.call('expenses.save', {'title': 'API subscription', 'date': '2025-12-31',
                                     'amount': '10.00', 'currency': 'CNY', **fields})


def by_currency(result, currency):
    return next(row for row in result['summary'] if row['currency'] == currency)


def test_inclusive_cross_year_period_separates_expenses_reimbursements_and_income(app):
    save(app, amount='0.01', status='waiting')
    save(app, amount='12.30', date='2026-01-01', status='reimbursed')
    save(app, amount='5.99', date='2026-01-31', status='non_reimbursable')
    save(app, amount='4.00', entry_type='income', date='2026-01-15', status='reimbursed')
    save(app, amount='100', currency='USD', entry_type='income')
    save(app, amount='50', currency='EUR', status='waiting')
    save(app, amount='900', date='2025-11-30')
    save(app, amount='800', date='2026-02-01')
    result = app.call('expenses.list', {'start_month': '2025-12', 'end_month': '2026-01'})
    assert result['month'] is None and len(result['items']) == 6
    assert result['summary_scope'] == 'period'
    cny = by_currency(result, 'CNY')
    assert cny['expense_total'] == '18.30' and cny['reimbursed'] == '12.30'
    assert cny['pending'] == '0.01' and cny['non_reimbursable'] == '5.99'
    assert cny['transfer_income'] == '4.00' and cny['net'] == '-2.00' and cny['net_minor'] == -200
    assert cny['share_due'] == '3.00' and cny['settlement_remaining'] == '-1.00'
    assert cny['count'] == 4 and cny['expense_count'] == 3 and cny['income_count'] == 1
    assert cny['total'] == cny['expense_total'] and cny['personal'] == cny['non_reimbursable']
    usd = by_currency(result, 'USD')
    assert usd['expense_total'] == '0.00' and usd['net'] == '100.00'
    assert usd['share_due_minor'] == 0 and usd['settlement_remaining_minor'] == -10000
    assert usd['reimbursed'] == usd['pending'] == usd['non_reimbursable'] == '0.00'
    assert by_currency(result, 'EUR')['net_minor'] == -5000
    assert by_currency(result, 'EUR')['share_due_minor'] == 2500


@pytest.mark.parametrize('cost,reimbursed,income,share,remaining', [
    ('1000', '200', '300', '400.00', '100.00'),
    ('1000', '200', '400', '400.00', '0.00'),
    ('1000', '200', '450', '400.00', '-50.00'),
    ('10.01', '2.00', '4.00', '4.01', '0.01'),
    ('0.01', '0', '0', '0.01', '0.01'),
    ('0.02', '0', '0.02', '0.01', '-0.01'),
    ('10', '10', '1', '0.00', '-1.00'),
    ('0', '0', '0', '0.00', '0.00'),
])
def test_half_share_and_transfer_comparison_round_to_cents(app, cost, reimbursed, income, share, remaining):
    from decimal import Decimal
    # Waiting costs are included until actually reimbursed.
    save(app, amount=str(Decimal(cost) - Decimal(reimbursed)), status='waiting')
    save(app, amount=reimbursed, status='reimbursed')
    if Decimal(income):
        save(app, amount=income, entry_type='income')
    totals = by_currency(app.call('expenses.list', {'month': '2025-12'}), 'CNY')
    assert totals['share_due'] == share and totals['settlement_remaining'] == remaining
    assert totals['share_due_minor'] == int(Decimal(share) * 100)
    assert totals['settlement_remaining_minor'] == int(Decimal(remaining) * 100)


def test_split_once_after_period_aggregation_and_recalculate_reimbursement(app):
    one = save(app, amount='0.01', status='waiting')
    save(app, amount='0.01', status='non_reimbursable', date='2026-01-01')
    params = {'start_month': '2025-12', 'end_month': '2026-01'}
    totals = by_currency(app.call('expenses.list', params), 'CNY')
    # Rounding each entry or month would overstate the half share as 0.02.
    assert totals['share_due'] == '0.01'
    app.call('expenses.save', {'id': one['id'], 'status': 'reimbursed'})
    totals = by_currency(app.call('expenses.list', params), 'CNY')
    assert totals['reimbursed'] == '0.01' and totals['share_due'] == '0.01'
    app.call('expenses.save', {'id': one['id'], 'amount': '20', 'status': 'waiting'})
    totals = by_currency(app.call('expenses.list', params), 'CNY')
    assert totals['reimbursed'] == '0.00' and totals['share_due'] == '10.01'


def test_half_share_aggregation_keeps_large_amounts_exact(app):
    for _ in range(3):
        save(app, amount='999999999.99', status='non_reimbursable')
    save(app, amount='999999999.99', entry_type='income')
    totals = by_currency(app.call('expenses.list', {'month': '2025-12'}), 'CNY')
    assert totals['expense_total'] == '2999999999.97'
    assert totals['share_due_minor'] == 149999999999
    assert totals['share_due'] == '1499999999.99'
    assert totals['settlement_remaining_minor'] == 50000000000
    assert totals['settlement_remaining'] == '500000000.00'


def test_list_filters_never_change_period_summary_or_category_options(app):
    expected = save(app, title='OpenAI API', category='AI订阅', notes='September usage', status='waiting')
    save(app, title='Taxi', category='其他报销', status='reimbursed')
    save(app, title='Received transfer', category='转账收入', entry_type='income', currency='USD')
    base = app.call('expenses.list', {'month': '2025-12'})
    filters = [
        {'query': 'openAI', 'entry_type': 'expense', 'status': 'waiting', 'category': 'AI订阅', 'currency': 'cny'},
        {'query': 'September'}, {'entry_type': 'income'}, {'status': 'reimbursed'},
        {'category': 'no-match'}, {'currency': 'HKD'}, {'query': 'no results'},
    ]
    for values in filters:
        result = app.call('expenses.list', {'month': '2025-12', **values})
        assert result['summary'] == base['summary']
        assert result['categories'] == base['categories']
        assert result['summary_scope'] == 'period'
    matched = app.call('expenses.list', {'month': '2025-12', **filters[0]})
    assert [item['id'] for item in matched['items']] == [expected['id']]
    assert app.call('expenses.list', {'month': '2025-12', 'entry_type': 'all', 'status': '', 'currency': None})['items'] == base['items']
    assert app.call('expenses.list', {'month': '2025-12', 'status': 'submitted'})['items'][0]['id'] == expected['id']


@pytest.mark.parametrize('legacy,normalized', [('unsubmitted', 'waiting'), ('submitted', 'waiting'), ('personal', 'non_reimbursable'), ('reimbursed', 'reimbursed')])
def test_legacy_records_normalize_on_read_without_rewriting_files_or_identity(app, legacy, normalized):
    item = save(app)
    path = app.data_dir / 'expenses/items' / (item['id'] + '.json')
    old = dict(item, status=legacy)
    old.pop('entry_type')
    path.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding='utf-8')
    before = path.read_bytes()
    loaded = app.call('expenses.get', {'id': item['id']})
    assert loaded['entry_type'] == 'expense' and loaded['status'] == normalized
    assert loaded['id'] == old['id'] and loaded['revision'] == old['revision'] and loaded['attachments'] == old['attachments']
    listed = app.call('expenses.list', {'month': '2025-12'})
    assert listed['items'][0]['status'] == normalized and path.read_bytes() == before
    # Saving unchanged legacy aliases remains a read-only no-op as well.
    app.call('expenses.save', {'id': item['id'], 'revision': loaded['revision'], 'status': legacy})
    assert path.read_bytes() == before


def test_income_expense_type_switch_resets_status_and_keeps_attachment_identity(app, tmp_path):
    item = save(app, status='reimbursed')
    source = tmp_path / 'payment.pdf'
    source.write_bytes(b'%PDF-1.7\nFixture payment evidence')
    job = app.call('expenses.attach', {'id': item['id'], 'revision': item['revision'], 'paths': [str(source)], 'kind': 'payment'})
    deadline = time.monotonic() + 5
    while job['id'] in app.jobs.active and time.monotonic() < deadline:
        time.sleep(.01)
    assert app.jobs.get(job['id'])['status'] == 'completed'
    item = app.call('expenses.get', {'id': item['id']})
    attachments = item['attachments']
    income = app.call('expenses.save', {'id': item['id'], 'revision': item['revision'], 'entry_type': 'income', 'status': 'reimbursed'})
    assert income['status'] == 'not_applicable' and income['attachments'] == attachments
    assert income['id'] == item['id'] and income['revision'] == item['revision'] + 1
    summary = by_currency(app.call('expenses.list', {'month': '2025-12'}), 'CNY')
    assert summary['expense_total'] == summary['reimbursed'] == '0.00' and summary['transfer_income'] == '10.00'
    opened = app.call('expenses.attachment', {'id': income['id'], 'attachment_id': attachments[0]['id']})
    assert Path(opened['path']).read_bytes() == source.read_bytes()
    expense = app.call('expenses.save', {'id': income['id'], 'revision': income['revision'], 'entry_type': 'expense'})
    assert expense['status'] == 'waiting' and expense['attachments'] == attachments
    with pytest.raises(ValueError, match='已更新'):
        app.call('expenses.save', {'id': income['id'], 'revision': income['revision'], 'entry_type': 'income'})


def test_income_requires_positive_amount_and_forces_non_reimbursement_status(app):
    with pytest.raises(ValueError, match='大于零'):
        save(app, amount='0.00', entry_type='income')
    item = save(app, entry_type='income', status='waiting')
    assert item['status'] == 'not_applicable' and item['category'] == '转账收入'
    zero = save(app, amount='0', entry_type='expense')
    with pytest.raises(ValueError, match='大于零'):
        app.call('expenses.save', {'id': zero['id'], 'entry_type': 'income'})
    assert app.call('expenses.get', {'id': zero['id']})['entry_type'] == 'expense'


def test_all_is_literal_for_text_filters_and_type_switch_preserves_custom_category(app):
    item = save(app, title='All API expenses', category='all')
    save(app, title='Other expense', category='Other')
    for field in ('query', 'category'):
        assert [row['id'] for row in app.call('expenses.list', {'month': '2025-12', field: 'all'})['items']] == [item['id']]
    updated = app.call('expenses.save', {'id': item['id'], 'entry_type': 'income'})
    assert updated['category'] == 'all'


@pytest.mark.parametrize('minor,text', [(-1, '-0.01'), (-99, '-0.99'), (-100, '-1.00'), (-101, '-1.01'), (0, '0.00'), (1, '0.01')])
def test_negative_net_format_is_exact_without_float(minor, text):
    assert formatted(minor) == text


@pytest.mark.parametrize('params', [
    {'start_month': '2026-01'}, {'end_month': '2026-01'},
    {'start_month': '2026-02', 'end_month': '2026-01'},
    {'month': '2026-01', 'start_month': '2026-01', 'end_month': '2026-02'},
    {'start_month': '2026-13', 'end_month': '2027-01'}, {'query': 123}, {'currency': 'GBP'},
    {'status': 'unknown'}, {'entry_type': 'transfer'}, {'category': 'x' * 81},
])
def test_invalid_period_or_filters_are_rejected(app, params):
    with pytest.raises(ValueError):
        app.call('expenses.list', params)
