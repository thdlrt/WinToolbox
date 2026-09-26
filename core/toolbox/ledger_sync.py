"""Shared WebDAV transport for immutable ledger operations and verified blobs."""
import json
import re

from .ledger import canonical, validate
from .project_memory_sync import MemoryRemote


class LedgerRemote(MemoryRemote):
    def sync(self, store, job, read_only=False):
        root = self.directory + 'ledger-v1/'
        if read_only:
            if self.xml(root, 0) is None:
                return {'uploaded': 0, 'downloaded': 0, 'conflicts': len(store.conflicts()), 'synced_operations': 0}
        else:
            self.ensure_directory()
            for suffix in ('', 'ops/', 'blobs/'):
                self.mkdir(root + suffix)
        known_names = {name for name, directory in self.children(root + 'ops/') if not directory}
        remote_blobs = {name for name, directory in self.children(root + 'blobs/') if not directory}
        uploaded = downloaded = 0
        # Upload the durable local queue. Retry is idempotent even after lost responses.
        local_snapshot = [] if read_only else store.export_operations()
        synced_ids = {op['op_id'] for op in local_snapshot}
        for op in local_snapshot:
            job.check_cancelled()
            filename = op['op_id'] + '.json'
            if filename in known_names:
                # JSON comparison tolerates platform indentation and key ordering.
                existing = json.loads(self.read(root + 'ops/' + filename, 1024 * 1024))
                if existing != op:
                    raise ValueError('WebDAV 记账操作内容冲突，已停止同步')
                continue
            for field, attachment in op['changes'].items():
                if field.startswith('attachment:') and attachment is not None:
                    digest = attachment['sha256']
                    body = store.get_blob(digest)
                    if len(body) != attachment['size']:
                        raise ValueError('本地记账附件大小校验失败')
                    if digest not in remote_blobs:
                        self.put_immutable(root + 'blobs/' + digest, body)
                        remote_blobs.add(digest)
            response = self.request('PUT', root + 'ops/' + filename, content=canonical(op),
                                    headers={'If-None-Match': '*', 'Content-Type': 'application/json; charset=utf-8'})
            if response.status_code == 412:
                if json.loads(self.read(root + 'ops/' + filename, 1024 * 1024)) != op:
                    raise ValueError('WebDAV 记账操作内容冲突，已停止同步')
            else:
                self.checked(response, (201, 204))
            uploaded += 1
        local_ids = {op['op_id'] for op in store.export_operations()}
        incoming = []
        for name, directory in self.children(root + 'ops/'):
            job.check_cancelled()
            if directory or not re.fullmatch(r'[a-f0-9]{32}\.json', name) or name[:-5] in local_ids:
                continue
            op = validate(json.loads(self.read(root + 'ops/' + name, 1024 * 1024)))
            if name[:-5] != op['op_id']:
                raise ValueError('WebDAV 记账操作标识不符')
            for field, attachment in op['changes'].items():
                if field.startswith('attachment:') and attachment is not None:
                    digest = attachment['sha256']
                    try:
                        body = store.get_blob(digest)
                    except FileNotFoundError:
                        body = self.read(root + 'blobs/' + digest, 50 * 1024 * 1024)
                        store.put_blob(body, digest)
                    if len(body) != attachment['size']:
                        raise ValueError('WebDAV 记账附件大小校验失败')
            incoming.append(op)
            synced_ids.add(op['op_id'])
            downloaded += 1
        if incoming:
            getattr(store, 'apply_remote', store.ingest)(incoming)
        if store.incomplete():
            raise ValueError('部分记账操作缺少父版本，已保留并等待下次同步')
        job.progress(100, '记账与诊断配置同步完成')
        return {'uploaded': uploaded, 'downloaded': downloaded, 'conflicts': len(store.conflicts()), 'synced_operations': len(synced_ids)}
