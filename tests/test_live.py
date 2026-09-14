import json
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
from toolbox.live import Recorder, LiveSession, register, _role


class Providers:
    def chat(self, messages, role='chat', stream_callback=None, cancel=None):
        if cancel:
            cancel()
        if role == 'translate':
            return json.dumps({'translation':'为什么增长？','is_question':True})
        result = '【English answer】Growth increased because of adoption.\n【中文参考】采用率提高。'
        if stream_callback:
            stream_callback(result)
        return result


class App:
    def __init__(self, path):
        self.data_dir = Path(path)
        self.events = []
        self.handlers = {}
        self.settings = SimpleNamespace(get=lambda:{'providers':[],'roles':{}}, secret=lambda _:None)
        self.providers = Providers()
        self.jobs = SimpleNamespace(register=lambda name, handler:None)
    def emit(self, type, **data): self.events.append({'type':type,**data})
    def register(self, method, handler): self.handlers[method] = handler
    def call(self, method, params): return self.handlers[method](params)


class LiveTests(unittest.TestCase):
    def test_answers_use_selected_preindexed_library_and_keep_sources(self):
        with tempfile.TemporaryDirectory() as d:
            app = App(d)
            queries, prompts = [], []
            def search(params):
                queries.append(params)
                return {'results': [{'id': 'S1', 'text': 'Growth was 12 percent.', 'document': 'Report'}]}
            app.register('knowledge.search', search)
            def chat(messages, **kwargs):
                prompts.append(messages)
                return 'Growth was 12 percent. [S1]'
            app.providers.chat = chat
            session = LiveSession(app, {'collection_id': 'prepared-library', 'record_audio': False})
            session.answer('What was the growth?')
            deadline = time.monotonic() + 3
            while not session.answers and time.monotonic() < deadline:
                time.sleep(.01)
            session.stop()
            self.assertEqual(queries, [{'collection_id': 'prepared-library', 'query': 'What was the growth?'}])
            self.assertIn('Growth was 12 percent.', prompts[0][1]['content'])
            self.assertEqual(session.answers[0]['sources'][0]['id'], 'S1')
            self.assertEqual(session.metadata['collection_id'], 'prepared-library')

    def test_recording_chunks_are_independently_readable(self):
        with tempfile.TemporaryDirectory() as d:
            r = Recorder(Path(d), 'system')
            r.write(b'\0\0' * 16000 * 60)
            r.write(b'\0\0' * 16000)
            r.close()
            files = sorted(Path(d).glob('*.wav'))
            self.assertEqual(len(files), 2)
            for p in files:
                with wave.open(str(p)) as w:
                    self.assertEqual(w.getframerate(), 16000)
                    self.assertGreater(w.getnframes(), 0)

    def test_final_dedup_translation_answer_and_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            app = App(d)
            session = LiveSession(app, {'source':'system','auto_answer':True})
            session.segment('one','Why did growth increase?',True,'system',0,2)
            session.segment('one','Why did growth increase?',True,'system',0,2)
            deadline = time.monotonic()+3
            while not session.answers and time.monotonic()<deadline: time.sleep(.01)
            session.stop()
            self.assertEqual(len(session.answers), 1)
            self.assertEqual(len(session.segments), 1)
            self.assertEqual(session.segments['one']['translation'], '为什么增长？')
            register(app)
            history=app.call('live.history',{})['sessions']
            self.assertEqual(history[0]['status'],'stopped')
            self.assertEqual(app.call('live.get',{'id':session.id})['answers'][0]['question'],'Why did growth increase?')
            with self.assertRaises(ValueError): app.call('live.get',{'id':'../../outside'})

    def test_own_microphone_is_context_in_online_dual_mode(self):
        with tempfile.TemporaryDirectory() as d:
            app=App(d);session=LiveSession(app,{'source':'both','auto_answer':True})
            session.segment('own','Why did growth increase?',True,'microphone',0,2)
            session.translate_pool.shutdown(wait=True)
            self.assertFalse(session.answers)
            session.stop()

    def test_missing_api_key_is_actionable(self):
        with tempfile.TemporaryDirectory() as d:
            app=App(d)
            with self.assertRaisesRegex(ValueError,'配置'): _role(app,'live_asr')

    def test_manual_answer_cancellation_stops_stream(self):
        with tempfile.TemporaryDirectory() as d:
            app=App(d);session=LiveSession(app,{})
            with self.assertRaises(ValueError): session.answer()
            session.stop()


if __name__=='__main__': unittest.main()
