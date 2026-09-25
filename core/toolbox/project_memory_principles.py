"""Native personal AGENTS block and a shared template for future projects."""
import hashlib
import json
import os
from pathlib import Path

from .project_memory import principles


def register(app, store):
    def global_path():
        return Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'AGENTS.md'

    def get(params):
        if params.get('target') == 'global':
            return principles.read_rules(global_path())
        if params.get('target') != 'template':
            raise ValueError('请选择个人全局原则或新项目模板')
        try:
            record = store.get_entry('memory-project-template')
        except KeyError:
            record = {'body': principles.DEFAULT_PROJECT_RULES, 'heads': []}
        if record.get('conflict'):
            raise ValueError('项目模板存在同步冲突，请先合并模板版本')
        return {'text': record['body'], 'hash': hashlib.sha256(json.dumps(sorted(record['heads'])).encode()).hexdigest(), 'heads': record['heads']}

    def save(params):
        text = params.get('text')
        if not isinstance(text, str) or len(text) > 32000:
            raise ValueError('原则正文不能超过 32000 字符')
        if params.get('target') == 'global':
            return principles.save_rules(global_path(), text, params.get('hash'))
        current = get(params)
        if current['hash'] != params.get('hash'):
            raise ValueError('项目模板已修改，请重新读取后合并')
        if '<!-- agent-knowledge:' in text:
            raise ValueError('只编辑原则正文，不要添加区块边界标记')
        store.save_entry({'id': 'memory-project-template', 'kind': 'knowledge', 'scope': 'global',
                          'title': '新项目知识原则模板', 'knowledge_type': 'preference', 'body': text,
                          'internal': True, 'archived': True}, parents=current['heads'])
        return get(params)

    app.register('memory.principles.get', get)
    app.register('memory.principles.save', save)
