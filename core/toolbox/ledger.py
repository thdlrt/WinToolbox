"""Portable immutable causal ledger shared by desktop and Android."""
import copy
import datetime
import hashlib
import json
import math
import re
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path

from .settings import atomic_json

DEFAULT_PROJECT = '00000000000000000000000000000001'
ID = re.compile(r'^[0-9a-f]{32}$')
HASH = re.compile(r'^[0-9a-f]{64}$')
ENTITY_TYPES = ('entry', 'project', 'network_profile')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def validate(op):
    if (not isinstance(op, dict) or op.get('schema') != 1 or op.get('entity_type') not in ENTITY_TYPES
            or not ID.fullmatch(str(op.get('op_id', ''))) or not ID.fullmatch(str(op.get('entity_id', '')))
            or not isinstance(op.get('parents'), list) or len(op['parents']) > 10000
            or any(not isinstance(p, str) or not ID.fullmatch(p) or p == op['op_id'] for p in op['parents'])
            or len(set(op['parents'])) != len(op['parents'])
            or not isinstance(op.get('changes'), dict) or not isinstance(op.get('deleted'), bool)
            or type(op.get('created_at')) not in (int, float) or not math.isfinite(op['created_at']) or op['created_at'] < 0
            or len(canonical(op)) > 1024 * 1024):
        raise ValueError('记账同步操作格式无效')
    allowed = {'entry': {'title', 'date', 'amount', 'amount_minor', 'currency', 'entry_type', 'status', 'category', 'notes', 'project_id', 'created_at'},
               'project': {'name', 'settlement_mode', 'created_at', 'archived'},
               'network_profile': {'name', 'target', 'port', 'timeout_ms', 'attempts'}}[op['entity_type']]
    for key, value in op['changes'].items():
        if not isinstance(key, str):
            raise ValueError('记账同步字段名称无效')
        if key.startswith('attachment:') and op['entity_type'] == 'entry':
            aid = key.removeprefix('attachment:')
            if not ID.fullmatch(aid):
                raise ValueError('记账附件标识无效')
            if value is not None:
                if (not isinstance(value, dict) or value.get('id') != aid or not HASH.fullmatch(str(value.get('sha256', '')))
                        or type(value.get('size')) is not int or not 0 < value['size'] <= 50 * 1024 * 1024
                        or not isinstance(value.get('original_name'), str) or len(value['original_name']) > 1000
                        or value.get('kind') not in ('invoice', 'receipt', 'payment', 'other')
                        or not re.fullmatch(r'attachments/' + op['entity_id'] + '/' + aid + r'\.(pdf|png|jpg|jpeg|webp|ofd)', str(value.get('relative_path', '')))):
                    raise ValueError('记账附件元数据无效')
        elif key not in allowed:
            raise ValueError('记账同步字段无效：' + key)
        elif key in ('title', 'name', 'category', 'notes', 'target'):
            limit = {'title': 200, 'name': 100, 'category': 80, 'notes': 5000, 'target': 2048}[key]
            if not isinstance(value, str) or '\0' in value or len(value) > limit or key != 'notes' and not value.strip():
                raise ValueError('记账同步文本字段无效：' + key)
        elif key in ('currency', 'entry_type', 'status', 'settlement_mode'):
            choices = {'currency': ('CNY', 'USD', 'EUR', 'HKD'), 'entry_type': ('expense', 'income'),
                       'status': ('waiting', 'reimbursed', 'non_reimbursable', 'not_applicable'),
                       'settlement_mode': ('general', 'half')}
            if value not in choices[key]:
                raise ValueError('记账同步选项无效：' + key)
        elif key == 'archived' and type(value) is not bool:
            raise ValueError('项目归档状态无效')
        elif key == 'project_id' and (not isinstance(value, str) or not ID.fullmatch(value)):
            raise ValueError('记账项目标识无效')
        elif key == 'date':
            try:
                if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) or datetime.date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except ValueError:
                raise ValueError('记账同步日期无效') from None
        elif key == 'amount' and (not isinstance(value, str) or not re.fullmatch(r'\d{1,9}\.\d{2}', value)):
            raise ValueError('记账同步金额无效')
        elif key in ('amount_minor', 'port', 'timeout_ms', 'attempts'):
            if key == 'port' and value is None:
                continue
            maximum = {'amount_minor': 99999999999, 'port': 65535, 'timeout_ms': 120000, 'attempts': 20}[key]
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError('记账同步数值无效：' + key)
        elif key == 'created_at' and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError('记账同步时间无效')
    if ('amount' in op['changes']) != ('amount_minor' in op['changes']):
        raise ValueError('金额和分值必须一起变更')
    if 'amount' in op['changes'] and int(Decimal(op['changes']['amount']) * 100) != op['changes']['amount_minor']:
        raise ValueError('记账金额与分值不符')
    if op['entity_type'] == 'entry' and not op['parents'] and not op['deleted']:
        required = {'title', 'date', 'amount', 'amount_minor', 'currency', 'entry_type', 'status', 'category', 'notes', 'project_id', 'created_at'}
        if not required.issubset(op['changes']):
            raise ValueError('新记账条目缺少必要字段')
    return op


class Ledger:
    def __init__(self, root):
        self.root = Path(root)
        self.ops_dir = self.root / 'ledger' / 'ops'
        self.blobs_dir = self.root / 'ledger' / 'blobs'
        self.ops_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.operations = {}
        self.on_change = lambda: None
        for path in self.ops_dir.glob('*.json'):
            op = validate(json.loads(path.read_text('utf-8')))
            if path.stem != op['op_id']:
                raise ValueError('记账操作文件标识不符')
            self.operations[op['op_id']] = op
        self.ingest([])  # Validate ancestry/cycles in restored local journals too.

    def ingest(self, operations):
        with self.lock:
            incoming = {}
            for op in operations:
                validate(op)
                if op['op_id'] in incoming and incoming[op['op_id']] != op:
                    raise ValueError('记账操作标识重复')
                incoming[op['op_id']] = op
            combined = {**self.operations, **incoming}
            for op in incoming.values():
                old = self.operations.get(op['op_id'])
                if old is not None and old != op:
                    raise ValueError('记账不可变操作发生冲突，已保留本地数据')
            for op in combined.values():
                for parent in op['parents']:
                    p = combined.get(parent)
                    if p and (p['entity_type'], p['entity_id']) != (op['entity_type'], op['entity_id']):
                        raise ValueError('记账操作父版本属于不同条目')
            # Kahn traversal rejects cycles before writing any received operation.
            degrees = {key: sum(p in combined for p in op['parents']) for key, op in combined.items()}
            children = {}
            for key, op in combined.items():
                for parent in op['parents']:
                    children.setdefault(parent, []).append(key)
            ready = [key for key, degree in degrees.items() if degree == 0]
            processed = 0
            while ready:
                key = ready.pop()
                processed += 1
                for child in children.get(key, []):
                    degrees[child] -= 1
                    if degrees[child] == 0:
                        ready.append(child)
            if processed != len(combined):
                raise ValueError('记账版本包含循环引用')
            for op in incoming.values():
                if op['op_id'] not in self.operations:
                    atomic_json(self.ops_dir / (op['op_id'] + '.json'), op)
                    self.operations[op['op_id']] = copy.deepcopy(op)

    def graph(self, kind, entity_id):
        ops = {k: v for k, v in self.operations.items() if v['entity_type'] == kind and v['entity_id'] == entity_id}
        ancestors, children = {}, {}
        degrees = {key: len(op['parents']) for key, op in ops.items()}
        for key, op in ops.items():
            for parent in op['parents']:
                children.setdefault(parent, []).append(key)
        ready = [key for key, degree in degrees.items() if degree == 0]
        while ready:
            key = ready.pop()
            found = set(ops[key]['parents'])
            for parent in ops[key]['parents']:
                found.update(ancestors[parent])
            ancestors[key] = found
            for child in children.get(key, []):
                degrees[child] -= 1
                if degrees[child] == 0:
                    ready.append(child)
        return {key: ops[key] for key in ancestors}, ancestors

    def get(self, kind, entity_id):
        with self.lock:
            ops, ancestors = self.graph(kind, entity_id)
            builtin = kind == 'project' and entity_id == DEFAULT_PROJECT
            if not ops and not builtin:
                return None
            heads = sorted(set(ops) - set().union(*(set(op['parents']) for op in ops.values()))) if ops else []
            result = {'id': entity_id, 'heads': heads, 'conflicts': {}, 'deleted': any(op['deleted'] for op in ops.values())}
            if builtin:
                result.update(name='AI报销', settlement_mode='half', created_at=0, archived=False)
            fields = set().union(*(set(op['changes']) for op in ops.values())) if ops else set()
            for field in fields:
                writers = [key for key, op in ops.items() if field in op['changes']]
                current = sorted(key for key in writers if not any(key in ancestors[other] for other in writers))
                candidates = [{'op_id': key, 'value': copy.deepcopy(ops[key]['changes'][field])} for key in current]
                values = {canonical(v['value']) for v in candidates}
                if len(values) > 1:
                    result['conflicts'][field] = candidates
                result[field] = candidates[-1]['value']
                if field.startswith('attachment:') and any(v['value'] is None for v in candidates):
                    result[field] = None
            if kind == 'entry' and 'amount' in result:
                result['conflicts'].pop('amount_minor', None)
                try:
                    result['amount_minor'] = int(Decimal(result['amount']) * 100)
                except Exception:
                    raise ValueError('同步金额无效') from None
                if result.get('entry_type') == 'income':
                    result['status'] = 'not_applicable'
                elif result.get('status') == 'not_applicable':
                    result['status'] = 'waiting'
            result['revision'] = len(ops)
            result['updated_at'] = max((float(op.get('created_at', 0)) for op in ops.values()), default=0)
            return result

    def list_entities(self, kind, include_deleted=False):
        with self.lock:
            ids = {v['entity_id'] for v in self.operations.values() if v['entity_type'] == kind}
            if kind == 'project':
                ids.add(DEFAULT_PROJECT)
            result = [self.get(kind, key) for key in sorted(ids)]
            return [v for v in result if v and (include_deleted or not v['deleted'])]

    def patch(self, kind, entity_id, changes, parents=None, deleted=False):
        with self.lock:
            current = self.get(kind, entity_id)
            if current and current['deleted']:
                raise ValueError('条目已删除；请新建条目')
            heads = current['heads'] if current else []
            if parents is not None and sorted(parents) != heads:
                raise ValueError('条目已有新版本，请刷新后重新解决冲突')
            op = {'schema': 1, 'op_id': uuid.uuid4().hex, 'entity_type': kind, 'entity_id': entity_id,
                  'parents': heads, 'changes': copy.deepcopy(changes), 'deleted': deleted, 'created_at': time.time()}
            self.ingest([op])
            result = self.get(kind, entity_id)
        self.on_change()
        return result

    def conflicts(self):
        return [{'entity_type': kind, 'id': item['id'], 'heads': item['heads'], 'fields': item['conflicts'],
                 'title': item.get('title') or item.get('name') or item['id']}
                for kind in ENTITY_TYPES for item in self.list_entities(kind) if item['conflicts']]

    def put_blob(self, body, digest=None):
        actual = hashlib.sha256(body).hexdigest()
        if len(body) > 50 * 1024 * 1024 or digest and actual != digest:
            raise ValueError('记账附件哈希校验失败或超过大小限制')
        with self.lock:
            self.blobs_dir.mkdir(parents=True, exist_ok=True)
            path = self.blobs_dir / actual
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() != actual:
                    raise ValueError('本地记账附件哈希校验失败')
            else:
                temporary = self.blobs_dir / (actual + '.tmp')
                temporary.write_bytes(body)
                temporary.replace(path)
        return actual

    def get_blob(self, digest):
        if not HASH.fullmatch(digest):
            raise ValueError('附件哈希无效')
        body = (self.blobs_dir / digest).read_bytes()
        if hashlib.sha256(body).hexdigest() != digest:
            raise ValueError('本地记账附件哈希校验失败')
        return body

    def export_operations(self):
        with self.lock:
            return copy.deepcopy(list(self.operations.values()))

    def incomplete(self):
        with self.lock:
            return sum(1 for op in self.operations.values() if any(p not in self.operations for p in op['parents']))
