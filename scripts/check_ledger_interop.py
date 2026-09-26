"""Compare the Python and Android JVM ledger implementations using synthetic data.

Run after Android :app:compileDebugUnitTestJavaWithJavac. Never reads user data.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import uuid

from toolbox.ledger import Ledger, DEFAULT_PROJECT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--android-root', required=True, type=Path)
    parser.add_argument('--java', required=True)
    parser.add_argument('--json-jar', required=True)
    args = parser.parse_args()
    base = args.android_root / 'app/build/intermediates/javac'
    import os
    classpath = os.pathsep.join(map(str, [base/'debug/compileDebugJavaWithJavac/classes', base/'debugUnitTest/compileDebugUnitTestJavaWithJavac/classes', args.json_jar]))
    with tempfile.TemporaryDirectory(prefix='ledger-interop-') as temp:
        root = Path(temp)
        pc = Ledger(root/'pc')
        entry_id, project_id = uuid.uuid4().hex, uuid.uuid4().hex
        pc.patch('project', project_id, {'name': '互通测试', 'settlement_mode': 'general', 'archived': False, 'created_at': 0})
        pc.patch('entry', entry_id, {'title':'电脑原始记录','date':'2026-09-26','amount':'12.30','amount_minor':1230,
            'currency':'CNY','entry_type':'expense','status':'waiting','category':'交通','notes':'原始备注','project_id':project_id,'created_at':0})
        original = pc.export_operations()
        def android(operations, patches=()):
            (root/'input.json').write_text(json.dumps({'operations':operations,'patches':list(patches)},ensure_ascii=False),'utf-8')
            subprocess.run([args.java,'-cp',classpath,'net.lanbridge.android.LedgerFixture',str(root/'input.json'),str(root/'output.json')],check=True,capture_output=True)
            return json.loads((root/'output.json').read_text('utf-8'))
        def compare(result):
            pc.ingest(result['operations'])
            for row in result['entries']:
                ours = pc.get('entry',row['id'])
                for key in ('title','date','amount','amount_minor','currency','entry_type','status','category','notes','project_id'):
                    assert row[key] == ours[key], (key,row[key],ours[key])
                assert row['_heads'] == ours['heads']
                assert row['_conflicts'] == ours['conflicts'], (row['_conflicts'],ours['conflicts'])
            assert {r['id'] for r in result['entries']} == {r['id'] for r in pc.list_entities('entry')}
        compare(android(original))
        pc.patch('entry',entry_id,{'title':'电脑离线改名','amount':'11.01','amount_minor':1101})
        mobile = android(original,[{'entity_type':'entry','entity_id':entry_id,'changes':{'title':'手机离线改名','notes':'手机备注','amount':'22.02','amount_minor':2202}}])
        pc.ingest(mobile['operations'])
        compare(android(pc.export_operations()))
        item = pc.get('entry',entry_id)
        assert 'title' in item['conflicts'] and 'amount' in item['conflicts'] and item['notes']=='手机备注'
        pc.patch('entry',entry_id,{'title':'电脑离线改名','amount':'22.02','amount_minor':2202},parents=item['heads'])
        compare(android(pc.export_operations()))
        assert not pc.get('entry',entry_id)['conflicts']
        # Two devices independently change type/status; derived fields converge.
        before = pc.export_operations()
        pc.patch('entry',entry_id,{'entry_type':'income','status':'not_applicable'})
        mobile = android(before,[{'entity_type':'entry','entity_id':entry_id,'changes':{'status':'reimbursed'}}])
        pc.ingest(mobile['operations'])
        compare(android(pc.export_operations()))
        assert pc.get('entry',entry_id)['status']=='not_applicable'
        # Remote delete wins even over a stale offline edit.
        before = pc.export_operations()
        pc.patch('entry',entry_id,{},deleted=True)
        mobile = android(before,[{'entity_type':'entry','entity_id':entry_id,'changes':{'notes':'删除前离线修改'}}])
        pc.ingest(mobile['operations'])
        compare(android(pc.export_operations()))
        assert not pc.list_entities('entry')
        print(json.dumps({'ok':True,'checks':['Python→Java→Python','project','exact_amount','disjoint_merge','concurrent_conflicts','conflict_resolution','income_status','delete_vs_offline_edit']},ensure_ascii=False))


if __name__=='__main__':
    main()
