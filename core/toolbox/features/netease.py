"""NetEase library access and local conversion; upstream ncmdump owns decoding."""
import base64
import csv
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import string
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..jobs import Cancelled
from ..media import binary, ffmpeg, probe
from ..settings import protect
from ._common import write_json

PUBLIC_KEY = b'''-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDgtQn2JZ34ZC28NWYpAUd98iZ37BUrX/aKzmFbt7clFSs6sXqHauqKWqdtLkF2KexO40H1YTX8z2lSgBBOAxLsvaklV8k4cBFK9snQXE9/DDaFt6Rr7iVZMldczhC0JNgTz+SHXT6CBHuX3e9SdB1Ua44oncaTWz7OBGLbCiK45wIDAQAB
-----END PUBLIC KEY-----'''


def aes_cbc(raw, key):
    pad = padding.PKCS7(128).padder()
    raw = pad.update(raw) + pad.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(b'0102030405060708')).encryptor()
    return base64.b64encode(enc.update(raw) + enc.finalize())


def weapi(data):
    key = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16)).encode()
    public = serialization.load_pem_public_key(PUBLIC_KEY).public_numbers()
    raw = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
    return {'params': aes_cbc(aes_cbc(raw, b'0CoJUm6Qyw8W8jud'), key).decode(),
            'encSecKey': format(pow(int.from_bytes(key[::-1], 'big'), public.e, public.n), '0256x')}


def client_root():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'Netease/CloudMusic'


def local_uid():
    try:
        value = (client_root() / 'info').read_text('utf-8').strip()
        return int(value) if value.isdigit() else None
    except OSError:
        return None


def converter_binary():
    candidates = [os.environ.get('WINTOOLBOX_NCMDUMP'), shutil.which('ncmdump')]
    if os.environ.get('WINTOOLBOX_FFMPEG'):
        candidates.append(str(Path(os.environ['WINTOOLBOX_FFMPEG']).parent / 'ncmdump/ncmdump.exe'))
    candidates.append(str(Path(__file__).resolve().parents[3] / 'desktop/src-tauri/resources/tools/ncmdump/ncmdump.exe'))
    for path in candidates:
        if path and Path(path).is_file():
            return str(Path(path).resolve())
    raise ValueError('缺少 ncmdump，请更新工具箱媒体运行包，或设置 WINTOOLBOX_NCMDUMP。')


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:140] or '音乐'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value, re.I):
        value = '_' + value
    return value


def publish(source, folder, stem):
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(10000):
        target = folder / (safe_name(stem) + (f' ({index})' if index else '') + source.suffix.lower())
        try:
            dest = target.open('xb')
        except FileExistsError:
            continue
        try:
            with dest, source.open('rb') as src:
                shutil.copyfileobj(src, dest)
            return target
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    raise ValueError('同名输出过多，请更换输出目录。')


def output_format(params):
    value = params.get('format', 'mp3')
    if value not in ('mp3', 'original'):
        raise ValueError('格式只能是 mp3 或 original')
    bitrate = int(params.get('bitrate', 320))
    if bitrate not in (128, 192, 256, 320):
        raise ValueError('MP3 码率只能是 128、192、256、320 kbps')
    return value, bitrate


def to_mp3(job, source, bitrate, metadata=None):
    target = source.with_name(source.stem + '-converted.mp3')
    args = ['-i', str(source), '-map', '0:a:0', '-map', '0:v?', '-map_metadata', '0']
    # Keep existing MP3 frames without introducing a second lossy encode.
    args += ['-c:a', 'copy'] if source.suffix.lower() == '.mp3' else ['-c:a', 'libmp3lame', '-b:a', f'{bitrate}k']
    args += ['-c:v', 'copy', '-id3v2_version', '3']
    for key, value in (metadata or {}).items():
        args += ['-metadata', f'{key}={value}']
    ffmpeg(job, args + [str(target)])
    return target


def convert(job):
    fmt, bitrate = output_format(job.params)
    paths = []
    for raw in job.params.get('paths', []):
        path = Path(raw).resolve()
        if path.is_dir():
            paths.extend(sorted(p for p in path.rglob('*') if p.is_file() and p.suffix.lower() == '.ncm'))
        elif path.is_file() and path.suffix.lower() == '.ncm':
            paths.append(path)
        else:
            raise ValueError('请选择 NCM 文件或包含 NCM 的文件夹：' + str(path))
    paths = list(dict.fromkeys(paths))
    if not paths:
        raise ValueError('没有找到 NCM 文件。')
    if not job.params.get('output_dir'):
        raise ValueError('请选择输出目录。')
    exe = converter_binary()
    results = []
    for index, source in enumerate(paths):
        job.progress(index * 95 / len(paths), f'转换 {index + 1}/{len(paths)}：{source.name}')
        folder = job.work_dir / f'convert-{index}'
        folder.mkdir(parents=True, exist_ok=True)
        # Separate staging prevents ncmdump from overwriting source-adjacent audio.
        try:
            job.run_process([exe, str(source), '-o', str(folder)], timeout=300)
            candidates = [p for p in folder.iterdir() if p.suffix.lower() in ('.mp3', '.flac', '.m4a', '.ogg', '.wav') and p.stat().st_size]
            if len(candidates) != 1:
                raise ValueError('ncmdump 未产生唯一有效音频；文件可能损坏或不受支持。')
            audio = candidates[0]
            info = probe(audio)
            if not info['has_audio'] or info['duration'] <= 0:
                raise ValueError('解密结果没有有效音频。')
            if fmt == 'mp3' and audio.suffix.lower() != '.mp3':
                audio = to_mp3(job, audio, bitrate)
            job.check_cancelled()
            target = publish(audio, job.params['output_dir'], source.stem)
            job.artifact(target, 'audio')
            results.append({'source': str(source), 'path': str(target), 'status': 'ok', 'duration': info['duration']})
        except Cancelled:
            raise
        except Exception as exc:
            results.append({'source': str(source), 'status': 'failed', 'error': str(exc)[:500]})
    report = job.work_dir / 'conversion-report.json'
    write_json(report, {'results': results})
    job.artifact(report, label='逐文件转换报告')
    failed = sum(x['status'] == 'failed' for x in results)
    job.progress(100, f'成功 {len(results) - failed}，失败 {failed}；原 NCM 已保留。')
    if failed:
        raise ValueError(f'{failed} 个文件转换失败，已完成的音频和逐文件报告可在结果文件中查看。')
    return {'results': results}


class NetEase:
    def __init__(self, app):
        self.app = app
        self.root = app.data_dir / 'netease'
        self.auth_path = self.root / 'auth.dpapi.json'
        self.lock = threading.RLock()
        self.qr_keys = {}
        self.qr_cookie = ''
        qr_path = self.root / 'qr-session.json'
        if qr_path.is_file():
            try:
                session = json.loads(qr_path.read_text('utf-8'))
                self.qr_keys = {session['key']: session['created_at']}
                if session.get('cookie'):
                    self.qr_cookie = protect(session['cookie'], decrypt=True)
            except (OSError, ValueError, KeyError):
                pass

    def cookie(self):
        if not self.auth_path.is_file():
            return ''
        try:
            return protect(json.loads(self.auth_path.read_text('utf-8'))['cookie'], decrypt=True)
        except Exception:
            raise ValueError('网易云登录凭据无法读取，请重新扫码登录。') from None

    def request(self, path, data, *, cookie=None, allowed=(200,)):
        cookie = self.cookie() if cookie is None else cookie
        csrf = dict(part.strip().split('=', 1) for part in cookie.split(';') if '=' in part).get('__csrf', '')
        payload = dict(data, csrf_token=csrf)
        headers = {'Referer': 'https://music.163.com/', 'User-Agent': 'Mozilla/5.0', 'Cookie': cookie}
        try:
            with httpx.Client(timeout=25, follow_redirects=False) as client:
                response = client.post('https://music.163.com/weapi/' + path, data=weapi(payload), headers=headers)
                response.raise_for_status()
                body = response.json()
                cookies = {c.name: c.value for c in response.cookies.jar}
        except (httpx.HTTPError, ValueError):
            raise ValueError('网易云接口请求失败，请检查网络并重试。') from None
        code = body.get('code')
        if code not in allowed:
            if code in (301, 302):
                raise ValueError('网易云登录已失效，请重新扫码登录。' if cookie else '此操作需要网易云登录，请扫码登录。')
            raise ValueError(f'网易云接口返回 {code}；请检查账号权限或稍后重试。')
        return body, cookies

    def account(self, cookie=None):
        body, _ = self.request('nuser/account/get', {}, cookie=cookie)
        profile = body.get('profile') or {}
        if not profile.get('userId'):
            raise ValueError('尚未登录网易云音乐，请扫码登录。')
        return {'uid': profile['userId'], 'nickname': profile.get('nickname', ''), 'authenticated': True}

    def status(self, params=None):
        account, message = None, ''
        if self.auth_path.is_file():
            try:
                account = self.account()
            except ValueError as exc:
                message = str(exc)
        return {'account': account, 'local_uid': local_uid(), 'authenticated': bool(account),
                'message': message, 'client_root': str(client_root()),
                'ncmdump_available': self.converter_available()}

    @staticmethod
    def converter_available():
        try:
            converter_binary()
            return True
        except ValueError:
            return False

    def qr_start(self, params):
        import qrcode
        import qrcode.image.svg
        initial_cookie = 'os=pc; appver=3.1.18; __remember_me=true; deviceId=' + secrets.token_hex(16)
        body, cookies = self.request('login/qrcode/unikey', {'type': 3}, cookie=initial_cookie)
        key = body.get('unikey')
        if not key:
            raise ValueError('网易云没有返回二维码，请稍后重试。')
        url = 'https://music.163.com/login?codekey=' + key
        image = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
        svg = io.BytesIO()
        image.save(svg)
        with self.lock:
            self.qr_keys = {key: time.time()}
            self.qr_cookie = initial_cookie + '; ' + '; '.join(f'{name}={value}' for name, value in cookies.items())
            write_json(self.root / 'qr-session.json', {'key': key, 'created_at': self.qr_keys[key], 'cookie': protect(self.qr_cookie)})
        return {'key': key, 'url': url, 'qr_image': 'data:image/svg+xml;base64,' + base64.b64encode(svg.getvalue()).decode(), 'expires_seconds': 180}

    def qr_check(self, params):
        key = params.get('key', '')
        with self.lock:
            started = self.qr_keys.get(key)
        if not started or time.time() - started > 180:
            return {'code': 800, 'message': '二维码已过期，请重新生成。'}
        body, cookies = self.request('login/qrcode/client/login', {'key': key, 'type': 3}, cookie=self.qr_cookie, allowed=(800, 801, 802, 803, 8821))
        code = body['code']
        if code == 8821:
            return {'code': code, 'message': '网易云要求使用其他登录方式或完成安全验证（8821）。请先在网易云官方网页正常登录，再导入 Cookie 文件。'}
        if code == 803:
            if not cookies.get('MUSIC_U'):
                raise ValueError('扫码已确认但未返回登录凭据，请重试。')
            cookie = self.qr_cookie + '; ' + '; '.join(f'{name}={value}' for name, value in cookies.items())
            account = self.account(cookie)
            write_json(self.auth_path, {'cookie': protect(cookie), 'uid': account['uid']})
            with self.lock:
                self.qr_keys.pop(key, None)
                (self.root / 'qr-session.json').unlink(missing_ok=True)
            return {'code': code, 'account': account, 'message': '已登录'}
        return {'code': code, 'message': {800: '二维码已过期', 801: '等待扫码', 802: '请在手机上确认登录'}[code]}

    def import_cookie(self, params):
        # File paths only: credentials do not enter persisted job params or CLI history.
        cookie = Path(params['path']).read_text('utf-8').strip()
        if '\n' in cookie or '\r' in cookie or 'MUSIC_U=' not in cookie:
            raise ValueError('Cookie 文件需要单行 Cookie 请求头，且包含 MUSIC_U。')
        account = self.account(cookie)
        write_json(self.auth_path, {'cookie': protect(cookie), 'uid': account['uid']})
        return account

    def disconnect(self, params):
        self.auth_path.unlink(missing_ok=True)
        return {'disconnected': True}

    def uid(self, params):
        if params.get('uid'):
            return int(params['uid'])
        if self.auth_path.is_file():
            return self.account()['uid']
        if local_uid():
            return local_uid()
        raise ValueError('请选择用户 ID 或先扫码登录。')

    def playlists(self, params):
        uid = self.uid(params)
        items = []
        for offset in range(0, 100000, 100):
            body, _ = self.request('user/playlist', {'uid': uid, 'limit': 100, 'offset': offset})
            page = body.get('playlist', [])
            items.extend({'id': x['id'], 'name': x['name'], 'track_count': x.get('trackCount', 0),
                          'creator_uid': x.get('creator', {}).get('userId'), 'special_type': x.get('specialType', 0)} for x in page)
            if not body.get('more') or not page:
                return {'uid': uid, 'playlists': items, 'source': 'online'}
        raise ValueError('歌单分页超出上限，结果未保存。')

    def likes(self, params):
        value = self.playlists(params)
        playlist = next((p for p in value['playlists'] if p['creator_uid'] == value['uid'] and p['special_type'] == 5), None)
        if not playlist:
            raise ValueError('没有找到该用户的“我喜欢的音乐”；请扫码后重试。')
        return self.tracks(dict(params, id=playlist['id']))

    def tracks(self, params):
        body, _ = self.request('v6/playlist/detail', {'id': int(params['id']), 'n': 100000, 's': 0})
        playlist = body['playlist']
        ids = [int(x['id']) for x in playlist.get('trackIds', [])]
        details = {x['id']: x for x in playlist.get('tracks', [])}
        missing = [id for id in ids if id not in details]
        for offset in range(0, len(missing), 200):
            chunk = missing[offset:offset + 200]
            value, _ = self.request('v3/song/detail', {'c': json.dumps([{'id': id} for id in chunk])})
            details.update({x['id']: x for x in value.get('songs', [])})
        tracks = []
        for id in ids:
            x = details.get(id, {})
            tracks.append({'id': id, 'name': x.get('name', ''), 'artists': [a['name'] for a in x.get('ar', [])],
                           'album': x.get('al', {}).get('name', ''), 'duration_ms': x.get('dt', 0), 'resolved': bool(x)})
        return {'id': playlist['id'], 'name': playlist['name'], 'track_count': playlist.get('trackCount', len(ids)),
                'tracks': tracks, 'source': 'online', 'fetched_at': time.time(),
                'complete': len(ids) == playlist.get('trackCount', len(ids)) and all(x['resolved'] for x in tracks)}

    def export(self, job):
        params = job.params
        job.progress(5, '读取网易云歌单及歌曲详情')
        if params.get('kind', 'likes') == 'playlists':
            value = self.playlists(params)
            rows = value['playlists']
        else:
            value = self.likes(params) if params.get('kind', 'likes') == 'likes' else self.tracks(params)
            rows = value['tracks']
        folder = Path(params.get('output_dir') or job.work_dir).resolve()
        json_path = job.work_dir / 'netease-library.json'
        write_json(json_path, value)
        csv_path = job.work_dir / 'netease-library.csv'
        with csv_path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ['id', 'name'])
            writer.writeheader()
            writer.writerows({k: ' / '.join(v) if isinstance(v, list) else v for k, v in row.items()} for row in rows)
        for path in [json_path, csv_path]:
            target = path if folder == job.work_dir.resolve() else publish(path, folder, '网易云-' + params.get('kind', 'likes'))
            job.artifact(target, label=target.name)
        job.progress(100, f'已导出 {len(rows)} 条记录' + ('；部分歌曲详情缺失，请检查 JSON complete 字段。' if value.get('complete') is False else ''))
        return value

    def download(self, job):
        fmt, bitrate = output_format(job.params)
        ids = list(dict.fromkeys(int(id) for id in job.params.get('ids', [])))
        if not ids or len(ids) > 5000 or not job.params.get('output_dir'):
            raise ValueError('请指定 1–5000 个歌曲 ID 和输出目录。')
        results = []
        for index, id in enumerate(ids):
            job.progress(index * 95 / len(ids), f'下载 {index + 1}/{len(ids)}：{id}')
            name = str(id)
            try:
                # Only the service's download endpoint decides the account's entitlement.
                value, _ = self.request('song/enhance/download/url', {'id': id, 'br': 999000 if fmt == 'original' else bitrate * 1000})
                item = value.get('data') or {}
                if not item.get('url') or item.get('code', 200) != 200 or item.get('freeTrialInfo'):
                    results.append({'id': id, 'status': 'unavailable', 'reason': '账号无下载权限、试听或歌曲不可用', 'code': item.get('code')})
                    continue
                detail, _ = self.request('v3/song/detail', {'c': json.dumps([{'id': id}])})
                song = next(iter(detail.get('songs', [])), {})
                name = song.get('name') or str(id)
                artists = ' / '.join(x['name'] for x in song.get('ar', []))
                url = item['url']
                validate_audio_url(url)
                ext = str(item.get('type', 'mp3')).lower()
                if ext not in ('mp3', 'flac', 'm4a', 'aac', 'ogg', 'wav'):
                    raise ValueError('下载接口返回不支持的音频格式。')
                source = job.work_dir / f'{id}.{ext}'
                with httpx.Client(timeout=45, follow_redirects=False) as client:
                    # Do not forward account cookies to CDN hosts or redirects.
                    for _ in range(6):
                        with client.stream('GET', url) as response:
                            if response.is_redirect:
                                url = str(response.url.join(response.headers['location']))
                                validate_audio_url(url)
                                continue
                            response.raise_for_status()
                            received = 0
                            with source.open('wb') as handle:
                                for block in response.iter_bytes(1024 * 256):
                                    job.check_cancelled()
                                    received += len(block)
                                    if received > 512 * 1024**2:
                                        raise ValueError('单曲下载超出 512 MiB 限制')
                                    handle.write(block)
                            expected = response.headers.get('content-length')
                            if expected and received != int(expected):
                                raise ValueError('音频下载不完整')
                            break
                    else:
                        raise ValueError('下载重定向过多')
                info = probe(source)
                if not info['has_audio'] or info['duration'] <= 0:
                    raise ValueError('下载结果不是有效音频')
                expected_ms = song.get('dt', 0)
                if expected_ms and info['duration'] * 1000 < expected_ms - 5000:
                    raise ValueError('下载结果疑似试听片段，未发布为完整歌曲')
                if fmt == 'mp3':
                    source = to_mp3(job, source, bitrate, {'title': name, 'artist': artists, 'album': song.get('al', {}).get('name', '')})
                job.check_cancelled()
                target = publish(source, job.params['output_dir'], f'{artists} - {name} [{id}]')
                job.artifact(target, 'audio')
                results.append({'id': id, 'name': name, 'status': 'ok', 'path': str(target), 'duration': info['duration']})
            except Cancelled:
                raise
            except Exception as exc:
                results.append({'id': id, 'name': name, 'status': 'failed', 'reason': str(exc) if isinstance(exc, ValueError) else '下载或音频验证失败，请检查网络与权限后重试。'})
            job.cancel_event.wait(.3)
        report = job.work_dir / 'download-report.json'
        write_json(report, {'results': results})
        job.artifact(report, label='逐歌曲下载报告')
        ok = sum(x['status'] == 'ok' for x in results)
        job.progress(100, f'下载成功 {ok}，不可用或失败 {len(ids) - ok}。')
        if ok != len(ids):
            raise ValueError(f'下载成功 {ok}/{len(ids)}；其余歌曲不可用或失败，请查看逐歌曲下载报告。')
        return {'results': results}


def validate_audio_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.port not in (None, 80, 443) or not any(host == domain or host.endswith('.' + domain) for domain in ('music.126.net', 'music.163.com')):
        raise ValueError('下载接口返回非网易云音频地址，已停止。')


def register(app):
    service = NetEase(app)
    app.netease = service
    for name, handler in [('status', service.status), ('qr.start', service.qr_start), ('qr.check', service.qr_check),
                          ('cookie.import', service.import_cookie), ('disconnect', service.disconnect),
                          ('playlists', service.playlists), ('likes', service.likes), ('tracks', service.tracks)]:
        app.register('netease.' + name, handler)
    for name, runner in [('convert', convert), ('export', service.export), ('download', service.download)]:
        tool = 'netease.' + name
        app.jobs.register(tool, runner)
        app.register(tool, lambda p, tool=tool: app.jobs.submit(tool, p))
