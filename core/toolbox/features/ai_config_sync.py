"""Manual AI configuration exchange through the shared WebDAV connection."""
import contextlib
import copy
import hashlib
import threading
import time
import uuid
from urllib.parse import urlsplit

from .. import ai_config
from ..settings import atomic_json, protect
from ..webdav_layout import shared_config, service_config
from .webdav import Remote


def identity(config):
    return hashlib.sha256(ai_config.encode({key: config.get(key) for key in ('url', 'username', 'password_dpapi', 'remote_path')})).hexdigest()


class AiRemote(Remote):
    def __init__(self, config):
        super().__init__(service_config(config, 'ai_config'))
        self.target = self.directory + 'config-v1.json'

    def upload(self, envelope, job):
        self.ensure_directory()
        temporary = self.directory + '.upload-' + uuid.uuid4().hex + '.part'
        try:
            job.check_cancelled()
            self.checked(self.request('PUT', temporary, content=ai_config.encode(envelope), headers={'If-None-Match': '*', 'Content-Type': 'application/json; charset=utf-8'}), (201, 204))
            job.check_cancelled()
            response = self.request('MOVE', temporary, headers={'Destination': self.target, 'Overwrite': 'T'})
            if response.status_code == 502:
                job.check_cancelled()
                response = self.request('MOVE', temporary, headers={'Destination': urlsplit(self.target).path, 'Overwrite': 'T'})
            self.checked(response, (201, 204))
            if hasattr(job, 'mark_committed'):
                job.mark_committed()
        finally:
            with contextlib.suppress(Exception):
                self.request('DELETE', temporary)

    def download(self, job):
        body = bytearray()
        with self.client.stream('GET', self.target) as response:
            if response.status_code == 404:
                raise ValueError('云端尚无 AI 配置，请先从已配置设备上传')
            self.checked(response, (200,))
            for block in response.iter_bytes(65536):
                job.check_cancelled()
                body.extend(block)
                if len(body) > ai_config.MAX_BYTES:
                    raise ValueError('AI 配置超过 1 MiB 大小限制')
        return ai_config.parse(bytes(body))


def register(app):
    lock = threading.RLock()
    transfer_lock = threading.Lock()
    requests, previews = {}, {}
    state_path = app.data_dir / 'ai-config-sync.json'
    state = ai_config.parse(state_path.read_bytes()) if state_path.exists() else {}

    def require_config():
        value = shared_config(app.data_dir)
        if not value.get('url') or not value.get('username') or not value.get('password_dpapi'):
            raise ValueError('请先保存统一 WebDAV 地址、账号和密码')
        return value

    def current_hash():
        return hashlib.sha256(ai_config.encode(ai_config.local_payload(app.settings))).hexdigest()

    def status(_):
        value = shared_config(app.data_dir)
        providers = [{key: row.get(key) for key in ('id', 'name', 'kind', 'has_key')} for row in app.settings.get()['providers']]
        with lock:
            return {'configured': bool(value.get('url') and value.get('username') and value.get('password_dpapi')),
                    'remote_path': service_config(value, 'ai_config')['remote_path'] + '/config-v1.json',
                    'local_providers': providers,
                    'last_upload': state.get('last_upload'), 'last_download': state.get('last_download'), 'error': state.get('error')}

    def export(_):
        config = require_config()
        return ai_config.encrypt(ai_config.local_payload(app.settings), protect(config['password_dpapi'], decrypt=True))

    def submit(params, upload=False):
        if set(params) - {'confirmation_token', 'confirm_replace'}:
            raise ValueError('AI 配置同步只使用已保存的 WebDAV 连接')
        config = require_config()
        token = uuid.uuid4().hex
        request = {'config': config, 'identity': identity(config), 'expires': time.monotonic() + 900,
                   'action': 'upload' if upload else 'preview'}
        if params.get('confirm_replace') is not None and type(params['confirm_replace']) is not bool:
            raise ValueError('请确认是否替换 AI 配置')
        if not upload and params.get('confirmation_token'):
            if params.get('confirm_replace') is not True:
                raise ValueError('替换 AI 配置需要确认')
            with lock:
                staged = previews.get(params['confirmation_token'])
            if not staged or staged['expires'] < time.monotonic():
                raise ValueError('AI 配置预览已过期，请重新下载预览')
            if staged['identity'] != identity(config):
                raise ValueError('WebDAV 连接已改变，请重新下载预览')
            if staged['local_hash'] != current_hash():
                raise ValueError('本机 AI 配置已改变，请重新下载预览')
            request.update(action='apply', preview_token=params['confirmation_token'])
        elif params.get('confirm_replace'):
            raise ValueError('请先下载并预览云端 AI 配置')
        with lock:
            now = time.monotonic()
            for container in (requests, previews):
                for key in [key for key, value in container.items() if value['expires'] < now]:
                    container.pop(key, None)
            requests[token] = request
        try:
            return app.jobs.submit('ai.config.upload' if upload else 'ai.config.download', {'request_token': token})
        except BaseException:
            with lock:
                requests.pop(token, None)
            raise

    def run(job):
        with lock:
            request = requests.pop(job.params.get('request_token'), None)
        if not request or request['expires'] < time.monotonic():
            raise ValueError('AI 配置请求已过期，请重新点击上传或下载')
        if not transfer_lock.acquire(blocking=False):
            raise ValueError('AI 配置同步正在进行，请稍后重试')
        remote = None
        try:
            with lock:
                state['error'] = None
            config = require_config()
            if identity(config) != request['identity']:
                raise ValueError('WebDAV 连接已改变，请重新操作')
            if request['action'] == 'apply':
                with lock:
                    staged = previews.get(request['preview_token'])
                with getattr(app, 'data_lock', contextlib.nullcontext()), app.settings.lock:
                    if not staged or staged['expires'] < time.monotonic():
                        raise ValueError('AI 配置预览已过期，请重新下载')
                    if staged['local_hash'] != current_hash() or identity(require_config()) != staged['identity']:
                        raise ValueError('连接或本机 AI 配置已改变，请重新下载预览')
                    payload = ai_config.validate(staged['payload'])
                    old_value, old_secrets = copy.deepcopy(app.settings.value), copy.deepcopy(app.settings.secrets)
                    previous_files = {path: path.read_bytes() if path.exists() else None for path in (app.settings.path, app.settings.secret_path)}
                    new_value = {**old_value, 'providers': payload['providers'], 'roles': payload['roles']}
                    new_secrets = {key: protect(value) for key, value in payload['secrets'].items() if value}
                    job.check_cancelled()
                    try:
                        atomic_json(app.settings.secret_path, new_secrets)
                        atomic_json(app.settings.path, new_value)
                        app.settings.value, app.settings.secrets = new_value, new_secrets
                        if hasattr(job, 'mark_committed'):
                            job.mark_committed()
                    except BaseException:
                        for path, body in previous_files.items():
                            if body is None:
                                path.unlink(missing_ok=True)
                            else:
                                path.write_bytes(body)
                        app.settings.value, app.settings.secrets = old_value, old_secrets
                        raise
                with lock:
                    previews.pop(request['preview_token'], None)
                    state['last_download'] = time.time()
                app.emit('ai.config.changed')
                job.progress(100, 'AI 模型与密钥已替换，其他设置保持完整')
                return {'applied': True, **ai_config.preview(payload)}
            remote = AiRemote(config)
            password = protect(config['password_dpapi'], decrypt=True)
            if request['action'] == 'upload':
                job.progress(10, '正在加密 AI 配置')
                envelope = ai_config.encrypt(ai_config.local_payload(app.settings), password)
                remote.upload(envelope, job)
                with lock:
                    state['last_upload'] = time.time()
                job.progress(100, 'AI 配置已加密上传')
                return {'uploaded': True, 'remote_path': service_config(config, 'ai_config')['remote_path'] + '/config-v1.json'}
            job.progress(20, '正在下载并验证 AI 配置')
            payload = ai_config.decrypt(remote.download(job), password)
            confirmation = uuid.uuid4().hex
            local_hash = current_hash()
            with lock:
                previews[confirmation] = {'payload': payload, 'expires': time.monotonic() + 900,
                                         'local_hash': local_hash, 'identity': request['identity']}
            job.progress(100, '请核对云端模型配置，再确认替换')
            return {'confirmation_token': confirmation, 'needs_confirmation': True, **ai_config.preview(payload)}
        except Exception as exc:
            with lock:
                state['error'] = str(exc) if isinstance(exc, ValueError) else 'AI 配置同步失败，本机配置已保留'
            if not isinstance(exc, ValueError):
                raise ValueError('AI 配置同步失败，请检查连接；本机配置已保留') from None
            raise
        finally:
            if remote:
                remote.close()
            with lock:
                atomic_json(state_path, state)
            transfer_lock.release()

    app.register('ai.config.status', status)
    app.register('ai.config.export', export)
    app.register('ai.config.upload', lambda params: submit(params, upload=True))
    app.register('ai.config.download', submit)
    app.jobs.register('ai.config.upload', run)
    app.jobs.register('ai.config.download', run)
