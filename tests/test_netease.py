import base64
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from toolbox.features import netease as n
from toolbox.netease_cli import CommandJob


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(n, 'local_uid', lambda: None)
    return n.NetEase(SimpleNamespace(data_dir=tmp_path))


def test_weapi_roundtrip_and_random_secret():
    # Known key injection verifies the double AES payload independently.
    from unittest.mock import patch
    with patch.object(n.secrets, 'choice', return_value='a'):
        value = n.weapi({'uid': 42, 'name': '喜欢'})
    raw = value['params'].encode()
    for key in [b'a' * 16, b'0CoJUm6Qyw8W8jud']:
        dec = Cipher(algorithms.AES(key), modes.CBC(b'0102030405060708')).decryptor()
        data = dec.update(base64.b64decode(raw)) + dec.finalize()
        pad = padding.PKCS7(128).unpadder()
        raw = pad.update(data) + pad.finalize()
    assert json.loads(raw) == {'uid': 42, 'name': '喜欢'}
    assert len(value['encSecKey']) == 256
    assert n.weapi({'uid': 42}) != n.weapi({'uid': 42})


def test_playlist_pagination_and_likes_identity(service, monkeypatch):
    calls = []
    def request(path, data):
        calls.append(data)
        if data['offset'] == 0:
            return {'more': True, 'playlist': [{'id': 1, 'name': '我喜欢的音乐', 'specialType': 5, 'creator': {'userId': 99}}]}, {}
        return {'more': False, 'playlist': [{'id': 2, 'name': 'different name', 'specialType': 5, 'creator': {'userId': 42}}]}, {}
    monkeypatch.setattr(service, 'request', request)
    monkeypatch.setattr(service, 'tracks', lambda p: p)
    assert service.likes({'uid': 42})['id'] == 2
    assert [c['offset'] for c in calls] == [0, 100]


def test_tracks_resolve_batches_preserve_order_and_missing(service, monkeypatch):
    def request(path, data):
        if path == 'v6/playlist/detail':
            return {'playlist': {'id': 1, 'name': 'P', 'trackCount': 4, 'trackIds': [{'id': i} for i in [3, 1, 2]], 'tracks': []}}, {}
        return {'songs': [{'id': 1, 'name': 'one', 'ar': [{'name': 'A'}]}, {'id': 3, 'name': 'three'}]}, {}
    monkeypatch.setattr(service, 'request', request)
    value = service.tracks({'id': 1})
    assert [x['id'] for x in value['tracks']] == [3, 1, 2]
    assert value['tracks'][2]['resolved'] is False
    assert value['complete'] is False


def test_qr_survives_cli_process_and_stores_no_cookie_in_result(service, monkeypatch):
    monkeypatch.setattr(n, 'protect', lambda value, decrypt=False: value if decrypt else value)
    def generate(path, data, **kwargs):
        assert data['type'] == 3
        return {'unikey': 'key'}, {'NMTID': 'session-id'}
    monkeypatch.setattr(service, 'request', generate)
    qr = service.qr_start({})
    other = n.NetEase(service.app)
    def check(path, data, **kwargs):
        assert data['type'] == 3
        assert 'NMTID=session-id' in kwargs['cookie']
        return {'code': 803}, {'MUSIC_U': 'private-token'}
    monkeypatch.setattr(other, 'request', check)
    monkeypatch.setattr(other, 'account', lambda cookie: {'uid': 42, 'nickname': 'N', 'authenticated': True})
    monkeypatch.setattr(n, 'protect', lambda cookie: 'encrypted')
    result = other.qr_check({'key': qr['key']})
    assert result['code'] == 803
    assert 'private-token' not in json.dumps(result)
    assert json.loads(other.auth_path.read_text('utf-8'))['cookie'] == 'encrypted'
    assert not (other.root / 'qr-session.json').exists()


def test_qr_security_challenge_is_not_reported_as_login(service, monkeypatch):
    service.qr_keys = {'key': n.time.time()}
    monkeypatch.setattr(service, 'request', lambda *a, **kw: ({'code': 8821}, {}))
    result = service.qr_check({'key': 'key'})
    assert result['code'] == 8821
    assert 'account' not in result and not service.auth_path.exists()


def test_failed_cookie_validation_preserves_existing_auth(service, tmp_path, monkeypatch):
    service.root.mkdir()
    service.auth_path.write_text('original', 'utf-8')
    cookie = tmp_path / 'cookie.txt'; cookie.write_text('MUSIC_U=invalid', 'utf-8')
    monkeypatch.setattr(service, 'account', lambda *a: (_ for _ in ()).throw(ValueError('invalid')))
    with pytest.raises(ValueError):
        service.import_cookie({'path': str(cookie)})
    assert service.auth_path.read_text('utf-8') == 'original'


@pytest.mark.parametrize('url', ['http://127.0.0.1/x', 'file:///music.mp3', 'https://music.126.net.evil.test/x', 'https://u:p@music.126.net/x', 'https://music.126.net:8000/x'])
def test_download_host_restriction(url):
    with pytest.raises(ValueError):
        n.validate_audio_url(url)


def test_download_failure_keeps_report_and_does_not_fall_back(service, tmp_path, monkeypatch):
    calls = []
    def request(path, data):
        calls.append(path)
        if data['id'] == 1:
            raise ValueError('需要登录')
        return {'data': {'code': 403, 'url': None}}, {}
    monkeypatch.setattr(service, 'request', request)
    job = CommandJob(tmp_path, {'ids': [1, 2], 'output_dir': str(tmp_path / 'out')})
    with pytest.raises(ValueError, match='0/2'):
        service.download(job)
    report = json.loads((job.work_dir / 'download-report.json').read_text('utf-8'))
    assert [x['status'] for x in report['results']] == ['failed', 'unavailable']
    assert calls == ['song/enhance/download/url'] * 2
    assert len(job.artifacts) == 1


def test_same_name_outputs_are_preserved(tmp_path):
    source = tmp_path / 's.mp3'; source.write_bytes(b'audio')
    folder = tmp_path / 'out'; folder.mkdir()
    old = folder / '歌.mp3'; old.write_bytes(b'old')
    result = n.publish(source, folder, '歌')
    assert result.name == '歌 (1).mp3'
    assert old.read_bytes() == b'old'
    assert result.read_bytes() == b'audio'


@pytest.mark.parametrize('duration_ms,expected_status', [(4000, 'ok'), (214000, 'failed')])
def test_download_real_audio_validation_and_publish(service, tmp_path, monkeypatch, duration_ms, expected_status):
    fixture = Path(__file__).resolve().parents[1] / 'output/netease-smoke/converted/upstream-test.mp3'
    if not fixture.is_file():
        pytest.skip('optional real audio fixture not prepared')
    def request(path, data):
        if path == 'song/enhance/download/url':
            return {'data': {'url': 'https://m1.music.126.net/test.mp3', 'code': 200, 'type': 'mp3'}}, {}
        return {'songs': [{'id': 1, 'name': 'test', 'dt': duration_ms, 'ar': [{'name': 'artist'}], 'al': {'name': 'album'}}]}, {}
    monkeypatch.setattr(service, 'request', request)
    real_client = httpx.Client
    received_headers = []
    def transport(request):
        received_headers.append(request.headers)
        return httpx.Response(200, content=fixture.read_bytes())
    monkeypatch.setattr(n.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(transport), **kw))
    job = CommandJob(tmp_path, {'ids': [1], 'output_dir': str(tmp_path / 'out')})
    if expected_status == 'ok':
        service.download(job)
    else:
        with pytest.raises(ValueError):
            service.download(job)
    report = json.loads((job.work_dir / 'download-report.json').read_text('utf-8'))
    assert report['results'][0]['status'] == expected_status
    assert all('cookie' not in headers for headers in received_headers)
    assert sum(x['kind'] == 'audio' for x in job.artifacts) == (1 if expected_status == 'ok' else 0)


def test_real_upstream_converter_mp3_and_original(tmp_path):
    fixture = Path(__file__).resolve().parents[1] / 'output/netease-smoke/upstream-test.ncm'
    if not fixture.is_file():
        pytest.skip('optional upstream test fixture not downloaded')
    try:
        n.converter_binary()
    except ValueError:
        pytest.skip('upstream Windows converter not staged')
    original = fixture.read_bytes()
    for fmt in ['original', 'mp3']:
        job = CommandJob(tmp_path, {'paths': [str(fixture)], 'output_dir': str(tmp_path / fmt), 'format': fmt, 'bitrate': 128})
        result = n.convert(job)
        audio = Path(result['results'][0]['path'])
        info = n.probe(audio)
        assert info['has_audio'] and abs(info['duration'] - 4) < .2
        if fmt == 'mp3':
            assert audio.suffix == '.mp3'
            assert next(x for x in info['streams'] if x['codec_type'] == 'audio')['codec_name'] == 'mp3'
    assert fixture.read_bytes() == original


def test_corrupt_ncm_is_reported_without_audio(tmp_path):
    try:
        n.converter_binary()
    except ValueError:
        pytest.skip('upstream converter not staged')
    source = tmp_path / '坏文件.ncm'; source.write_bytes(b'not-ncm')
    job = CommandJob(tmp_path, {'paths': [str(source)], 'output_dir': str(tmp_path / 'out')})
    with pytest.raises(ValueError):
        n.convert(job)
    assert source.read_bytes() == b'not-ncm'
    assert json.loads((job.work_dir / 'conversion-report.json').read_text('utf-8'))['results'][0]['status'] == 'failed'
    assert all(x['kind'] != 'audio' for x in job.artifacts)
