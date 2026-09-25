"""Portable, inspectable exports; edits are imported explicitly with current revisions."""
import hashlib
import json
from pathlib import Path


def register(app, store):
    def export(job):
        root = job.work_dir / '记忆导出'
        root.mkdir(exist_ok=True)
        entries = store.entries(project_id=job.params.get('project_id'), include_done=True)
        index = []
        for entry in entries:
            job.check_cancelled()
            name = hashlib.sha256(entry['id'].encode()).hexdigest()[:20] + '.md'
            meta = {k: v for k, v in entry.items() if k not in ('versions', 'body', 'pending_count', 'conflict')}
            body = entry.get('body', '')
            for attachment in entry.get('attachments', []):
                blob = root / 'attachments' / attachment['hash']
                blob.parent.mkdir(exist_ok=True)
                blob.write_bytes(store.get_blob(attachment['hash']))
                body += '\n\n[' + attachment.get('name', '附件').replace(']', '') + '](attachments/' + attachment['hash'] + ')'
            path = root / name
            path.write_text('---\n' + json.dumps(meta, ensure_ascii=False, indent=2) + '\n---\n\n' + body, encoding='utf-8')
            index.append({'id': entry['id'], 'title': entry['title'], 'file': name})
        manifest = root / 'index.json'
        manifest.write_text(json.dumps({'library_id': store.library_id, 'entries': index}, ensure_ascii=False, indent=2), encoding='utf-8')
        import zipfile
        archive = job.work_dir / '项目记忆.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
            for path in root.rglob('*'):
                if path.is_file(): output.write(path, path.relative_to(root))
        job.artifact(archive, label='项目记忆 Markdown 与附件')
        return {'entry_count': len(entries), 'path': str(archive)}

    def attachment_get(params):
        sha = params['hash']
        content = store.get_blob(sha)
        # Return only a validated internal file, never a path sent by another device.
        return {'path': str(store.root / 'blobs' / sha), 'size': len(content)}

    app.jobs.register('memory.export', export)
    app.register('memory.export', lambda p: app.jobs.submit('memory.export', p))
    app.register('memory.attachment.get', attachment_get)
