"""Portable AI-only configuration, authenticated encryption and strict validation."""
import base64
import copy
import hashlib
import json
import os
import re
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT = 'wintoolbox-ai-config'
ENVELOPE_FORMAT = FORMAT + '-encrypted'
AAD = b'wintoolbox-ai-config-v1'
MAX_BYTES = 1024 * 1024


def encode(value):
    try:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError):
        raise ValueError('AI 配置格式无效') from None
    if len(body) > MAX_BYTES:
        raise ValueError('AI 配置超过 1 MiB 大小限制')
    return body


def parse(body):
    if len(body) > MAX_BYTES:
        raise ValueError('AI 配置超过 1 MiB 大小限制')
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        return json.loads(body, object_pairs_hook=pairs)
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('AI 配置不是有效的 JSON 对象') from None


def text(value, limit, empty=False):
    return isinstance(value, str) and len(value) <= limit and (empty or bool(value.strip())) and not any(ord(c) < 32 for c in value)


def validate(payload):
    if (not isinstance(payload, dict) or set(payload) != {'format', 'version', 'providers', 'roles', 'secrets'}
            or payload.get('format') != FORMAT or type(payload.get('version')) is not int or payload['version'] != 1):
        raise ValueError('AI 配置版本或字段无效')
    providers, roles, secrets = payload['providers'], payload['roles'], payload['secrets']
    if not isinstance(providers, list) or len(providers) > 100 or not isinstance(roles, dict) or len(roles) > 100 or not isinstance(secrets, dict):
        raise ValueError('AI 模型服务配置格式无效')
    ids = set()
    for provider in providers:
        if (not isinstance(provider, dict) or set(provider) - {'id', 'name', 'kind', 'base_url', 'region', 'model'}
                or not isinstance(provider.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', provider['id']) or provider['id'] in ids
                or provider.get('kind') not in ('openai', 'dashscope', 'gemini')
                or 'name' in provider and not text(provider['name'], 200, empty=True) or not text(provider.get('base_url'), 2048)):
            raise ValueError('AI 供应商字段无效')
        try:
            url = urlsplit(provider['base_url'])
            if url.scheme not in ('https', 'http') or not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment:
                raise ValueError()
            _ = url.port
        except ValueError:
            raise ValueError('AI 服务地址无效，不得包含账号、密码或查询参数') from None
        if any(key in provider and not text(provider[key], 50 if key == 'region' else 200, empty=True) for key in ('region', 'model')):
            raise ValueError('AI 供应商选项无效')
        ids.add(provider['id'])
    for name, role in roles.items():
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', name) or not isinstance(role, dict) or set(role) != {'provider_id', 'model'}
                or not text(role.get('provider_id'), 100) or role['provider_id'] not in ids | {'local'}
                or not text(role.get('model'), 200, empty=True)):
            raise ValueError('AI 模型角色配置无效')
    if any(key not in ids or not text(value, 8192, empty=True) for key, value in secrets.items()):
        raise ValueError('AI 密钥配置无效')
    encode(payload)
    return copy.deepcopy(payload)


def encrypt(payload, password, *, salt=None, nonce=None):
    validate(payload)
    if not isinstance(password, str) or not password:
        raise ValueError('请先保存统一 WebDAV 密码')
    salt = os.urandom(16) if salt is None else salt
    nonce = os.urandom(12) if nonce is None else nonce
    if len(salt) != 16 or len(nonce) != 12:
        raise ValueError('AI 配置加密参数无效')
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 200000, 32)
    cipher = AESGCM(key).encrypt(nonce, encode(payload), AAD)
    envelope = {'format': ENVELOPE_FORMAT, 'version': 1,
                'salt': base64.b64encode(salt).decode('ascii'), 'nonce': base64.b64encode(nonce).decode('ascii'),
                'ciphertext': base64.b64encode(cipher).decode('ascii')}
    encode(envelope)
    return envelope


def decrypt(envelope, password):
    encode(envelope)
    if (not isinstance(envelope, dict) or set(envelope) != {'format', 'version', 'salt', 'nonce', 'ciphertext'}
            or envelope.get('format') != ENVELOPE_FORMAT or type(envelope.get('version')) is not int or envelope['version'] != 1):
        raise ValueError('AI 加密配置版本或字段无效')
    try:
        salt, nonce, cipher = [base64.b64decode(envelope[key], validate=True) for key in ('salt', 'nonce', 'ciphertext')]
        if len(salt) != 16 or len(nonce) != 12 or len(cipher) < 16 or not isinstance(password, str) or not password:
            raise ValueError()
        key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 200000, 32)
        plaintext = AESGCM(key).decrypt(nonce, cipher, AAD)
    except Exception:
        raise ValueError('AI 配置解密失败：WebDAV 密码不匹配或文件已损坏，本机配置未更改') from None
    return validate(parse(plaintext))


def local_payload(settings):
    with settings.lock:
        providers = [{key: value for key, value in row.items() if key not in ('api_key', 'has_key', 'clear_key')} for row in settings.value['providers']]
        ids = {row['id'] for row in providers}
        return validate({'format': FORMAT, 'version': 1, 'providers': providers,
                         'roles': copy.deepcopy(settings.value['roles']),
                         'secrets': {key: value for key, value in settings.export_secrets().items() if key in ids}})


def preview(payload):
    return {'providers': [{**row, 'has_key': bool(payload['secrets'].get(row['id']))} for row in payload['providers']],
            'roles': copy.deepcopy(payload['roles']), 'secret_count': sum(bool(value) for value in payload['secrets'].values())}
