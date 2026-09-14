"""Executed only by an isolated optional runtime. Request and result are file IPC."""
import json
import os
import re
import sys
from pathlib import Path


def group_words(words, duration=6, chars=90):
    segments, current = [], None
    for word in words:
        text = word.get("text", "")
        if not text.strip():
            continue
        if current and (word["end"] - current["start"] > duration or len(current["text"])+len(text) > chars or word["start"]-current["end"] > .8):
            segments.append(current)
            current = None
        if current is None:
            current = {"start":float(word["start"]),"end":float(word["end"]),"text":text}
        else:
            current["text"] += text
            current["end"] = float(word["end"])
        if re.search(r"[。！？.!?]$", text.strip()) and current["end"]-current["start"] > 1:
            segments.append(current)
            current = None
    if current:
        segments.append(current)
    return segments


def run(request):
    engine = request["engine"]
    audio = request.get("audio")
    model = request["model_path"]
    device = request.get("device", "auto")
    if engine == "faster-whisper":
        # CUDA wheels bundle DLLs but Windows DLL search does not discover them automatically.
        import site
        dll_handles = []
        if os.name == "nt":
            for site_path in site.getsitepackages():
                for directory in Path(site_path).glob("nvidia/*/bin"):
                    dll_handles.append(os.add_dll_directory(str(directory)))
        import ctranslate2
        from faster_whisper import WhisperModel
        if device == "auto":
            device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
        whisper = WhisperModel(model, device=device, compute_type=request.get("compute_type", "float16" if device == "cuda" else "int8"), local_files_only=True)
        language = request.get("language")
        segments, info = whisper.transcribe(audio, language=None if language in (None,"auto","") else language, vad_filter=True, word_timestamps=True, beam_size=5, condition_on_previous_text=False)
        result = []
        for segment in segments:
            if segment.words:
                result.extend(group_words([{"text":w.word,"start":w.start,"end":w.end} for w in segment.words]))
            else:
                result.append({"start":segment.start,"end":segment.end,"text":segment.text})
        return {"segments":result,"language":info.language}
    if engine == "qwen-asr":
        import torch
        from qwen_asr import Qwen3ASRModel
        if device == "auto":
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
        dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
        model_obj = Qwen3ASRModel.from_pretrained(model, dtype=dtype, device_map=device, max_inference_batch_size=1, max_new_tokens=2048,
            forced_aligner=str(Path(model)/"aligner"), forced_aligner_kwargs={"dtype":dtype,"device_map":device})
        language = request.get("language")
        language = {"en":"English","zh":"Chinese","ja":"Japanese","ko":"Korean","auto":None}.get(language,language)
        results = model_obj.transcribe(audio=audio, language=language, return_time_stamps=True)
        words = [{"start":w.start_time,"end":w.end_time,"text":w.text+(' ' if r.language == 'English' else '')} for r in results for w in (r.time_stamps or [])]
        return {"segments":group_words(words),"language":results[0].language if results else None}
    if engine == "sensevoice":
        import torch
        from funasr import AutoModel
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
        import soundfile as sf
        if device == "auto":
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
        model_obj = AutoModel(model=model, device=device, disable_update=True, trust_remote_code=True)
        # Load once for the entire file. Chunk timestamps are explicitly marked, not invented word times.
        segments=[]
        with sf.SoundFile(audio) as stream:
            sr=stream.samplerate
            offset=0
            while True:
                samples=stream.read(sr*15,dtype="float32",always_2d=False)
                if not len(samples):
                    break
                results=model_obj.generate(input=samples,cache={},language=request.get("language") or "auto",use_itn=True,batch_size_s=60,fs=sr)
                text="".join(rich_transcription_postprocess(r.get("text","")) for r in results)
                length=len(samples)/sr
                if text.strip():
                    segments.append({"start":offset,"end":offset+length,"text":text,"timestamp_source":"chunk"})
                offset+=length
        return {"segments":segments}
    if engine == "pyannote":
        import torch
        import soundfile as sf
        from pyannote.audio import Pipeline
        waveform,sr = sf.read(audio, dtype="float32", always_2d=True)
        pipe = Pipeline.from_pretrained(model, token=os.getenv("HF_TOKEN"))
        if torch.cuda.is_available() and device != "cpu":
            pipe.to(torch.device("cuda"))
        result = pipe({"waveform":torch.from_numpy(waveform.T),"sample_rate":sr})
        diarization = getattr(result,"speaker_diarization",result)
        return {"turns":[{"start":turn.start,"end":turn.end,"speaker":speaker} for turn,_,speaker in diarization.itertracks(yield_label=True)]}
    if engine == "demucs":
        import torch
        import torchaudio
        import soundfile as sf
        def save(filepath, src, sample_rate, **kwargs):
            sf.write(filepath, src.detach().cpu().numpy().T, sample_rate)
        torchaudio.save = save
        from demucs.separate import main
        main(["--two-stems","vocals","-n","htdemucs","-o",request["output_dir"],audio])
        return {"background":str(Path(request["output_dir"])/"htdemucs"/Path(audio).stem/"no_vocals.wav")}
    if engine == "cosyvoice":
        sys.path[:0] = [request["source"], request["matcha"]]
        import torch
        import soundfile as sf
        from cosyvoice.cli.cosyvoice import AutoModel
        obj = AutoModel(model_dir=model)
        reference = request.get("reference_audio")
        if reference:
            if request.get("reference_text"):
                chunks = obj.inference_zero_shot(request["text"],request["reference_text"],reference,stream=False,text_frontend=False)
            else:
                chunks = obj.inference_cross_lingual(request["text"],reference,stream=False,text_frontend=False)
        else:
            speakers = obj.list_available_spks()
            if not speakers:
                raise ValueError("此 CosyVoice 模型需要参考音频；请在高级选项中选择一段清晰的人声录音")
            chunks = obj.inference_sft(request["text"],request.get("voice") or speakers[0],stream=False,text_frontend=False)
        values = [c["tts_speech"] for c in chunks]
        if not values:
            raise RuntimeError("CosyVoice 未产生语音")
        sf.write(request["output_audio"],torch.cat(values,dim=1).squeeze(0).cpu().numpy(),obj.sample_rate)
        return {"audio":request["output_audio"]}
    raise ValueError(f"未知运行引擎：{engine}")


if __name__ == "__main__":
    if hasattr(sys.stdout,"reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    request = json.loads(Path(sys.argv[1]).read_text("utf-8"))
    result = run(request)
    target = Path(request["output"])
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(result,ensure_ascii=False),encoding="utf-8")
    temporary.replace(target)
