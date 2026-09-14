"""Protocol/lifetime checks with synthetic PCM; no real keys or personal recordings."""
import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from toolbox.app import App
from toolbox.live import CloudASR, LocalASR, LiveSession, Recorder


def until(predicate, timeout=5):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("condition did not become true")


@pytest.fixture
def live_app(tmp_path):
    events=[]
    app=App(tmp_path/"live-data",emit=events.append,register_features=False,register_live=True)
    app.settings.secret=lambda pid:"test-key-never-sent-to-network"
    app.providers.chat=lambda *a,**k:'{"translation":"测试译文","is_question":false}'
    yield app,events
    app.close()


class Socket:
    def __init__(self, realtime=False, reject=False, result_on_binary=None):
        self.queue=queue.Queue()
        self.sent=[]
        self.binary=[]
        self.closed=False
        self.realtime=realtime
        self.reject=reject
        self.result_on_binary=result_on_binary

    def send(self,text):
        msg=json.loads(text);self.sent.append(msg)
        action=msg.get("type") or msg.get("header",{}).get("action")
        if action in ("run-task","session.update"):
            if self.reject:
                self.queue.put({"header":{"event":"task-failed","error_message":"Model unavailable"}})
            else:
                self.queue.put({"type":"session.updated"} if self.realtime else {"header":{"event":"task-started"}})
        elif action=="input_audio_buffer.append":
            self.send_binary(msg["audio"])
        elif action=="finish-task":
            self.queue.put({"header":{"event":"result-generated"},"payload":{"output":{"sentence":{"sentence_id":1,"begin_time":200,"end_time":800,"text":"trailing words","sentence_end":True}}}})
            self.queue.put({"header":{"event":"task-finished"}})
        elif action=="session.finish":
            self.queue.put({"type":"conversation.item.input_audio_transcription.completed","item_id":"utterance","transcript":"Hello world"})
            self.queue.put({"type":"session.finished"})

    def send_binary(self,pcm):
        self.binary.append(pcm)
        if self.result_on_binary:
            self.result_on_binary(self)

    def recv(self):
        value=self.queue.get(timeout=5)
        if isinstance(value,Exception):
            raise value
        return json.dumps(value) if value is not None else ""

    def close(self):
        self.closed=True
        self.queue.put(None)


def start_cloud(monkeypatch,app,socket,model=None,purpose="meeting",source_language="auto"):
    if model:
        app.settings.update({"roles":{"live_asr":{"provider_id":"dashscope","model":model}}})
    import websocket
    monkeypatch.setattr(websocket,"create_connection",lambda *a,**k:socket)
    session=LiveSession(app,{"source":"system","record_audio":False,"auto_answer":False,"purpose":purpose,"source_language":source_language,"language":source_language})
    engine=CloudASR(session,"system")
    thread=threading.Thread(target=engine.run,daemon=True)
    session.threads.append(thread)
    engine.feed(b"\0\0"*16000,start=3)
    thread.start()
    return session,engine,thread


@pytest.mark.parametrize("purpose", ["meeting", "captions"])
@pytest.mark.parametrize("source_language", ["auto", "en"])
def test_caption_semantic_punctuation_and_language_without_changing_meetings(monkeypatch,live_app,purpose,source_language):
    app,_=live_app
    socket=Socket()
    session,engine,thread=start_cloud(monkeypatch,app,socket,purpose=purpose,source_language=source_language)
    until(lambda:bool(socket.binary))
    parameters=socket.sent[0]["payload"]["parameters"]
    if purpose == "captions":
        assert parameters["semantic_punctuation_enabled"] is True
        assert parameters["max_sentence_silence"] == 1300
        assert parameters.get('language_hints') == (['en'] if source_language == 'en' else None)
    else:
        assert "multi_threshold_mode_enabled" not in parameters
        assert "semantic_punctuation_enabled" not in parameters
        assert 'language_hints' not in parameters
        assert parameters["max_sentence_silence"] == 600
    session.stop()
    assert not thread.is_alive()


@pytest.mark.parametrize('source_language', ['auto', 'en'])
def test_qwen_realtime_language_maps_to_input_audio_transcription(monkeypatch, live_app, source_language):
    app, _ = live_app
    socket = Socket(realtime=True)
    session, _, thread = start_cloud(monkeypatch, app, socket, model='qwen3-asr-flash-realtime', purpose='captions', source_language=source_language)
    until(lambda: bool(socket.binary))
    assert socket.sent[0]['session']['input_audio_transcription'] == ({'language': 'en'} if source_language == 'en' else {})
    session.stop()
    assert not thread.is_alive()


def test_native_finish_drains_trailing_sentence_and_uses_capture_offset(monkeypatch,live_app):
    app,events=live_app
    socket=Socket()
    session,engine,thread=start_cloud(monkeypatch,app,socket)
    until(lambda:bool(socket.binary))
    session.stop()
    assert not thread.is_alive() and socket.closed
    result=list(session.segments.values())
    assert result[0]["text"]=="trailing words"
    assert result[0]["start"]==pytest.approx(3.2)
    assert result[0]["end"]==pytest.approx(3.8)
    run=socket.sent[0]["header"]["task_id"]
    finish=next(x["header"]["task_id"] for x in socket.sent if x.get("header",{}).get("action")=="finish-task")
    assert run==finish


def test_qwen_stash_and_vad_timestamps_and_session_finish(monkeypatch,live_app):
    app,events=live_app
    def on_audio(socket):
        socket.queue.put({"type":"input_audio_buffer.speech_started","item_id":"utterance","audio_start_ms":100})
        socket.queue.put({"type":"input_audio_buffer.speech_stopped","item_id":"utterance","audio_end_ms":700})
        socket.queue.put({"type":"conversation.item.input_audio_transcription.text","item_id":"utterance","text":"Hello","stash":" world"})
    socket=Socket(realtime=True,result_on_binary=on_audio)
    session,_,thread=start_cloud(monkeypatch,app,socket,"qwen3-asr-flash-realtime")
    until(lambda:any(e.get("type")=="live.segment" for e in events))
    preview=next(e for e in events if e.get("type")=="live.segment")
    assert preview["text"]=="Hello world" and not preview["final"]
    assert preview["start"]==pytest.approx(3.1)
    session.stop()
    result=list(session.segments.values())[0]
    assert result["final"] and result["end"]==pytest.approx(3.7)
    assert any(x.get("type")=="session.finish" for x in socket.sent)
    assert not thread.is_alive()


def test_explicit_provider_failure_stops_session_and_keeps_error(monkeypatch,live_app):
    app,events=live_app
    socket=Socket(reject=True)
    session,_,thread=start_cloud(monkeypatch,app,socket)
    until(lambda:session.metadata.get("ended_at"))
    assert session.stop_event.is_set()
    assert session.metadata["status"]=="error"
    assert "Model unavailable" in session.metadata["error"]
    assert socket.closed and not thread.is_alive()


def test_reconnect_discards_old_queue_and_backfills_without_answering(monkeypatch,live_app):
    import websocket
    app,events=live_app
    submitted=[]
    app.jobs.submit=lambda tool,params:submitted.append((tool,params)) or {"id":"recovery"}
    def drop(socket):
        socket.queue.put({"header":{"event":"result-generated"},"payload":{"output":{"sentence":{"sentence_id":1,"begin_time":0,"end_time":500,"text":"first words","sentence_end":True}}}})
        socket.queue.put(OSError("connection lost"))
    first=Socket(result_on_binary=drop)
    second=Socket()
    sockets=iter([first,second])
    monkeypatch.setattr(websocket,"create_connection",lambda *a,**k:next(sockets))
    session=LiveSession(app,{"source":"system","record_audio":True,"auto_answer":False})
    session.started=time.monotonic()-12
    engine=CloudASR(session,"system")
    original_status=session.status
    listening=[]
    def status(kind,message):
        original_status(kind,message)
        if kind=="reconnecting":
            engine.feed(b"\x01\0"*16000,start=10.5)
        if kind=="listening":
            listening.append(kind)
            if len(listening)==2:
                engine.feed(b"\x02\0"*16000,start=20)
    session.status=status
    engine.feed(b"\0\0"*16000,start=10)
    thread=threading.Thread(target=engine.run,daemon=True);session.threads.append(thread);thread.start()
    until(lambda:bool(second.binary),timeout=6)
    session.stop()
    assert second.binary==[b"\x02\0"*16000]
    assert submitted[0][0]=="live.backfill"
    assert submitted[0][1]["source"]=="system"
    assert submitted[0][1]["start"]==pytest.approx(8.5)
    assert any(s["start"]==pytest.approx(20.2) for s in session.segments.values())
    assert not session.answers


class LineStream:
    def __init__(self):
        self.lines=queue.Queue()
    def __iter__(self):
        while True:
            line=self.lines.get()
            if line is None:
                return
            yield line


class Child:
    def __init__(self,ready=True):
        self.stdout=LineStream();self.stderr=LineStream();self.dead=False
        self.stdin=SimpleNamespace(write=self.write,flush=lambda:None)
        if ready:
            self.stdout.lines.put('{"ready":true}\n')
    def write(self,line):
        self.stdout.lines.put('{"text":"local trailing text"}\n')
    def poll(self):
        return 0 if self.dead else None
    def kill(self):
        self.dead=True;self.stdout.lines.put(None);self.stderr.lines.put(None)
    def wait(self,timeout=None):
        return 0


@pytest.mark.parametrize("ready",[False,True])
def test_local_startup_cancel_is_prompt_and_final_buffer_drains(monkeypatch,live_app,ready):
    import toolbox.live as live
    app,events=live_app
    app.models.require=lambda *a,**k:{"python":"fixture.exe","path":"fixture-model"}
    child=Child(ready=ready)
    monkeypatch.setattr(live.subprocess,"Popen",lambda *a,**k:child)
    session=LiveSession(app,{"source":"microphone","engine":"faster-whisper","record_audio":False,"auto_answer":False})
    engine=LocalASR(session,"microphone")
    engine.feed(struct.pack("<h",4000)*4800,start=6)
    thread=threading.Thread(target=engine.run,daemon=True);session.threads.append(thread);thread.start()
    until(lambda:bool(session.children))
    if ready:
        until(lambda:engine.frames.empty())
    begin=time.monotonic();session.stop()
    assert time.monotonic()-begin<2
    assert child.dead and not thread.is_alive()
    if ready:
        result=list(session.segments.values())[0]
        assert result["text"]=="local trailing text"
        assert result["start"]==6 and result["end"]==pytest.approx(6.3)


def test_backfill_crosses_minute_boundary_applies_source_offset_and_keeps_other_source(live_app):
    app,events=live_app
    session=LiveSession(app,{"source":"both","record_audio":True})
    session.metadata["source_offsets"]={"system":5}
    session.segments={"mic":{"id":"mic","text":"same words","start":64,"end":65,"source":"microphone","final":True}}
    session.save()
    recorder=Recorder(session.root,"system")
    recorder.write(b"\0\0"*(61*16000));recorder.close()
    app.providers.transcribe=lambda path,**kwargs:[{"start":0,"end":kwargs["duration"],"text":"same words"}]
    from toolbox.media import parse_json
    def translation(messages,**kwargs):
        items=json.loads(messages[-1]["content"])
        return json.dumps({"segments":[{"id":s["id"],"translation":"相同文字"} for s in items]})
    app.providers.chat=translation
    job=app.jobs.submit("live.backfill",{"session_id":session.id,"source":"system","start":64,"end":66})
    until(lambda:app.jobs.get(job["id"])["status"] not in ("queued","running"))
    result=app.jobs.get(job["id"])
    assert result["status"]=="completed",result
    saved=json.loads((session.root/"session.json").read_text("utf-8"))
    recovered=[s for s in saved["segments"] if s.get("recovered")]
    # Adjacent identical statements and matching speech from the other source must both survive.
    assert len(recovered)==2 and recovered[0]["start"]==64 and recovered[1]["start"]==65
    assert any(s["source"]=="microphone" for s in saved["segments"])
    assert not saved["answers"]
    session.stop()


def test_duplicate_inflight_question_does_not_start_second_request(live_app):
    app,events=live_app
    session=LiveSession(app,{"source":"system","record_audio":False})
    entered=threading.Event();release=threading.Event();calls=[]
    def chat(*args,**kwargs):
        calls.append(1);entered.set();release.wait(2)
        return "answer"
    app.providers.chat=chat
    assert session.answer("Why?")["started"]
    assert entered.wait(1)
    assert not session.answer("Why?")["started"]
    release.set();until(lambda:bool(session.answers))
    assert len(calls)==1
    session.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows COM/WASAPI regression")
def test_cold_process_rpc_enumeration_and_capture_from_other_thread():
    # The child must not preload soundcard: that would hide the S_FALSE import bug.
    code = r'''
import json, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
from toolbox.app import App
assert "soundcard" not in sys.modules
with tempfile.TemporaryDirectory() as directory:
    app = App(directory, register_features=False, register_live=True)
    assert "soundcard" not in sys.modules
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(app.call, "live.devices").result(timeout=15)
        second = pool.submit(app.call, "live.devices").result(timeout=15)
    def sample_shape():
        from toolbox.live import _com_apartment
        with _com_apartment():
            import soundcard as sc
            speaker = sc.default_speaker()
            if speaker is None:
                return None
            loopback = sc.get_microphone(id=speaker.id, include_loopback=True)
            with loopback.recorder(samplerate=48000, blocksize=256) as recorder:
                samples = recorder.record(numframes=256)
                shape = list(samples.shape)
                del samples
                return shape
    with ThreadPoolExecutor(max_workers=1) as pool:
        shape = pool.submit(sample_shape).result(timeout=15)
    print(json.dumps({"first": len(first["devices"]), "second": len(second["devices"]), "shape": shape}))
    app.close()
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert result.returncode == 0, result.stderr
    assert "com_loaded" not in result.stderr
    assert "Exception ignored" not in result.stderr
    data = json.loads(result.stdout)
    assert data["first"] == data["second"]
    assert data["shape"] is None or data["shape"][0] == 256
