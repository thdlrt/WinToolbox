import json
import math
import re
from pathlib import Path

from .settings import atomic_json


def validate(segments):
    result, ids = [], set()
    for index, original in enumerate(segments):
        segment = dict(original)
        segment["id"] = str(segment.get("id", f"s{index+1:06d}"))
        if segment["id"] in ids:
            raise ValueError("字幕片段 ID 重复")
        ids.add(segment["id"])
        start, end = float(segment["start"]), float(segment["end"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError(f"字幕 {segment['id']} 时间范围无效")
        segment.update(start=round(start, 3), end=round(end, 3), text=str(segment.get("text", "")))
        result.append(segment)
    return sorted(result, key=lambda s: (s["start"], s["end"]))


def stamp(seconds, sep=","):
    total = round(seconds*1000)
    hours, remain = divmod(total, 3600000)
    minutes, remain = divmod(remain, 60000)
    sec, ms = divmod(remain, 1000)
    return f"{hours:02d}:{minutes:02d}:{sec:02d}{sep}{ms:03d}"


def text_for(segment, bilingual=True):
    text = segment.get("text", "")
    if segment.get("speaker"):
        text = f"[{segment['speaker']}] {text}"
    translation = segment.get("translation", "").strip()
    if translation:
        text = text + "\n" + translation if bilingual else translation
    return text


def save(path, segments, bilingual=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    segments = validate(segments)
    ext = path.suffix.lower()
    if ext == ".json":
        atomic_json(path, {"schema": 1, "segments": segments})
        return str(path)
    if ext == ".txt":
        text = "\n\n".join(text_for(s, bilingual) for s in segments)
    elif ext in (".srt", ".vtt"):
        sep = "." if ext == ".vtt" else ","
        text = ("WEBVTT\n\n" if ext == ".vtt" else "") + "\n\n".join(f"{i+1}\n{stamp(s['start'],sep)} --> {stamp(s['end'],sep)}\n{text_for(s,bilingual)}" for i,s in enumerate(segments)) + "\n"
    else:
        raise ValueError("字幕支持 JSON、SRT、VTT 和 TXT 导出")
    temp = path.with_suffix(ext + ".tmp")
    temp.write_text(text if text else ("（未检测到语音）\n" if ext == ".txt" else ""), encoding="utf-8")
    temp.replace(path)
    return str(path)


def load(path):
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text("utf-8-sig"))
        return {"segments": validate(data if isinstance(data,list) else data["segments"])}
    text = path.read_text("utf-8-sig").replace("\r\n", "\n")
    pattern = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{3})")
    def seconds(value):
        match = pattern.search(value)
        if not match:
            raise ValueError("字幕时间格式无效")
        h,m,s,ms = match.groups()
        return int(h or 0)*3600+int(m)*60+int(s)+int(ms)/1000
    segments = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        for i,line in enumerate(lines):
            if "-->" in line:
                a,b = line.split("-->",1)
                segments.append({"id": f"s{len(segments)+1:06d}", "start": seconds(a), "end": seconds(b), "text": "\n".join(lines[i+1:])})
                break
    if not segments and text.strip() not in ("", "WEBVTT"):
        raise ValueError("文件没有可编辑的时间轴；请打开 SRT、VTT 或 JSON 字幕")
    return {"segments": validate(segments)}
