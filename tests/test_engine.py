import json
import os
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from toolbox.app import App
from toolbox.jobs import Job, Cancelled
from toolbox.media import normalize_segments, parse_json, summarize, translate, probe, binary, atempo
from toolbox.providers import Providers, ProviderError
from toolbox.settings import Settings
from toolbox.storage import Storage
from toolbox import subtitles


@pytest.fixture
def app(tmp_path):
    instance=App(tmp_path/"data",register_features=False,register_live=False)
    yield instance
    instance.close()


def wait_job(app,id,timeout=15):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        record=app.jobs.get(id)
        if record["status"] not in ("queued","running","cancelling"):
            return record
        time.sleep(.03)
    raise TimeoutError(record)


def make_job(app,params=None):
    record={"id":"test-direct","params":params or {},"tool":"media","status":"running","progress":0,"message":"","created_at":"2026-01-01","artifacts":[]}
    return Job(app.jobs,record)


def wave_file(path,seconds=2):
    import math,struct
    with wave.open(str(path),"wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
        out.writeframes(b"".join(struct.pack("<h",int(3000*math.sin(i*2*math.pi*440/16000))) for i in range(int(16000*seconds))))
    return path


def test_defaults_are_nonsecret_and_updates_merge(app):
    initial=app.settings.get()
    assert initial["roles"]["transcribe"]["model"]=="qwen-audio-3.0-asr-flash"
    changed=app.settings.update({"preferences":{"theme":"dark"}})
    assert changed["preferences"]["record_audio"] is True
    assert not any("api_key" in p for p in changed["providers"])


@pytest.mark.skipif(os.name!="nt",reason="DPAPI is Windows-only")
def test_dpapi_roundtrip_and_mask_never_overwrites(app):
    p=app.settings.get()["providers"][0]
    p["api_key"]="engine-fixture-secret"
    response=app.settings.update({"providers":[p]})
    assert response["providers"][0]["has_key"]
    assert "engine-fixture-secret" not in app.settings.secret_path.read_text("utf-8")
    assert app.settings.secret("dashscope")=="engine-fixture-secret"
    p["api_key"]="********"
    with pytest.raises(ValueError):
        app.settings.update({"providers":[p]})
    assert app.settings.secret("dashscope")=="engine-fixture-secret"
    p["api_key"]=""
    app.settings.update({"providers":[p]})
    assert app.settings.secret("dashscope")=="engine-fixture-secret"
    app.settings.reload()
    assert app.settings.secret("dashscope")=="engine-fixture-secret"


def test_no_key_actionable_error(app):
    with pytest.raises(ProviderError,match="API 密钥"):
        app.providers.chat([{"role":"user","content":"hi"}])


def test_jobs_persist_cancel_retry_and_artifact(app,tmp_path):
    source=tmp_path/"output.txt"
    def runner(job):
        source.write_text("real output",encoding="utf-8")
        job.progress(50,"half")
        job.artifact(source,"text")
        return {"ok":True}
    app.jobs.register("fixture",runner)
    record=app.jobs.submit("fixture",{"value":1})
    record=wait_job(app,record["id"])
    assert record["status"]=="completed"
    assert record["artifacts"][0]["size"]==11
    retry=wait_job(app,app.jobs.retry(record["id"])["id"])
    assert retry["status"]=="completed" and retry["id"]!=record["id"]
    def cancellable(job):
        while True:
            job.check_cancelled()
            time.sleep(.01)
    active=app.jobs.submit("cancel",{},cancellable)
    app.jobs.cancel(active["id"])
    assert wait_job(app,active["id"])["status"]=="cancelled"


def test_checkpoint_content_and_parameter_invalidation(app,tmp_path):
    job=make_job(app)
    output=tmp_path/"step.json"
    count=[]
    def run():
        count.append(1)
        output.write_text(str(len(count)),encoding="utf-8")
        return {"value":len(count)},[output]
    assert job.checkpoint("a",{"input":"x"},{"model":"one"},run)["value"]==1
    assert job.checkpoint("a",{"input":"x"},{"model":"one"},run)["value"]==1
    output.write_text("corrupt",encoding="utf-8")
    assert job.checkpoint("a",{"input":"x"},{"model":"one"},run)["value"]==2
    assert job.checkpoint("a",{"input":"x"},{"model":"two"},run)["value"]==3
    assert job.checkpoint("a",{"input":"y"},{"model":"two"},run)["value"]==4


def test_subtitle_roundtrip_and_bad_times(tmp_path):
    segments=[{"id":"a","start":.12,"end":1.23,"text":"Hello","translation":"你好"},{"id":"b","start":1.3,"end":2,"text":"世界"}]
    for ext in ("srt","vtt","json"):
        path=tmp_path/f"中文字幕.{ext}"
        subtitles.save(path,segments)
        result=subtitles.load(path)["segments"]
        assert len(result)==2 and result[0]["start"]==.12
    with pytest.raises(ValueError):
        subtitles.validate([{"id":"bad","start":0,"end":float("nan"),"text":"bad"}])
    with pytest.raises(ValueError):
        subtitles.validate([{"id":"bad","start":1,"end":0,"text":"bad"}])


def test_normalize_silence_and_boundaries():
    assert normalize_segments([{"start":0,"end":1,"text":"  "}],2)==[]
    result=normalize_segments([{"start":-.2,"end":5,"text":"hi"}],2)
    assert result[0]["start"]==0 and result[0]["end"]==2


class StubSettings:
    def __init__(self,kind="openai"):
        self.kind=kind
    def get(self):
        return {"providers":[{"id":"test","kind":self.kind,"base_url":"https://fixture.test/v1"}],"roles":{r:{"provider_id":"test","model":"fixture-model"} for r in ("chat","vision","embedding","transcribe","tts")}}
    def secret(self,pid):
        return "unit-test-key"


def test_openai_stream_and_embed():
    def handler(request):
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200,json={"data":[{"index":1,"embedding":[0,1]},{"index":0,"embedding":[1,0]}]})
        return httpx.Response(200,headers={"content-type":"text/event-stream"},text='data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: {"choices":[{"delta":{"content":" world"}}]}\n\ndata: [DONE]\n\n')
    provider=Providers(StubSettings(),httpx.Client(transport=httpx.MockTransport(handler)))
    parts=[]
    assert provider.chat([{"role":"user","content":"hello"}],stream_callback=parts.append)=="Hello world"
    assert parts==["Hello"," world"]
    assert provider.embed(["a","b"])==[[1,0],[0,1]]


def test_api_usage_persists_response_tokens_and_audio_without_content(tmp_path):
    storage = Storage(tmp_path / "usage-data")
    def handler(request):
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "safe result"}}],
                                             "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}})
        raise AssertionError(request.url)
    provider = Providers(StubSettings(), httpx.Client(transport=httpx.MockTransport(handler)), usage=storage)
    assert provider.chat([{"role": "user", "content": "private prompt sentinel"}]) == "safe result"
    storage.record_usage("test", "fixture-asr", "live_asr", "realtime_asr", audio_seconds=12.5, latency_ms=500)
    summary = storage.usage_summary("test")
    assert summary["periods"]["today"]["requests"] == 2
    assert summary["periods"]["today"]["total_tokens"] == 18
    assert summary["periods"]["today"]["audio_seconds"] == 12.5
    assert summary["periods"]["today"]["token_reported_requests"] == 1
    with storage.connect() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(api_usage)")}
        values = db.execute("SELECT provider_id,model,role,operation,status FROM api_usage").fetchall()
    assert "prompt" not in columns and "response" not in columns
    assert all("private prompt sentinel" not in str(row) and "safe result" not in str(row) for row in values)


def test_dashscope_stream_usage_chunk_is_counted(tmp_path):
    storage = Storage(tmp_path / "usage-data")
    def handler(request):
        payload = json.loads(request.content)
        assert payload["stream_options"] == {"include_usage": True}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            text='data: {"choices":[{"delta":{"content":"Hello"}}],"usage":null}\n\ndata: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\ndata: [DONE]\n\n')
    provider = Providers(StubSettings("dashscope"), httpx.Client(transport=httpx.MockTransport(handler)), usage=storage)
    assert provider.chat([{"role": "user", "content": "hello"}], stream_callback=lambda _: None) == "Hello"
    summary = storage.usage_summary("test")["periods"]["all"]
    assert summary["requests"] == 1 and summary["total_tokens"] == 7 and summary["token_reported_requests"] == 1


def test_provider_errors_do_not_echo_headers():
    provider=Providers(StubSettings(),httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(401,json={"error":{"message":"Invalid API key"}}))))
    with pytest.raises(ProviderError) as error:
        provider.chat([{"role":"user","content":"hello"}])
    assert "401" in str(error.value) and "unit-test-key" not in str(error.value)


def test_dashscope_asr_full_text_preserved(tmp_path):
    wav=wave_file(tmp_path/"audio.wav")
    body={"output":{"text":"First sentence. Second sentence.","sentence":{"sentence_id":2,"sentence_end":True,"begin_time":1000,"end_time":2000,"text":"Second sentence."}}}
    settings=StubSettings("dashscope")
    original=settings.get
    settings.get=lambda:{**original(),"roles":{**original()["roles"],"transcribe":{"provider_id":"test","model":"qwen-audio-3.0-asr-flash"}}}
    provider=Providers(settings,httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=body))))
    result=provider.transcribe(wav,duration=2)
    assert result[0]["text"]==body["output"]["text"]
    assert result[0]["timestamp_source"]=="chunk"


def test_translation_requires_exact_ids(app):
    job=make_job(app)
    segments=[{"id":"a","start":0,"end":1,"text":"hello"},{"id":"b","start":1,"end":2,"text":"world"}]
    app.providers.chat=lambda *a,**k:'{"segments":[{"id":"a","translation":"你好"}]}'
    with pytest.raises(RuntimeError,match="校验失败"):
        translate(app,job,segments)
    assert "translation" not in segments[0]
    app.providers.chat=lambda *a,**k:'{"segments":[{"id":"b","translation":"世界"},{"id":"a","translation":"你好"}]}'
    result=translate(app,job,segments)
    assert [s["translation"] for s in result]==["你好","世界"]


def test_long_summary_covers_entire_tail(app):
    calls=[]
    def chat(messages,**kwargs):
        text=messages[-1]["content"]
        calls.append(text)
        return "notes " + ("TAIL_SENTINEL" if "TAIL_SENTINEL" in text else "part")
    app.providers.chat=chat
    result=summarize(app,make_job(app),[{"id":"a","start":0,"end":60,"text":"x"*40000+"TAIL_SENTINEL"}],mode="course")
    assert any("TAIL_SENTINEL" in text for text in calls)
    assert "TAIL_SENTINEL" in result
    assert len(calls)>4


def test_media_pipeline_real_ffmpeg_and_checkpoint(app,tmp_path):
    wav=wave_file(tmp_path/"中文 input.wav")
    calls=[]
    def asr(path,**kwargs):
        calls.append(path)
        assert probe(path)["has_audio"]
        return [{"start":0,"end":1,"text":"hello"}]
    app.providers.transcribe=asr
    params={"paths":[str(wav)],"output_dir":str(tmp_path/"output"),"engine":"api","translate":False}
    record=wait_job(app,app.jobs.submit("media",params)["id"],timeout=30)
    assert record["status"]=="completed",record
    assert any(Path(a["path"]).suffix==".srt" for a in record["artifacts"])
    assert len(calls)==1
    again=wait_job(app,app.jobs.submit("media",params)["id"],timeout=30)
    assert again["status"]=="completed"
    assert len(calls)==1
    params["output_dir"]=str(tmp_path/"other-output")
    different=wait_job(app,app.jobs.submit("media",params)["id"],timeout=30)
    assert different["status"]=="completed",different
    assert len(calls)==2


def test_cancel_process(app):
    job=make_job(app)
    timer=threading.Timer(.25,job.cancel_event.set)
    timer.start()
    started=time.monotonic()
    with pytest.raises(Cancelled):
        job.run_process([sys.executable,"-c","import time; time.sleep(20)"])
    assert time.monotonic()-started<5


def test_dispatch_unknown_and_maintenance(app):
    with pytest.raises(LookupError):
        app.call("missing.method")
    app.maintenance=True
    with pytest.raises(RuntimeError,match="恢复"):
        app.call("settings.update",{"settings":{}})
    assert app.call("app.info")["name"]=="WinToolbox"


def test_unsafe_runtime_zip_rejected(app,tmp_path):
    import zipfile
    archive=tmp_path/"bad.zip"
    with zipfile.ZipFile(archive,"w") as package:
        package.writestr("../escape.txt","bad")
    with pytest.raises(ValueError):
        app.models.extract(archive,tmp_path/"extract")


def test_atempo_chain():
    assert atempo(8)=="atempo=2,atempo=2,atempo=2.000000"
    assert atempo(.25)=="atempo=0.5,atempo=0.500000"


def test_video_burn_and_dub_produce_playable_outputs(app,tmp_path):
    video=tmp_path/"视频 sample.mp4"
    subprocess.run([binary("ffmpeg"),"-hide_banner","-loglevel","error","-f","lavfi","-i","color=c=blue:s=320x180:d=2","-f","lavfi","-i","sine=frequency=440:duration=2","-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac","-shortest",str(video)],check=True,capture_output=True)
    app.providers.transcribe=lambda *a,**k:[{"start":.1,"end":1.5,"text":"Hello world"}]
    app.providers.tts=lambda text,path,voice: str(wave_file(Path(path),seconds=.7))
    result=wait_job(app,app.jobs.submit("media",{"paths":[str(video)],"engine":"api","dub":True,"burn":True,"tts_provider":"qwen"})["id"],timeout=30)
    assert result["status"]=="completed",result
    videos=[a for a in result["artifacts"] if a["kind"]=="video"]
    assert len(videos)==2
    for artifact in videos:
        metadata=probe(artifact["path"])
        assert metadata["has_audio"] and metadata["has_video"]
        assert abs(metadata["duration"]-2)<.3


def test_backup_password_cannot_enter_job_params(app):
    with pytest.raises(ValueError,match="密码"):
        app.jobs.submit("backup-export",{"password":"sensitive"})


def test_restore_rejects_active_live(app):
    app.live_holder={"session":SimpleNamespace(stop_event=threading.Event())}
    with pytest.raises(RuntimeError,match="实时会话"):
        app.before_restore("fake-id")
    app.live_holder["session"].stop_event.set()
