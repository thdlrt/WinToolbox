"""Deterministic retrieval and read-only knowledge review; no model decisions."""
from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path


CHOICES = {
    'origin': {'user', 'paper', 'observation', 'inference', 'legacy'},
    'evidence': {'explicit', 'reported', 'local_checked', 'hypothesis', 'unclassified'},
    'usage': {'constraint', 'reference', 'suggestion'},
    'lifecycle': {'active', 'candidate', 'quarantined', 'retired'},
}


def validate_governance(data):
    g = data.get('governance')
    if g is None:
        return  # Legacy records stay readable; never silently promote their evidence.
    if not isinstance(g, dict):
        raise ValueError('governance 必须是对象')
    for field, choices in CHOICES.items():
        if field in g and (not isinstance(g[field], str) or g[field] not in choices):
            raise ValueError('无效 governance.' + field)
    for field in ('sources', 'applies_to'):
        if field in g and (not isinstance(g[field], list) or any(not isinstance(x, str) or not x.strip() for x in g[field])):
            raise ValueError('governance.' + field + ' 必须是非空字符串数组')
    for field in ('reason', 'superseded_by'):
        if field in g and (not isinstance(g[field], str) or not g[field].strip()):
            raise ValueError('governance.' + field + ' 必须是非空字符串')
    if g.get('superseded_by') == data.get('id') and g.get('superseded_by'):
        raise ValueError('记录不能替代自身')
    if 'review_after' in g:
        try:
            date.fromisoformat(g['review_after'])
        except (ValueError, TypeError):
            raise ValueError('review_after 必须是 ISO 日期') from None
    if g.get('usage') == 'constraint' and (g.get('origin') != 'user' or g.get('evidence') != 'explicit'):
        raise ValueError('约束用途仅用于用户明确要求；经验和论文默认作为参考或建议')
    if g.get('lifecycle') in ('quarantined', 'retired') and not g.get('reason'):
        raise ValueError('隔离或退役需要 reason，保留处置依据')


def exclusion_reason(row, include_candidates=False):
    if row.get('internal'): return 'internal'
    if row.get('conflict'): return 'conflict'
    if row.get('archived') or row.get('status') == 'done': return 'archived_or_done'
    promotion = row.get('promotion', {}).get('state')
    if promotion == 'rejected': return 'rejected'
    g = row.get('governance', {})
    if g.get('superseded_by'): return 'superseded'
    if g.get('lifecycle') in ('quarantined', 'retired'): return g['lifecycle']
    if not include_candidates and (promotion == 'candidate' or g.get('lifecycle') == 'candidate'):
        return 'candidate'
    return None


def relevance(row, query):
    q = query.strip().casefold()
    if not q: return 0
    title, body = row.get('title', '').casefold(), row.get('body', '').casefold()
    if q == title: return 100
    if q in title: return 50
    return 10 if q in body else 0


def excerpt(body, query, cap):
    cap = max(0, cap)
    pos = body.casefold().find(query.casefold()) if query else -1
    start = max(0, pos - min(80, cap // 4)) if pos >= 0 and len(body) > cap else 0
    end = min(len(body), start + cap)
    return body[start:end], {'start': start, 'end': end, 'total_chars': len(body),
                              'truncated': start > 0 or end < len(body)}


def recall_rows(rows, project_id=None, query='', limit=5, max_chars=8000, include_candidates=False):
    eligible = [r for r in rows if not exclusion_reason(r, include_candidates)
                and (project_id is None or r.get('project_id') == project_id or r.get('scope') == 'global')
                and (not query.strip() or relevance(r, query))]
    eligible.sort(key=lambda r: (0 if project_id and r.get('project_id') == project_id else 1,
                                 -relevance(r, query), r['title'], r['id']))
    remaining = max(0, max_chars)
    selected = []
    picked = eligible[:max(0, limit)]
    for index, row in enumerate(picked):
        if remaining <= 0: break
        # Share the text budget so one large result cannot consume every slot.
        cap = max(1, remaining // (len(picked) - index))
        body, span = excerpt(row.get('body', ''), query.strip(), cap)
        keys = ('id', 'title', 'kind', 'scope', 'project_id', 'knowledge_type', 'status', 'heads', 'conflict', 'governance', 'promotion')
        item = {k: row[k] for k in keys if k in row}
        item.update(body=body, retrieval={**span, 'match': 'title' if query and query.casefold() in row['title'].casefold() else 'body',
                                        'read_id': row['id'], 'usage': row.get('governance', {}).get('usage', 'reference'),
                                        'evidence': row.get('governance', {}).get('evidence', 'unclassified')})
        selected.append(item)
        remaining -= len(body)
    return {'entries': selected, 'total': len(eligible), 'body_chars': max(0, max_chars) - remaining,
            'budget_unit': 'body_characters_not_tokens', 'omitted': len(eligible) - len(selected)}


def sweep_rows(rows, library_id, project_id, checkpoint=None, today=None):
    """Review only. Checkpoint stores dedup state, never changes knowledge records."""
    today = today or date.today().isoformat()
    checkpoint = Path(checkpoint) if checkpoint else None
    previous = {}
    if checkpoint and checkpoint.exists():
        previous = json.loads(checkpoint.read_text(encoding='utf-8'))
        if previous.get('library_id') != library_id or previous.get('project_id') != project_id:
            raise ValueError('巡检 checkpoint 不属于当前库/项目')
    rows = [r for r in rows if r.get('project_id') == project_id and not r.get('internal')]
    groups = {}
    for row in rows:
        if not exclusion_reason(row) and row.get('kind') == 'knowledge' and row.get('body', '').strip():
            key = json.dumps([row.get('knowledge_type'), row['body'].strip(), row.get('governance', {})], sort_keys=True, ensure_ascii=False)
            groups.setdefault(hashlib.sha256(key.encode()).hexdigest(), []).append(row['id'])
    duplicates = {rid: sorted(ids) for ids in groups.values() if len(ids) > 1 for rid in ids}
    seen, findings, changed = {}, [], []
    for row in rows:
        g = row.get('governance', {})
        due = bool(g.get('review_after') and g['review_after'] <= today)
        reasons = []
        if row.get('conflict'): reasons.append('conflict')
        if not exclusion_reason(row, include_candidates=True):
            if not row.get('body', '').strip(): reasons.append('empty_body')
            if row.get('kind') == 'knowledge':
                if not g.get('origin') or not g.get('evidence') or not g.get('sources'): reasons.append('provenance_incomplete')
                if due: reasons.append('review_due_not_proven_invalid')
                if row['id'] in duplicates: reasons.append('exact_duplicate_candidate')
        fingerprint = json.dumps([sorted(row.get('heads', [])), due, duplicates.get(row['id'], []), reasons], ensure_ascii=False)
        seen[row['id']] = fingerprint
        is_changed = previous.get('seen', {}).get(row['id']) != fingerprint
        if is_changed: changed.append(row['id'])
        if reasons:
            findings.append({'id': row['id'], 'title': row['title'], 'heads': row.get('heads', []),
                             'reasons': reasons, 'related_duplicates': duplicates.get(row['id'], []), 'new_or_changed': is_changed})
    state = {'schema_version': 1, 'library_id': library_id, 'project_id': project_id, 'seen': seen, 'findings': findings}
    if checkpoint:
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temp = checkpoint.with_name(checkpoint.name + '.tmp')
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(checkpoint)
    new = [f for f in findings if f['new_or_changed']]
    return {'checked': len(rows), 'changed': len(changed), 'findings': new, 'pending_count': len(findings),
            'notify': bool(new), 'knowledge_mutated': False, 'checkpoint': str(checkpoint) if checkpoint else None}
