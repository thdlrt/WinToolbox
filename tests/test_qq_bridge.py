import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from toolbox.features.qq_bridge import QQBridge, validate

THREAD = '01a17c71-92fd-7950-880c-f07dda53d55f'


class Inline:
    def submit(self, fn, *args, **kwargs): return fn(*args, **kwargs)
    def shutdown(self, **kwargs): pass


class QQBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bridge = QQBridge(SimpleNamespace(data_dir=self.root), seal=lambda s, **kw: 'SEALED' if not kw else s)
        self.bridge.workers.shutdown(wait=True)
        self.bridge.workers = Inline()
        self.bridge.config = validate({'enabled': True, 'thread_id': THREAD})
        self.bridge.identity = {'app_id': 'newbot', 'owner_id': 'owner'}
        self.sent = []
        self.bridge._command = lambda op, **p: self.sent.append((op, p)) or {'ok': True, 'message_ids': ['sent-id']}
        self.bridge.connected = True
        self.dispatched = []
        self.bridge._dispatch = self.dispatched.append

    def tearDown(self):
        self.bridge.close()
        self.bridge.db.close()
        self.temp.cleanup()

    def event(self, **kw):
        return {'message_id': 'm1', 'sender_id': 'owner', 'kind': 'c2c', 'text': '你好', **kw}

    def test_owner_and_deduplication(self):
        self.bridge.receive(self.event(sender_id='stranger'))
        self.bridge.receive(self.event()); self.bridge.receive(self.event())
        self.assertEqual(self.dispatched, ['m1'])
        self.assertNotIn('token', json.dumps(self.bridge.status()['recent']))

    def test_pair_requires_private_exact_code_before_deadline(self):
        self.bridge.identity['owner_id'] = ''
        self.bridge.pair()
        text = '绑定 ' + self.bridge.pair_code
        self.bridge.receive(self.event(kind='group', text=text))
        self.assertFalse(self.bridge.identity['owner_id'])
        self.bridge.receive(self.event(sender_id='actual-owner', text=text))
        self.assertEqual(self.bridge.identity['owner_id'], 'actual-owner')
        self.assertEqual(len(self.sent), 1)

    def test_group_requires_explicit_allowlist_owner_and_mention(self):
        self.bridge.receive(self.event(kind='group', group_id='g1', mentioned=True))
        self.assertFalse(self.dispatched)
        self.bridge.receive(self.event(kind='group', group_id='g1', mentioned=True, text='/接入本群'))
        self.bridge.receive(self.event(kind='group', group_id='g1', mentioned=False))
        self.assertFalse(self.dispatched)
        self.bridge.receive(self.event(kind='group', group_id='g1', mentioned=True))
        self.assertEqual(self.dispatched, ['m1'])

    def token(self):
        self.bridge.receive(self.event())
        return self.bridge.db.execute('SELECT token FROM incoming').fetchone()[0]

    def test_reply_idempotent_and_files_real(self):
        file = self.root / 'emoji.png'; file.write_bytes(b'fixture')
        params = {'reply_token': self.token(), 'request_id': THREAD, 'files': [str(file)]}
        self.assertTrue(self.bridge.reply(params)['ok'])
        self.assertTrue(self.bridge.reply(params)['ok'])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]['files'], [str(file)])
        with self.assertRaises(ValueError): self.bridge.reply({**params, 'text': 'changed'})

    def test_unknown_outcome_never_retried(self):
        params = {'reply_token': self.token(), 'request_id': THREAD, 'text': 'hi'}
        def fail(*a, **k): raise TimeoutError()
        self.bridge._command = fail
        with self.assertRaises(TimeoutError): self.bridge.reply(params)
        self.assertEqual(self.bridge.receipt({'request_id': THREAD})['status'], 'unknown')
        with self.assertRaises(ValueError): self.bridge.reply(params)

    def test_changed_owner_blocks_stale_reply(self):
        token = self.token(); self.bridge.identity['owner_id'] = 'other'
        with self.assertRaises(ValueError): self.bridge.reply({'reply_token': token, 'request_id': THREAD, 'text': 'secret'})
        self.assertFalse(self.sent)

    def test_dsh_bot_never_started(self):
        prefs = self.root / '.dsh-qqbot'; prefs.mkdir()
        (prefs / 'model-prefs.json').write_text(json.dumps({'sessionIds': {'qqbot:1906035432:c2c:owner': 'session'}}))
        self.bridge.binding = True
        with patch('toolbox.features.qq_bridge.Path.home', return_value=self.root):
            self.bridge._event({'event': 'credentials', 'credentials': [{'appId': '1906035432', 'appSecret': 'do-not-use'}]})
        self.assertFalse(self.sent)
        self.assertIn('独立', self.bridge.error)
        self.assertNotIn('do-not-use', json.dumps(self.bridge.status()))

    def test_dispatch_uses_bound_thread_and_source_cli(self):
        self.token(); del self.bridge._dispatch
        client = SimpleNamespace(tool=lambda name, params: self.sent.append((name, params)) or {}, close=lambda: None)
        with patch('toolbox.features.qq_bridge.connect_codex', return_value=client): self.bridge._dispatch('m1')
        name, params = self.sent[0]
        self.assertEqual(name, 'send_message_to_thread')
        self.assertEqual(params['threadId'], THREAD)
        self.assertIn('qq.bridge.reply', params['prompt'])
        self.assertEqual(self.bridge.status()['recent'][0]['status'], 'delivered')

    def test_defaults_not_shared_and_invalid_config(self):
        a = validate({}); a['groups'].append('x')
        self.assertEqual(validate({})['groups'], [])
        with self.assertRaises(ValueError): validate({'enabled': True})
        with self.assertRaises(ValueError): validate({'groups': ['../bad']})


if __name__ == '__main__': unittest.main()
