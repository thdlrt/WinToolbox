"""Compact read-only Codex snapshots and a bounded CJK glyph atlas for the robot."""
import base64
from functools import lru_cache
import json
import math
from pathlib import Path
import time
from .stackchan_quota import accounts_for_robot

SPEEDS = {'slow': 20, 'normal': 12, 'fast': 8, 'rapid': 5}
PAGES = ('face', 'tasks', 'weather', 'pc', 'printer', 'quota')
_completed = {}


def timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return 0
    return value / 1000 if value > 100_000_000_000 else value


def recent_tasks(sessions, now):
    """Idle is not completion. Keep active work and explicitly verified recent ends."""
    rows = []
    for row in sessions.get('rows', []):
        if row.get('stale'):
            continue
        state = row['state']
        completed = timestamp(row.get('completed_at'))
        if state in ('running', 'waiting', 'error'):
            rows.append(dict(row))
        elif state in ('idle', 'done') and completed and 0 <= now - completed <= 3600:
            rows.append({**row, 'state': 'done', 'updated_at': completed})
    rows.sort(key=lambda r: ({'waiting': 0, 'error': 0, 'running': 1, 'done': 2}[r['state']],
                             -timestamp(r.get('updated_at'))))
    return rows


def read_sessions(thread_id):
    from .feishu_bridge import connect_codex
    client = connect_codex(thread_id, launch=False)
    try:
        result = client.tool('list_threads', {'limit': 50})
        raw = result.get('structuredContent')
        if not isinstance(raw, dict):
            raw = json.loads(next(b['text'] for b in result.get('content', []) if b.get('type') == 'text'))
        seen, rows = set(), []
        now, inspected = time.time(), 0
        for item in raw.get('pinnedThreads', []) + raw.get('threads', []):
            if item.get('kind') != 'codex' or item.get('hostId') != 'local' or item.get('id') in seen:
                continue
            seen.add(item['id'])
            status = item.get('status')
            if isinstance(status, dict):
                status = status.get('type')
            status = str(status).replace('_', '').replace('-', '').lower()
            state = {'active': 'running', 'idle': 'idle', 'needsinput': 'waiting',
                     'needsattention': 'waiting', 'waiting': 'waiting', 'error': 'error'}.get(status)
            if status == 'notloaded' and 0 <= now - timestamp(item.get('updatedAt')) <= 3600:
                state = 'idle'  # Candidate only; a verified completed turn is still required.
            if state is None:
                continue
            updated = timestamp(item.get('updatedAt'))
            row = {'id': item['id'], 'title': str(item.get('title') or '未命名任务')[:80],
                   'state': state, 'updated_at': updated}
            key = (item['id'], updated)
            if state == 'idle' and 0 <= now - updated <= 3600:
                if key not in _completed and inspected < 4:
                    inspected += 1
                    try:
                        detail = client.tool('read_thread', {'threadId': item['id'], 'turnLimit': 1,
                                             'includeOutputs': False, 'maxOutputCharsPerItem': 1})
                        detail = detail.get('structuredContent') or json.loads(next(
                            b['text'] for b in detail.get('content', []) if b.get('type') == 'text'))
                        turn = next(iter(detail.get('turns', [])), {})
                        _completed[key] = timestamp(turn.get('completedAt')) if turn.get('status') == 'completed' else 0
                    except Exception:
                        # Failure never promotes an idle chat to completed; retry later.
                        pass
                if _completed.get(key):
                    row['completed_at'] = _completed[key]
            rows.append(row)
        for key in list(_completed):
            if now - key[1] > 3600:
                _completed.pop(key, None)
        priority = {'waiting': 0, 'error': 0, 'running': 1, 'idle': 2}
        rows.sort(key=lambda x: (priority[x['state']], -float(x['updated_at'] or 0)))
        return {'available': True, 'sampled_at': time.time(), 'rows': rows, 'total': len(rows),
                'running': sum(r['state'] == 'running' for r in rows),
                'waiting': sum(r['state'] in ('waiting', 'error') for r in rows)}
    finally:
        client.close()


@lru_cache(maxsize=256)
def glyph(char):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 17)
    width = min(26, max(1, math.ceil(font.getlength(char))))
    image = Image.new('1', (width, 22))
    ImageDraw.Draw(image).text((0, 18), char, font=font, anchor='ls', fill=1)
    # Continuous bits avoid the padding Pillow adds to each narrow row.
    raw = bytearray((width * 22 + 7) // 8)
    for index, value in enumerate(image.get_flattened_data() if hasattr(image, 'get_flattened_data') else image.getdata()):
        if value:
            raw[index // 8] |= 128 >> (index % 8)
    return [width, base64.b64encode(raw).decode('ascii')]


def panel_packet(packet, sessions, printer, config, now=None, hold_group=None, hold_quota=None):
    """Four compact rows, tiny shared quota, and only the glyphs actually on screen."""
    now = time.time() if now is None else now
    rows = recent_tasks(sessions, packet.get('epoch', now))
    pages = max(1, math.ceil(len(rows) / 4))
    group = int(now // SPEEDS[config['rotation_speed']]) % pages if config['rotation_enabled'] else 0
    if hold_group is not None:
        group = max(0, min(pages - 1, hold_group))
    visible = rows[group * 4:(group + 1) * 4]
    accounts = accounts_for_robot(packet.get('ai_quota', {}), packet.get('quota', {}))
    quota_pages = max(1, math.ceil(len(accounts) / 3))
    quota_group = int(now // SPEEDS[config['rotation_speed']]) % quota_pages if config['rotation_enabled'] else 0
    if hold_quota is not None:
        quota_group = max(0, min(quota_pages - 1, hold_quota))
    result = {**packet, 'sessions': {k: sessions[k] for k in ('available', 'sampled_at', 'total', 'running', 'waiting') if k in sessions},
              'printer': {k: printer[k] for k in ('available', 'connected', 'sampled_at', 'state', 'progress', 'remaining_minutes',
                          'layer', 'total_layers', 'nozzle', 'bed', 'error_code', 'configured') if k in printer},
              'rotation': {'enabled': config['rotation_enabled'], 'seconds': SPEEDS[config['rotation_speed']],
                           'pages': config['rotation_pages'], 'touch_pause': 20}}
    result['ai_quota'] = {'accounts': [], 'page': quota_group + 1, 'pages': quota_pages, 'total': len(accounts)}
    # An error remains visible as stale/unavailable; never mark a failed refresh as fresh.
    result['sessions']['error'] = bool(sessions.get('error'))
    result['sessions'].update(total=len(rows), running=sum(r['state'] == 'running' for r in rows),
                              waiting=sum(r['state'] in ('waiting', 'error') for r in rows),
                              done=sum(r['state'] == 'done' for r in rows))
    result['sessions'].update(page=group + 1, pages=pages)
    result['printer']['name'] = str(config.get('printer_name') or 'Bambu P2S')[:14]
    def fit(value, width):
        value = str(value)[:40]
        if sum(glyph(c)[0] for c in value) <= width:
            return value
        text, used = '', glyph('…')[0]
        for char in value:
            used += glyph(char)[0]
            if used > width:
                break
            text += char
        return text + '…'

    title_width = 276
    while True:
        result['sessions']['rows'] = [{k: r[k] for k in ('id', 'state', 'updated_at')} | {'title': fit(r['title'], title_width)} for r in visible]
        result['printer']['title'] = fit(printer.get('title') or '', title_width)
        result['ai_quota']['accounts'] = [{**a, 'name': fit(a['name'], min(title_width, 190))}
                                        for a in accounts[quota_group * 3:(quota_group + 1) * 3]]
        texts = [r['title'] for r in result['sessions']['rows']] + [result['printer']['name'], result['printer']['title']]
        texts += [a['name'] for a in result['ai_quota']['accounts']]
        chars = set(''.join(texts))
        result['glyphs'] = {char: glyph(char) for char in sorted(chars)}
        if len(result['glyphs']) <= 100 and len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) <= 7000:
            return result
        title_width -= 16
        if title_width < 64:
            raise ValueError('任务台数据超过设备安全大小，请刷新数据')
