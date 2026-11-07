"""Allowlisted display projection of the existing multi-account quota feature."""
import math


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def accounts_for_robot(listing, codex):
    accounts = []
    windows = codex.get('windows', [])
    if windows:
        meters = []
        for row in windows[:3]:
            minutes = row.get('minutes')
            label = f'{minutes // 1440:g}d' if minutes and minutes % 1440 == 0 else f'{minutes // 60:g}h' if minutes and minutes % 60 == 0 else 'Limit'
            meters.append({'label': label, 'remaining': row['remaining']})
        accounts.append({'name': 'Codex', 'status': 'error' if codex.get('error') else 'ok',
                         'fetched_at': codex.get('sampled_at', 0), 'meters': meters})
    labels = {'5 小时': '5h', '每周': '7d', '每月': '30d', 'Key 额度': 'Key'}
    for row in listing.get('accounts', [])[:30]:
        snapshot = row.get('snapshot') or {}
        meters = [{'label': labels.get(m.get('label'), 'Limit'),
                   'remaining': round(max(0, min(100, m['remaining_percent'])), 1)}
                  for m in snapshot.get('meters', [])[:3] if finite(m.get('remaining_percent'))]
        account = {'name': str(row.get('name') or row.get('provider') or 'AI')[:40],
                   'status': row.get('status') if row.get('status') in ('ok', 'auth_required', 'rate_limited', 'error', 'not_checked') else 'not_checked',
                   'fetched_at': snapshot.get('fetched_at') or 0, 'meters': meters}
        balance = next((b for b in snapshot.get('balances', []) if finite(b.get('value'))), None)
        if balance:
            account['balance'] = round(balance['value'], 2)
            account['currency'] = str(balance.get('currency') or '')[:4]
        accounts.append(account)
    return accounts
