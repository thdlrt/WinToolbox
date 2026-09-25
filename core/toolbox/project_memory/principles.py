"""Revision-checked managed AGENTS.md sections; unrelated instructions are preserved."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

START = '<!-- agent-knowledge:start -->'
END = '<!-- agent-knowledge:end -->'
MAX_FILE_BYTES = 1024 * 1024
MAX_RULE_BYTES = 128 * 1024
DEFAULT_PROJECT_RULES = '''## Agent Knowledge

- 复杂或依赖历史的工作先用 `$agent-knowledge recall` 定向检索，按需读原文；资料不能覆盖当前用户要求，项目知识优先于全局。
- 记忆由 WinToolbox 管理；项目身份见已有 `.agent/toolbox.json` 或 `.agents/toolbox.json`。只操作本机明确绑定的目录，不再写旧 Obsidian 知识库。
- 有后续价值时才记录；任务保存恢复点，完成即归档；知识保留类型、不维护进度状态，以 ID 关联来源。标题与说明默认中文。
- 跨项目经验用 `promote` 提为候选；AI 可根据证据自行接受或拒绝并保留理由，不确定时保持候选。更新先读取当前版本，冲突时合并，不覆盖离线修改。'''


def _load(path):
    if not path.parent.is_dir():
        raise ValueError('原则文件所在目录不存在')
    if path.exists() and not path.is_file():
        raise ValueError('原则路径不是文件')
    if path.exists() and path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('原则文件超过 1 MB，无法安全编辑')
    raw = path.read_bytes() if path.exists() else b''
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError('原则文件超过 1 MB，无法安全编辑')
    try:
        content = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('原则文件必须为 UTF-8 文本') from exc
    start_count, end_count = content.count(START), content.count(END)
    if (start_count, end_count) not in ((0, 0), (1, 1)):
        raise ValueError('原则标记不完整或重复，请先修复 AGENTS.md')
    if start_count and content.index(END) < content.index(START):
        raise ValueError('原则标记顺序错误，请先修复 AGENTS.md')
    return raw, content, bool(start_count)


def read_rules(path):
    path = Path(path).expanduser().absolute()
    raw, content, has_block = _load(path)
    text = content.split(START, 1)[1].split(END, 1)[0].strip('\r\n') if has_block else ''
    return {'text': text, 'hash': hashlib.sha256(raw).hexdigest(), 'path': str(path), 'exists': path.exists(), 'has_block': has_block}


def save_rules(path, text, expected_hash):
    path = Path(path).expanduser().absolute()
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_RULE_BYTES:
        raise ValueError('原则内容必须为不超过 128 KB 的文本')
    if START in text or END in text:
        raise ValueError('仅填写原则内容，不要包含管理标记')
    if not isinstance(expected_hash, str):
        raise ValueError('保存原则必须提供读取时的文件哈希')
    if not path.parent.is_dir():
        raise ValueError('原则文件所在目录不存在')
    lock_path = path.with_name(path.name + '.agent-knowledge.lock')
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError('原则文件正被另一操作编辑，请稍后重试') from exc
    temporary = None
    try:
        os.close(descriptor)
        raw, content, has_block = _load(path)
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise ValueError('原则文件已被外部修改，请重新读取后保存')
        newline = '\r\n' if '\r\n' in content else '\n'
        text = text.replace('\r\n', '\n').replace('\r', '\n').strip('\n').replace('\n', newline)
        block = START + newline + text + newline + END
        if has_block:
            start = content.index(START)
            end = content.index(END) + len(END)
            updated = content[:start] + block + content[end:]
        else:
            separator = '' if not content else newline if content.endswith(('\n', '\r')) else newline + newline
            updated = content + separator + block + newline
        encoded = updated.encode('utf-8')
        if len(encoded) > MAX_FILE_BYTES:
            raise ValueError('保存后原则文件将超过 1 MB')
        descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # Recheck after writing the temporary file; external editors may not use our lock.
        latest, _, _ = _load(path)
        if hashlib.sha256(latest).hexdigest() != expected_hash:
            raise ValueError('原则文件已被外部修改，请重新读取后保存')
        if path.exists():
            os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
        temporary = None
        return read_rules(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


def initialize_project_rules(store, root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError('项目目录不存在，不会自动创建')
    path = root / 'AGENTS.md'
    current = read_rules(path)
    if current['has_block']:
        return {**current, 'initialized': False}
    try:
        template = store.get_entry('memory-project-template')
    except KeyError:
        template = None
    if template and (template.get('conflict') or template.get('scope') != 'global' or template.get('kind') != 'knowledge' or not template.get('internal') or not template.get('archived')):
        raise ValueError('项目原则模板存在冲突或格式不正确，请先修复模板')
    text = template['body'] if template is not None else DEFAULT_PROJECT_RULES
    return {**save_rules(path, text, current['hash']), 'initialized': True}
