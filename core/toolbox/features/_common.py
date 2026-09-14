import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, temp = tempfile.mkstemp(prefix=".toolbox-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def child_path(root, relative):
    raw = str(relative)
    if "\\" in raw or ":" in raw or "\x00" in raw:
        raise ValueError("包中包含非法路径")
    parts = PurePosixPath(raw).parts
    if not parts or raw.startswith("/") or any(p in (".", "..") for p in parts):
        raise ValueError("包中包含越界路径")
    root = Path(root).resolve()
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        raise ValueError("包中包含越界路径")
    return target


def extract_zip(archive, target, *, max_bytes=8 * 1024**3, max_files=20000, check_cancel=None):
    items = archive.infolist()
    if len(items) > max_files or sum(x.file_size for x in items) > max_bytes:
        raise ValueError("压缩包超出允许大小或文件数量")
    seen = set()
    checked = []
    for item in items:
        if check_cancel:
            check_cancel()
        # ZipInfo.filename normalizes backslashes on Windows and truncates NULs.
        # Validate the original archive name before either transformation.
        original_name = item.orig_filename
        output = child_path(target, original_name)
        name = str(output).casefold()
        if name in seen or stat.S_ISLNK(item.external_attr >> 16):
            raise ValueError("压缩包存在重复路径或符号链接")
        if any(part.endswith((" ", ".")) or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part, re.I)
               for part in PurePosixPath(original_name).parts):
            raise ValueError("压缩包包含 Windows 保留文件名")
        seen.add(name)
        checked.append((item, output))
    for item, output in checked:
        if check_cancel:
            check_cancel()
        if item.is_dir():
            output.mkdir(parents=True, exist_ok=True)
            continue
        output.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(item) as source, output.open("xb") as dest:
            count = 0
            while block := source.read(1024 * 1024):
                if check_cancel:
                    check_cancel()
                count += len(block)
                if count > item.file_size:
                    raise ValueError("压缩包文件大小不一致")
                dest.write(block)


def parse_json_reply(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)
