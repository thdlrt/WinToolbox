"""Immutable, record-level WebDAV replication. Never shares a database or bindings."""
import hashlib
import json
import re
from urllib.parse import quote, unquote, urljoin, urlsplit

from .features.webdav import Remote

MAX_RECORD = 8 * 1024 * 1024
MAX_BLOB = 64 * 1024 * 1024
HEX = re.compile(r'^[a-f0-9]{64}$')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def payload(op):
    return op.get('data') or op.get('payload') or {}


def bucket(op):
    data = payload(op)
    kind = op.get('entity_type') or op.get('kind') or data.get('entity_type')
    if kind == 'project':
        return 'catalog'
    if data.get('scope') == 'device':
        return None
    project = data.get('project_id') or op.get('project_id')
    return 'projects/' + digest(project) if project and data.get('scope', 'project') == 'project' else 'global'


def blob_hashes(operations):
    found = set()
    for op in operations:
        for item in payload(op).get('attachments', []):
            value = item.get('hash') if isinstance(item, dict) else item
            if isinstance(value, str) and HEX.fullmatch(value):
                found.add(value)
    return found


class MemoryRemote(Remote):
    def __init__(self, config):
        super().__init__(config)
        self.base = self.directory + 'project-memory/'

    def mkdir(self, url):
        response = self.request('MKCOL', url)
        self.checked(response, (201, 405))
        if response.status_code == 405:
            root = self.xml(url, 0)
            if root is None or not root.findall('.//{DAV:}collection'):
                raise ValueError('WebDAV 路径已被文件占用')

    def children(self, url):
        root = self.xml(url, 1)
        if root is None:
            return []
        expected = urlsplit(url)
        result = []
        for row in root.findall('{DAV:}response'):
            raw = row.findtext('{DAV:}href', '')
            candidate = urlsplit(urljoin(url, raw))
            path = unquote(candidate.path).rstrip('/')
            parent, _, name = path.rpartition('/')
            if (candidate.scheme != expected.scheme or candidate.netloc != expected.netloc
                    or candidate.query or candidate.fragment or parent + '/' != unquote(expected.path)
                    or name in ('', '.', '..') or '/' in name or '\\' in name):
                continue
            prop = next((node.find('{DAV:}prop') for node in row.findall('{DAV:}propstat')
                         if ' 200 ' in node.findtext('{DAV:}status', '')), None)
            if prop is None:
                continue
            result.append((name, prop.find('.//{DAV:}collection') is not None))
        return result

    def read(self, url, limit=MAX_RECORD):
        data = bytearray()
        with self.client.stream('GET', url) as response:
            self.checked(response, (200,))
            for block in response.iter_bytes():
                data.extend(block)
                if len(data) > limit:
                    raise ValueError('WebDAV 记录或附件超过大小限制')
        return bytes(data)

    def put_immutable(self, url, data):
        response = self.request('PUT', url, content=data, headers={'If-None-Match': '*', 'Content-Type': 'application/octet-stream'})
        if response.status_code == 412:
            if self.read(url, max(MAX_RECORD, len(data))) != data:
                raise ValueError('WebDAV 不可变记录内容不一致，已停止同步并保留本地数据')
        else:
            self.checked(response, (201, 204))

    def libraries(self):
        result = []
        for name, directory in self.children(self.base):
            if directory and HEX.fullmatch(name):
                value = json.loads(self.read(self.base + name + '/library.json'))
                if value.get('format') != 1 or digest(value.get('library_id')) != name:
                    raise ValueError('WebDAV 知识库标识校验失败')
                result.append({'library_id': value['library_id']})
        return result

    def sync(self, store, job, verify=False):
        self.ensure_directory()
        self.mkdir(self.base)
        info = store.info()
        existing = self.libraries()
        if existing and info['library_id'] not in [item['library_id'] for item in existing]:
            raise ValueError('服务器已有其他知识库；请先选择并加入知识库，不会自动合并')
        root = self.base + digest(info['library_id']) + '/'
        self.mkdir(root)
        self.put_immutable(root + 'library.json', canonical({'format': 1, 'library_id': info['library_id']}))
        for path in ('catalog', 'global', 'projects', 'blobs'):
            self.mkdir(root + path + '/')
        local = [op for op in store.export_operations() if bucket(op)]
        uploaded = 0
        made = {'catalog', 'global'}
        remote_blobs = {name for name, directory in self.children(root + 'blobs/') if not directory}
        remote_ops = {}
        # Upload blobs first: a published operation must never reference an unfinished blob.
        for value in sorted(blob_hashes(local)):
            job.check_cancelled()
            if value in remote_blobs and not verify:
                continue
            body = store.get_blob(value)
            if len(body) > MAX_BLOB or hashlib.sha256(body).hexdigest() != value:
                raise ValueError('本地附件校验失败或超过 64 MB')
            self.put_immutable(root + 'blobs/' + value, body)
        for op in local:
            job.check_cancelled()
            folder = bucket(op)
            if folder not in made:
                self.mkdir(root + folder + '/')
                made.add(folder)
            if folder not in remote_ops:
                remote_ops[folder] = {name for name, directory in self.children(root + folder + '/') if not directory}
            filename = digest(op['op_id']) + '.json'
            if filename in remote_ops[folder] and not verify:
                continue
            body = canonical(op)
            if len(body) > MAX_RECORD:
                raise ValueError('知识记录超过 8 MB')
            self.put_immutable(root + folder + '/' + filename, body)
            uploaded += 1
        known = set(store.operation_ids())
        known_filenames = {digest(op_id) + '.json' for op_id in known}
        downloaded = 0
        def pull(folder):
            nonlocal downloaded
            for name, is_dir in self.children(root + folder + '/'):
                job.check_cancelled()
                if is_dir or not name.endswith('.json') or not HEX.fullmatch(name[:-5]):
                    continue
                if name in known_filenames and not verify:
                    continue
                op = json.loads(self.read(root + folder + '/' + quote(name, safe='')))
                if not isinstance(op, dict) or digest(op.get('op_id')) != name[:-5] or bucket(op) != folder:
                    raise ValueError('WebDAV 操作记录路径或标识不匹配')
                if op['op_id'] in known:
                    if verify:
                        store.ingest_operations([op])
                    continue
                # Import each operation durably; causal parent order is handled by the store.
                for value in blob_hashes([op]):
                    try:
                        store.get_blob(value)
                    except (OSError, KeyError, ValueError):
                        body = self.read(root + 'blobs/' + value, MAX_BLOB)
                        if hashlib.sha256(body).hexdigest() != value:
                            raise ValueError('WebDAV 附件校验失败')
                        store.add_blob(body)
                store.ingest_operations([op])
                known.add(op['op_id'])
                downloaded += 1
        pull('catalog')
        pull('global')
        for project in store.projects():
            if project.get('subscribed', True):
                pull('projects/' + digest(project['id']))
        job.progress(100, '项目记忆同步完成')
        return {'uploaded': uploaded, 'downloaded': downloaded, 'conflicts': len(store.conflicts()), 'library_id': info['library_id']}
