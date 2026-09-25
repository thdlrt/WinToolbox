"""Explicit SSH targets, offline remote runtime and bounded stdio exchange."""
import base64
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import time
import zipfile

HOST = re.compile(r'^(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_][A-Za-z0-9_.-]*$')
MAX_OUTPUT = 128 * 1024 * 1024


def target(params):
    host = str(params.get('host', '')).strip()
    root = str(params.get('remote_root', '')).strip()
    if not HOST.fullmatch(host) or len(host) > 253:
        raise ValueError('请填写 SSH 主机别名或 user@hostname，不要填写命令或参数')
    if not root.startswith('/') or len(root) > 1000 or any(ord(c) < 32 for c in root) or '..' in PurePosixPath(root).parts:
        raise ValueError('远端数据目录必须是绝对 POSIX 路径，不能包含 ..')
    if root == '/':
        raise ValueError('请选择专用远端数据目录，不能使用根目录')
    return host, root.rstrip('/')


def run(host, code, data, job, timeout=120):
    # OpenSSH invokes a remote shell; quote the complete Python program as one argument.
    command = 'python3 -c ' + shlex.quote(code)
    args = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=yes', '--', host, command]
    import tempfile
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        request = json.dumps(data, ensure_ascii=False).encode('utf-8')
        if len(request) > MAX_OUTPUT:
            raise ValueError('SSH 交换超过 128 MB，请减少本次选择的项目或附件')
        source.write(request)
        source.seek(0)
        try:
            process = subprocess.Popen(args, stdin=source, stdout=output, stderr=error, creationflags=flags)
        except FileNotFoundError as exc:
            raise ValueError('未找到 OpenSSH 客户端，请安装系统 SSH 客户端') from exc
        start = time.monotonic()
        try:
            while process.poll() is None:
                job.check_cancelled()
                if time.monotonic() - start > timeout:
                    raise TimeoutError('SSH 操作超时，本地记录仍已保存，可以重试')
                if output.seek(0, 2) > MAX_OUTPUT or error.seek(0, 2) > 1024 * 1024:
                    raise ValueError('SSH 返回数据过大')
                time.sleep(.1)
            output.seek(0)
            error.seek(0)
            if process.returncode:
                detail = error.read(3000).decode('utf-8', 'replace').strip()
                raise RuntimeError('SSH 操作失败；请检查已信任主机、免密连接和远端 Python 3。' + detail)
            body = output.read(MAX_OUTPUT + 1)
            if len(body) > MAX_OUTPUT:
                raise ValueError('SSH 返回数据过大')
            return json.loads(body)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


LAUNCHER = '''import json
import pathlib
import sys

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(encoding='utf-8')
config = json.loads(pathlib.Path(__file__).with_name('client.json').read_text(encoding='utf-8'))
sys.path.insert(0, config['core'])
from toolbox.project_memory.cli import main
sys.exit(main(['--store', config['store'], *sys.argv[1:]]))
'''


INSTALL = r'''
import base64, io, json, os, pathlib, sys, zipfile
sys.stdin.reconfigure(encoding='utf-8'); sys.stdout.reconfigure(encoding='utf-8')
p=json.load(sys.stdin); root=pathlib.Path(p['root']); runtime=root/'runtime'
state=pathlib.Path(os.environ.get('XDG_STATE_HOME', str(pathlib.Path.home()/'.local/state')))/'wintoolbox-agent'
client=state/'client.json'; desired={'python':sys.executable,'core':str(runtime),'store':str(root/'data')}
if client.exists() and json.loads(client.read_text(encoding='utf-8')).get('store') != desired['store']:
 raise ValueError('This host already uses another memory store; explicit migration is required')
root.mkdir(parents=True, exist_ok=True); runtime.mkdir(exist_ok=True)
with zipfile.ZipFile(io.BytesIO(base64.b64decode(p['archive'], validate=True))) as z:
 for i in z.infolist():
  dest=(runtime/i.filename).resolve()
  if not dest.is_relative_to(runtime.resolve()) or i.is_dir(): raise ValueError('invalid runtime member')
  dest.parent.mkdir(parents=True, exist_ok=True)
  temporary=dest.with_suffix(dest.suffix+'.tmp'); temporary.write_bytes(z.read(i)); os.replace(temporary,dest)
state.mkdir(parents=True,exist_ok=True); temp=client.with_suffix('.tmp'); temp.write_text(json.dumps(desired),encoding='utf-8'); os.replace(temp,client)
launcher=state/'agent-knowledge.py'; temp=launcher.with_suffix('.tmp'); temp.write_text(p['launcher'],encoding='utf-8'); os.replace(temp,launcher)
print(json.dumps({'installed':True,'remote_root':str(root),'python':sys.version.split()[0]}))
'''


def install(params, job):
    host, root = target(params)
    package = Path(__file__).parent / 'project_memory'
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('toolbox/__init__.py', '')
        for path in sorted(package.rglob('*.py')):
            z.write(path, 'toolbox/project_memory/' + path.relative_to(package).as_posix())
    job.progress(10, '正在通过 SSH 安装独立项目记忆命令行')
    result = run(host, INSTALL, {'root': root, 'archive': base64.b64encode(archive.getvalue()).decode(), 'launcher': LAUNCHER}, job)
    job.progress(100, '远端项目记忆命令行已安装')
    return result


EXCHANGE = r'''
import json,pathlib,sys
sys.stdin.reconfigure(encoding='utf-8'); sys.stdout.reconfigure(encoding='utf-8')
p=json.load(sys.stdin); root=pathlib.Path(p['root']); sys.path.insert(0,str(root/'runtime'))
from toolbox.project_memory.store import MemoryStore
from toolbox.project_memory.cli import exchange
from toolbox.project_memory.principles import initialize_project_rules, save_rules
s=MemoryStore(root/'data')
try:
 result=exchange(s,p)
 if p.get('project_path'):
  project=p.get('project_id')
  if not project or project not in p.get('project_ids',[]): raise ValueError('Project must be selected before binding')
  result['location']=s.bind_project(project,p['project_path'])
  rules=initialize_project_rules(s,p['project_path'])
  if rules['initialized']:
   command='python3 "${XDG_STATE_HOME:-$HOME/.local/state}/wintoolbox-agent/agent-knowledge.py" recall --project .'
   save_rules(pathlib.Path(rules['path']),rules['text']+'\n- 远端无需安装个人技能；在项目目录执行 `'+command+'` 检索记忆；同一入口支持 capture、promote 和其他命令。',rules['hash'])
  result['rules_initialized']=bool(rules.get('initialized', False))
 print(json.dumps(result,ensure_ascii=False))
finally: s.close()
'''


def sync(params, store, job):
    from .project_memory_sync import blob_hashes, bucket
    host, root = target(params)
    project_ids = params.get('project_ids')
    if project_ids is not None and (not isinstance(project_ids, list) or any(not isinstance(p, str) for p in project_ids)):
        raise ValueError('project_ids 必须是项目 ID 数组')
    selected = set(project_ids) if project_ids is not None else {p['id'] for p in store.projects() if p.get('subscribed')}
    operations = [op for op in store.export_operations() if bucket(op) and
                  (op.get('entity_type') == 'project' or op.get('scope') == 'global' or op.get('project_id') in selected)]
    blobs = {h: base64.b64encode(store.get_blob(h)).decode() for h in blob_hashes(operations)}
    request = {'root': root, 'library_id': store.info()['library_id'], 'operations': operations,
               'known_ids': store.operation_ids(), 'blobs': blobs, 'project_ids': sorted(selected)}
    if params.get('project_path'):
        project_path = str(params['project_path'])
        target({'host': host, 'remote_root': project_path})
        if params.get('project_id') not in selected:
            raise ValueError('远端项目绑定必须选择对应项目')
        request.update(project_path=project_path, project_id=params['project_id'])
    job.progress(10, '正在交换远端离线记录')
    response = run(host, EXCHANGE, request, job)
    if response.get('library_id') != request['library_id']:
        raise ValueError('远端知识库标识不匹配，未导入数据')
    if response.get('missing_blobs'):
        raise ValueError('远端记录缺少附件，未导入；请先修复远端附件后重试')
    import hashlib
    for h, encoded in response.get('blobs', {}).items():
        body = base64.b64decode(encoded, validate=True)
        if hashlib.sha256(body).hexdigest() != h:
            raise ValueError('远端附件校验失败')
        store.add_blob(body)
    for h in blob_hashes(response['operations']):
        store.get_blob(h)
    accepted = store.ingest_operations(response['operations'])
    job.progress(100, '远端记录同步完成')
    return {'uploaded': len(operations), 'downloaded': accepted['accepted'], 'host': host}
