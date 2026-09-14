import hashlib
import json
import sys
from pathlib import Path


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    request = json.loads(sys.stdin.readline())
    path = Path(request["params"]["path"]).expanduser().resolve()
    if not path.is_file():
        raise ValueError("请选择存在的文件")
    size = path.stat().st_size
    hasher = hashlib.sha256()
    read = 0
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            hasher.update(block)
            read += len(block)
            emit({"type": "progress", "percent": read / max(1, size) * 99, "message": "正在计算 SHA-256"})
    emit({"type": "result", "result": {"path": str(path), "size": size, "algorithm": "SHA-256", "hash": hasher.hexdigest()}})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit({"type": "error", "message": str(exc)})
        sys.exit(1)
