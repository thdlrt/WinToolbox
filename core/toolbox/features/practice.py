"""English listening practice with reusable, verified, per-sentence audio."""
import base64
import copy
import hashlib
import json
import re
import threading
import time
import uuid
import unicodedata
import wave
from pathlib import Path

from ..media import binary
from ..settings import atomic_json
from ..jobs import Cancelled


def split_sentences(text):
    text = str(text or '').strip()
    if not text or len(text) > 6000 or not re.search('[A-Za-z]', text):
        raise ValueError('请输入英文，最多 6000 字符')
    # Keep decimals, initials and common titles together; preserve all text.
    protected = re.sub(r'\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc)\.|\b[A-Za-z]\.(?=\s|[A-Za-z])|(?<=\d)\.(?=\d)',
                       lambda m: m[0].replace('.', '\u2024'), text, flags=re.I)
    pieces = re.split(r'(?<=[.!?])\s+|\n+', protected)
    result = []
    for piece in pieces:
        piece = ' '.join(piece.replace('\u2024', '.').split())
        # Bound individual requests and playback transfers without dropping words.
        while len(piece) > 450:
            end = piece.rfind(' ', 0, 450)
            if end <= 0:
                raise ValueError('单个单词过长，请检查输入')
            result.append(piece[:end])
            piece = piece[end:].strip()
        if piece:
            result.append(piece)
    if len(result) > 80:
        raise ValueError('请分批生成，每次最多 80 句')
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def valid_audio(path, meta):
    try:
        if hashlib.sha256(path.read_bytes()).hexdigest() != meta['sha256']:
            return False
        with wave.open(str(path), 'rb') as audio:
            return audio.getnframes() > 0 and audio.getframerate() == 24000 and audio.getnchannels() == 1 and audio.getsampwidth() == 2
    except (OSError, ValueError, KeyError, wave.Error, EOFError):
        return False


def register(app):
    root = app.data_dir / 'practice'
    cache = root / 'audio'
    records = root / 'items'
    archive = root / 'archive'
    folders_path = root / 'folders.json'
    cache.mkdir(parents=True, exist_ok=True)
    records.mkdir(parents=True, exist_ok=True)
    generation_lock = threading.Lock()
    records_lock = threading.RLock()

    def identifier(value):
        if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
            raise ValueError('无效的练习记录')
        return value

    def new_id():
        return uuid.uuid4().hex + uuid.uuid4().hex

    def load(item_id):
        path = records / (identifier(item_id) + '.json')
        if not path.is_file():
            raise ValueError('练习记录不存在')
        item = json.loads(path.read_text('utf-8'))
        if item.get('id') != item_id:
            raise ValueError('练习记录已损坏')
        # Existing hash-named items remain independent paragraphs. Never infer
        # that an old one-sentence item belonged to a different paragraph.
        if not item.get('audio_id') and (cache / (item_id + '.wav')).is_file():
            item['audio_id'] = item_id
        item.setdefault('revision', 1)
        item.setdefault('content_revision', 1)
        item.setdefault('folder_id', None)
        item.setdefault('fragments', [])
        item.setdefault('versions', [])
        item.setdefault('sentences', [])
        item.setdefault('stale', False)
        item.setdefault('status', 'ready' if item.get('audio_id') else 'draft')
        item.setdefault('updated_at', item.get('created_at', 0))
        return item

    def public(item):
        result = {k: copy.deepcopy(v) for k, v in item.items() if not k.startswith('_')}
        aid = result.get('audio_id')
        result['path'] = str(cache / (identifier(aid) + '.wav')) if aid else None
        if 'fragments' in result:
            result['fragments'] = [public(fragment) for fragment in result['fragments']]
        return result

    def put(item):
        atomic_json(records / (identifier(item['id']) + '.json'), item)

    def expect(item, params):
        if params.get('revision') is not None and params['revision'] != item['revision']:
            raise ValueError('条目已更新，请刷新后重试')

    def get(params):
        with records_lock:
            return public(load(params.get('id')))

    def listing(params):
        items = []
        with records_lock:
            for path in records.glob('*.json'):
                try:
                    item = public(load(path.stem))
                    if 'folder_id' not in params or item['folder_id'] == params['folder_id']:
                        items.append(item)
                except (OSError, ValueError):
                    continue
        return {'items': sorted(items, key=lambda x: x['updated_at'], reverse=True)}

    def folder_rows():
        if not folders_path.is_file():
            return []
        return json.loads(folders_path.read_text('utf-8')).get('folders', [])

    def check_folder(folder_id):
        if folder_id in (None, ''):
            return None
        if not isinstance(folder_id, str) or not re.fullmatch('[a-f0-9]{32}', folder_id):
            raise ValueError('无效的分类')
        if not any(row['id'] == folder_id for row in folder_rows()):
            raise ValueError('分类不存在')
        return folder_id

    def folder_list(_):
        with records_lock:
            counts = {}
            for item in listing({})['items']:
                counts[item['folder_id']] = counts.get(item['folder_id'], 0) + 1
            return {'folders': [{**row, 'item_count': counts.get(row['id'], 0)} for row in folder_rows()]}

    def folder_name(value):
        name = str(value or '').strip()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise ValueError('分类名称需为 1 到 80 个字符')
        return name

    def folder_save(params, create=False):
        with records_lock:
            rows = folder_rows()
            name = folder_name(params.get('name'))
            fid = uuid.uuid4().hex if create else check_folder(params.get('id'))
            if any(row['name'].casefold() == name.casefold() and row['id'] != fid for row in rows):
                raise ValueError('分类名称已存在')
            row = {'id': fid, 'name': name, 'created_at': time.time(), 'updated_at': time.time()} if create else next((row for row in rows if row['id'] == fid), None)
            if row is None:
                raise ValueError('分类不存在')
            row.update(name=name, updated_at=time.time())
            if create:
                rows.append(row)
            atomic_json(folders_path, {'folders': rows})
            app.emit('practice.changed', folder_id=fid)
            return {**row, 'item_count': next((f['item_count'] for f in folder_list({})['folders'] if f['id'] == fid), 0)}

    def folder_delete(params):
        with records_lock:
            fid = check_folder(params.get('id'))
            if fid is None:
                raise ValueError('未分类不能删除')
            moved = 0
            for entry in listing({'folder_id': fid})['items']:
                item = load(entry['id'])
                item.update(folder_id=None, revision=item['revision'] + 1, updated_at=time.time())
                put(item)
                moved += 1
            atomic_json(folders_path, {'folders': [row for row in folder_rows() if row['id'] != fid]})
            app.emit('practice.changed', folder_id=fid)
            return {'ok': True, 'id': fid, 'moved_count': moved}

    def voice_name(value):
        voice = str(value or 'Cherry').strip()
        if not voice or len(voice) > 100:
            raise ValueError('请输入有效声音名称')
        return voice

    def keep_version(item):
        if item.get('audio_id') and not item.get('versions'):
            item['versions'] = [{k: copy.deepcopy(item.get(k)) for k in ('text', 'voice', 'model', 'audio_id', 'sentences', 'generated_at', 'revision')}]

    def invalidate(item):
        item.pop('_generation', None)
        if item.get('status') == 'generating':
            item['status'] = 'stale' if item.get('stale') else 'ready' if item.get('audio_id') else 'draft'
        for fragment in item.get('fragments', []):
            fragment.pop('_generation', None)
            if fragment.get('status') == 'generating':
                fragment['status'] = 'stale' if fragment.get('stale') else 'ready' if fragment.get('audio_id') else 'draft'

    def save(params):
        with records_lock:
            existing = params.get('id')
            item = load(existing) if existing else {'id': new_id(), 'revision': 1, 'content_revision': 1, 'created_at': time.time(), 'text': '', 'voice': 'Cherry', 'folder_id': None, 'sentences': [], 'fragments': [], 'versions': [], 'stale': False, 'status': 'draft'}
            expect(item, params)
            text = '\n'.join(split_sentences(params.get('text', item['text'])))
            voice = voice_name(params.get('voice', item['voice']))
            folder_id = check_folder(params.get('folder_id', item['folder_id']))
            title = str(params.get('title', item.get('title') or text.split('\n', 1)[0][:80])).strip()
            if not title or len(title) > 120:
                raise ValueError('标题需为 1 到 120 个字符')
            changed = text != item['text'] or voice != item['voice']
            if existing and not changed and folder_id == item['folder_id'] and title == item.get('title'):
                return public(item)
            keep_version(item)
            if changed:
                item['stale'] = bool(item.get('audio_id'))
                item['status'] = 'stale' if item['stale'] else 'draft'
                for fragment in item['fragments']:
                    keep_version(fragment)
                    fragment.update(stale=True, status='stale')
                invalidate(item)
                if existing:
                    item['content_revision'] += 1
            item.update(text=text, voice=voice, title=title, folder_id=folder_id, updated_at=time.time())
            if existing:
                item['revision'] += 1
            put(item)
            app.emit('practice.changed', id=item['id'])
            return public(item)

    def soft_delete(params):
        with records_lock:
            item = load(params.get('id'))
            expect(item, params)
            fid = params.get('fragment_id')
            target = next((f for f in item['fragments'] if f['id'] == identifier(fid)), None) if fid else item
            if target is None:
                raise ValueError('片段不存在')
            archive.mkdir(exist_ok=True)
            path = archive / (new_id() + '.json')
            atomic_json(path, {'deleted_at': time.time(), 'parent_id': item['id'] if fid else None, 'kind': 'fragment' if fid else 'item', 'record': target})
            if fid:
                item['fragments'] = [f for f in item['fragments'] if f['id'] != fid]
                item.update(revision=item['revision'] + 1, updated_at=time.time())
                put(item)
            else:
                (records / (item['id'] + '.json')).unlink()
            app.emit('practice.changed', id=item['id'])
            return {'ok': True, 'id': item['id'], 'fragment_id': fid, 'archive_path': str(path)}

    def audio(params):
        aid = identifier(params.get('id'))
        # Older players request an item ID. New players use explicit audio_id.
        if (records / (aid + '.json')).is_file():
            with records_lock:
                aid = identifier(load(aid).get('audio_id'))
        path = cache / (aid + '.wav')
        if not path.is_file() or path.stat().st_size > 40 * 1024 * 1024:
            raise ValueError('音频不存在或过大，请重新生成或按句播放')
        return {'mime': 'audio/wav', 'data': base64.b64encode(path.read_bytes()).decode()}

    def model_spec(voice):
        values = app.settings.get()
        if values.get('preferences', {}).get('model_mode') == 'local':
            raise ValueError('生成口语练习请在设置中选择 API 模式；已保存练习仍可离线播放')
        role = values['roles'].get('tts', {})
        provider = next((p for p in values['providers'] if p['id'] == role.get('provider_id')), None)
        if not provider or not role.get('model'):
            raise ValueError('请在设置中配置语音合成模型')
        spec = {k: provider.get(k) for k in ('id', 'kind', 'base_url', 'native_url', 'region')}
        spec.update(model=role['model'], voice=voice, schema=1)
        return spec, copy.deepcopy(role), copy.deepcopy(provider)

    def submit(params, fragment=False):
        if not isinstance(params.get('force', False), bool):
            raise ValueError('重新生成选项无效')
        with records_lock:
            if fragment:
                item = load(params.get('id'))
                expect(item, params)
                text = '\n'.join(split_sentences(params.get('text')))
                def selection_text(value):
                    value = unicodedata.normalize('NFKC', value).translate(str.maketrans({'’': "'", '‘': "'", 'ʼ': "'", '‐': '-', '‑': '-', '–': '-'}))
                    return ' '.join(value.split()).casefold()
                if selection_text(text) not in selection_text(item['text']):
                    raise ValueError('选中片段不在当前段落中，请重新选择')
                voice = voice_name(params.get('voice', item['voice']))
                fid = params.get('fragment_id')
                selected = next((f for f in item['fragments'] if f['id'] == identifier(fid)), None) if fid else next((f for f in item['fragments'] if f['text'] == text and f['voice'] == voice), None)
                if fid and selected is None:
                    raise ValueError('片段不存在')
                if selected is None:
                    selected = {'id': new_id(), 'parent_id': item['id'], 'text': text, 'voice': voice, 'created_at': time.time(), 'revision': 1, 'sentences': [], 'versions': [], 'stale': False}
                    item['fragments'].append(selected)
                else:
                    keep_version(selected)
                    if text != selected['text'] or voice != selected['voice']:
                        selected['stale'] = bool(selected.get('audio_id'))
                    selected.update(text=text, voice=voice, revision=selected['revision'] + 1)
                item['revision'] += 1
                selected.update(fragment_id=selected['id'], parent_revision=item['content_revision'], status='generating', updated_at=time.time())
                target = selected
            else:
                saved = save(params)
                item = load(saved['id'])
                target = item
            token = new_id()
            target['_generation'] = token
            target['status'] = 'generating'
            target.pop('error', None)
            item['updated_at'] = time.time()
            put(item)
            request = {'id': item['id'], 'fragment_id': target['id'] if fragment else None,
                       'revision': item['revision'], 'content_revision': item['content_revision'], 'token': token, 'text': target['text'], 'voice': target['voice'],
                       'force_nonce': new_id() if params.get('force') else None}
            job = app.jobs.submit('practice.fragment' if fragment else 'practice.generate', request)
            app.emit('practice.changed', id=item['id'])
            return job

    def alive(request):
        with records_lock:
            try:
                item = load(request['id'])
            except ValueError as exc:
                raise Cancelled('条目已删除，生成结果未写回') from exc
            target = next((f for f in item['fragments'] if f['id'] == request['fragment_id']), None) if request.get('fragment_id') else item
            if item['content_revision'] != request['content_revision'] or target is None or target.get('_generation') != request['token']:
                raise Cancelled('条目已编辑或片段已删除，生成结果未写回')
            return item, target

    def generate(job):
        request = job.params
        sentences = split_sentences(request.get('text'))
        voice = voice_name(request.get('voice'))
        while not generation_lock.acquire(timeout=.1):
            job.check_cancelled()
        try:
            _, previous = alive(request)
            spec, role, provider = model_spec(voice)
            preferred = {s['text']: identifier(s['audio_id']) for s in previous.get('sentences', [])} if previous.get('_audio_spec') == digest(spec) and not request.get('force_nonce') else {}
            output, reused = [], 0
            for i, sentence in enumerate(sentences):
                job.check_cancelled()
                alive(request)
                fingerprint = {'text': sentence, 'spec': spec}
                if request.get('force_nonce'):
                    fingerprint['force'] = request['force_nonce']
                aid = preferred.get(sentence) or digest(fingerprint)
                target, meta_path = cache / (aid + '.wav'), cache / (aid + '.json')
                try:
                    meta = json.loads(meta_path.read_text('utf-8'))
                except (OSError, ValueError):
                    meta = {}
                if valid_audio(target, meta):
                    reused += 1
                else:
                    current = app.settings.get()
                    current_provider = next((p for p in current['providers'] if p['id'] == provider['id']), {})
                    if current['roles'].get('tts') != role or any(current_provider.get(k) != provider.get(k) for k in ('kind', 'base_url', 'native_url', 'region')):
                        raise ValueError('生成期间模型配置已变化，请重新提交')
                    raw = cache / (aid + '.raw')
                    if not raw.is_file() or not raw.stat().st_size:
                        incoming = job.work_dir / ('tts-' + uuid.uuid4().hex + '.bin')
                        app.providers.tts(sentence, incoming, voice=voice)
                        if not incoming.is_file() or not incoming.stat().st_size:
                            raise ValueError('语音服务未返回音频')
                        # Retain an already paid provider response before checking
                        # cancellation; a later valid request can finish this cache.
                        incoming.replace(raw)
                    job.check_cancelled()
                    alive(request)
                    # Publish only a verified WAV, never a partial provider response.
                    temp = job.work_dir / (aid + '.wav')
                    job.run_process([binary('ffmpeg'), '-hide_banner', '-nostdin', '-y', '-loglevel', 'error',
                                     '-i', str(raw), '-vn', '-ac', '1', '-ar', '24000', '-c:a', 'pcm_s16le', str(temp)])
                    content = temp.read_bytes()
                    meta = {'sha256': hashlib.sha256(content).hexdigest()}
                    if not valid_audio(temp, meta):
                        raise ValueError('语音服务未返回有效音频，未保存缓存')
                    temp.replace(target)
                    atomic_json(meta_path, meta)
                    raw.unlink(missing_ok=True)
                with wave.open(str(target), 'rb') as wav:
                    duration = wav.getnframes() / wav.getframerate()
                output.append({'text': sentence, 'audio_id': aid, 'duration': duration})
                job.progress((i + 1) / len(sentences) * 95, f'{i + 1}/{len(sentences)} 句 · 复用 {reused} 句')
            audio_id = digest({'kind': 'practice-full', 'sentences': [s['audio_id'] for s in output], 'schema': 2})
            full_path = cache / (audio_id + '.wav')
            temp = job.work_dir / (audio_id + '-full.wav')
            with wave.open(str(temp), 'wb') as merged:
                merged.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                for sentence in output:
                    job.check_cancelled()
                    with wave.open(str(cache / (sentence['audio_id'] + '.wav')), 'rb') as src:
                        merged.writeframes(src.readframes(src.getnframes()))
            temp.replace(full_path)
            job.check_cancelled()
            with records_lock:
                item, target = alive(request)
                keep_version(target)
                target.update(text='\n'.join(sentences), voice=voice, model=role['model'], audio_id=audio_id,
                              sentences=output, reused=reused, stale=False, status='ready', generated_at=time.time(), updated_at=time.time())
                target['_audio_spec'] = digest(spec)
                target.pop('_generation', None)
                target.pop('error', None)
                item['revision'] += 1
                if target is not item:
                    target['revision'] += 1
                    target['parent_revision'] = item['content_revision']
                target.setdefault('versions', []).append({k: copy.deepcopy(target.get(k)) for k in ('text', 'voice', 'model', 'audio_id', 'sentences', 'generated_at', 'revision')})
                item['updated_at'] = time.time()
                put(item)
                result = public(target)
            snapshot = job.work_dir / 'practice-result.json'
            atomic_json(snapshot, result)
            job.artifact(full_path, 'audio', '完整练习音频.wav')
            job.artifact(snapshot, 'json', '练习文本与句子')
            app.emit('practice.changed', id=item['id'])
            return result
        except Exception as exc:
            with records_lock:
                try:
                    item, target = alive(request)
                except Cancelled:
                    pass
                else:
                    target.update(status='error', error=str(exc), updated_at=time.time())
                    put(item)
                    app.emit('practice.changed', id=item['id'])
            raise
        finally:
            generation_lock.release()

    def word(params):
        from ..pronunciation import lookup
        return lookup(str(params.get('word') or ''))

    app.jobs.register('practice.generate', generate)
    app.jobs.register('practice.fragment', generate)
    app.register('practice.generate', lambda p: submit(p))
    app.register('practice.fragment', lambda p: submit(p, fragment=True))
    app.register('practice.save', save)
    app.register('practice.move', lambda p: save({'id': p.get('id'), 'folder_id': p.get('folder_id'), **({'revision': p['revision']} if 'revision' in p else {})}))
    app.register('practice.delete', soft_delete)
    app.register('practice.folders', folder_list)
    app.register('practice.folders.list', folder_list)
    app.register('practice.folders.create', lambda p: folder_save(p, create=True))
    app.register('practice.folders.rename', folder_save)
    app.register('practice.folders.delete', folder_delete)
    app.register('practice.list', listing)
    app.register('practice.get', get)
    app.register('practice.audio', audio)
    app.register('practice.word', word)
