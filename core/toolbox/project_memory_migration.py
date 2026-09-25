"""Read-only legacy inventory followed by a revision-checked, atomic log import."""
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

import yaml

from .settings import atomic_json
from .project_memory.store import MemoryStore


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def local_path(value):
    value = str(value)
    if os.name != 'nt' and re.match(r'^[A-Za-z]:[\\/]', value):
        return Path('/mnt') / value[0].lower() / value[3:].replace('\\', '/')
    return Path(value).expanduser()


def parse(path):
    raw = path.read_bytes()
    text = raw.decode('utf-8-sig')
    match = re.match(r'^---\r?\n(.*?)\r?\n---(?:\r?\n|$)(.*)', text, re.S)
    if not match:
        return None
    meta = yaml.safe_load(match[1])
    if not isinstance(meta, dict) or not meta.get('ak_id'):
        return None
    # YAML timestamps remain strings in JSON, including unknown metadata.
    meta = json.loads(json.dumps(meta, default=str, ensure_ascii=False))
    return meta, match[2], raw


class Migration:
    def __init__(self, store, root):
        self.store, self.root = store, Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def preview(self, central_path):
        central = local_path(central_path).resolve()
        if not (central / 'Projects').is_dir():
            raise ValueError('请选择旧 AgentKnowledge 中央目录（包含 Projects）')
        records, sources, projects, warnings = [], {}, {}, []
        contexts = []
        candidates = set(central.rglob('*.md'))
        for path in sorted((central / 'Projects').glob('*.md')):
            parsed = parse(path)
            if not parsed:
                continue
            meta, body, raw = parsed
            if meta.get('kind') != 'project':
                continue
            project_id = meta['ak_id']
            context = meta.get('context_dir', '.agent')
            if context not in ('.agent', '.agents'):
                raise ValueError(f'项目上下文目录无效：{path.name}')
            root = local_path(meta.get('project_root', '')).resolve()
            # Registration is evidence only. Bind only after a matching local manifest.
            manifest = root / context / 'manifest.md'
            valid = False
            if manifest.is_file():
                parsed_manifest = parse(manifest)
                valid = bool(parsed_manifest and parsed_manifest[0]['ak_id'] == project_id)
            projects[project_id] = {'id': project_id, 'name': meta.get('title', root.name),
                                    'path': str(root) if valid else None, 'context_dir': context}
            if valid:
                contexts.append(root / context)
                candidates.update((root / context).rglob('*.md'))
            else:
                warnings.append(f'{meta.get("title", project_id)}：当前设备没有匹配的项目目录，仅导入登记信息')
        seen = {}
        blobs = {}
        for path in sorted(candidates):
            if '_system' in path.parts or (path.is_relative_to(central) and 'Templates' in path.relative_to(central).parts):
                continue
            if path.is_symlink():
                warnings.append(f'跳过符号链接：{path.name}')
                continue
            parsed = parse(path)
            if not parsed:
                continue
            meta, body, raw = parsed
            if meta.get('kind') not in ('project', 'task', 'knowledge', 'promotion'):
                warnings.append(f'未识别记录类型：{path.name}')
                continue
            sources[str(path)] = digest(raw)
            kind = meta['kind']
            clean = {key: value for key, value in meta.items() if key not in ('project_root', 'central_root', 'context_dir')}
            data = {'id': meta['ak_id'], 'kind': 'knowledge' if kind == 'promotion' else kind,
                    'title': meta.get('title', path.stem), 'body': body,
                    'scope': meta.get('scope', 'project'), 'project_id': meta.get('project_id') or None,
                    'related_ids': meta.get('related_ids', []), 'legacy': clean,
                    'created_at': meta.get('created'), 'updated_at': meta.get('updated')}
            if kind == 'project':
                data.update(name=data['title'])
            elif kind == 'task':
                data['status'] = 'done' if meta.get('status') in ('done', 'archived') else meta.get('status', 'active')
            else:
                data['knowledge_type'] = meta.get('knowledge_type') or 'fact'
                data['archived'] = meta.get('status') == 'archived'
                if kind == 'promotion':
                    data['promotion'] = {'state': meta.get('status', 'candidate'), 'source_id': meta.get('source_id'),
                                         'source_project_id': meta.get('source_project_id'), 'source_hash': meta.get('source_hash')}
            # Preserve local linked evidence as content-addressed blobs; never fetch URLs.
            attachments = []
            refs = re.findall(r'!?\[\[([^\]|]+)(?:\|[^\]]*)?\]\]|!?\[[^\]]*\]\(([^)]+)\)', body)
            allowed = [central.parent, *(context.parent for context in contexts)]
            for wiki, markdown in refs:
                ref = (wiki or markdown).split('#')[0].strip('<> ')
                if not ref or '://' in ref or ref.startswith('#'):
                    continue
                choices = [(path.parent / ref).resolve(), (central.parent / ref).resolve()]
                for context in contexts:
                    if path.is_relative_to(context):
                        choices.extend([(context / ref).resolve(), (context.parent / ref).resolve()])
                        if '/' not in ref and '\\' not in ref:
                            choices.extend(context.rglob(ref if Path(ref).suffix else ref + '.md'))
                if not Path(ref).suffix:
                    choices.extend([p.with_suffix('.md') for p in list(choices)])
                target = next((p for p in choices if p.is_file() and any(p.is_relative_to(r.resolve()) for r in allowed)), None)
                if not target and any(p.is_dir() for p in choices):
                    continue
                if not target:
                    warnings.append(f'{path.name}：未打包链接 {ref}')
                    continue
                if target.stat().st_size > 64 * 1024 * 1024:
                    warnings.append(f'{path.name}：附件大于 64 MiB，保留原链接 {ref}')
                    continue
                raw_blob = target.read_bytes()
                sha = digest(raw_blob)
                blobs[sha] = {'path': str(target), 'sha256': sha, 'size': len(raw_blob)}
                sources[str(target)] = sha
                attachments.append({'hash': sha, 'name': target.name, 'size': len(raw_blob), 'original_link': ref})
            if attachments:
                data['attachments'] = attachments
            encoded = json.dumps(data, ensure_ascii=False, sort_keys=True)
            # Registry/manifest are the same logical project, not duplicate content.
            if kind == 'project' and data['id'] in seen:
                continue
            if encoded in seen.get(data['id'], []):
                continue
            if data['id'] in seen:
                warnings.append(f'发现同 ID 的不同版本，将保留冲突：{data["title"]}')
            seen.setdefault(data['id'], []).append(encoded)
            records.append(data)
        token = uuid.uuid4().hex
        report = {'preview_id': token, 'library_id': self.store.info()['library_id'], 'central_path': str(central),
                  'sources': sources, 'records': records, 'projects': list(projects.values()),
                  'blobs': blobs, 'warnings': sorted(set(warnings)), 'record_count': len(records),
                  'project_count': len(projects), 'attachment_count': len(blobs)}
        atomic_json(self.root / (token + '.json'), report)
        return {k: v for k, v in report.items() if k not in ('sources', 'records', 'blobs')}

    def apply(self, preview_id, job=None):
        if not re.fullmatch('[a-f0-9]{32}', str(preview_id)):
            raise ValueError('无效迁移预览')
        plan_path = self.root / (preview_id + '.json')
        plan = json.loads(plan_path.read_text('utf-8'))
        if plan.get('completed'):
            return plan['completed']
        if plan['library_id'] != self.store.info()['library_id']:
            raise ValueError('记忆库已切换，请重新预览')
        for file, sha in plan['sources'].items():
            if not Path(file).is_file() or digest(Path(file).read_bytes()) != sha:
                raise ValueError(f'预览后源文件发生变化，请重新预览：{Path(file).name}')
        backup = self.root / (preview_id + '-sources')
        backup.mkdir(exist_ok=True)
        for file, sha in plan['sources'].items():
            raw = Path(file).read_bytes()
            if digest(raw) != sha:
                raise ValueError('源文件正在写入，请稍后重新预览')
            (backup / sha).write_bytes(raw)
        # Persist the import batch before commit, so crash/retry reuses operation IDs.
        batch_path = self.root / (preview_id + '-operations.json')
        if batch_path.is_file():
            operations = json.loads(batch_path.read_text('utf-8'))
        else:
            operations = []
            for i, data in enumerate(plan['records']):
                if job:
                    job.progress(10 + i * 70 / max(1, len(plan['records'])), '准备项目与知识')
                # A separate replica per source preserves conflicting legacy siblings.
                with tempfile.TemporaryDirectory(prefix='memory-migration-') as stage:
                    staged = MemoryStore(Path(stage), device_id=self.store.info()['device_id'])
                    try:
                        staged.join_library(plan['library_id'])
                        if data['kind'] == 'project':
                            staged.save_project(data, parents=[])
                        else:
                            staged.save_entry(data, parents=[])
                        operations.extend(staged.export_operations())
                    finally:
                        staged.close()
            atomic_json(batch_path, operations)
        for item in plan['blobs'].values():
            self.store.add_blob((backup / item['sha256']).read_bytes())
        existing_content = {json.dumps([op['entity_id'], op['data']], ensure_ascii=False, sort_keys=True)
                            for op in self.store.export_operations()}
        operations = [op for op in operations if json.dumps([op['entity_id'], op['data']], ensure_ascii=False, sort_keys=True) not in existing_content]
        result = self.store.ingest_operations(operations)
        if job:
            job.mark_committed()
        bound, warnings = [], list(plan['warnings'])
        for project in plan['projects']:
            if project['path']:
                try:
                    self.store.bind_project(project['id'], project['path'], context_dir=project['context_dir'])
                    bound.append(project['id'])
                except (OSError, ValueError) as exc:
                    warnings.append(f'{project["name"]}：本机绑定未完成：{exc}')
        completed = {'record_count': len(plan['records']), 'project_count': len(plan['projects']),
                     'bound_count': len(bound), 'attachment_count': len(plan['blobs']),
                     'warnings': warnings, 'backup_path': str(backup), 'import': result,
                     'legacy_unchanged': True}
        plan['completed'] = completed
        atomic_json(plan_path, plan)
        return completed
