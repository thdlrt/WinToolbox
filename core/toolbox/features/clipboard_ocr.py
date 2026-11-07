"""Explicit, local clipboard OCR. Image and text stay out of job records."""
import io
import json
import os
import re
from pathlib import Path

from .clipboard_history import MAX_BYTES, WindowsClipboard


def clipboard(app):
    adapter = getattr(getattr(app, 'clipboard_history', None), 'adapter', None)
    if adapter is not None:
        return adapter
    if os.name != 'nt':
        raise RuntimeError('剪贴板 OCR 仅支持 Windows 桌面')
    return WindowsClipboard()


def recognize(job, payload):
    from PIL import Image
    if len(payload) > MAX_BYTES:
        raise ValueError('剪贴板图片超过 32 MB，请先缩小图片')
    image_path, result_path = job.work_dir / 'ocr-input.png', job.work_dir / 'ocr-result.json'
    try:
        with Image.open(io.BytesIO(payload)) as source:
            if source.width * source.height > 25_000_000:
                raise ValueError('图片超过 2500 万像素，请先缩小图片')
            # DIB/PNG, palette and transparent screenshots share an opaque input.
            rgba = source.convert('RGBA')
            image = Image.new('RGB', rgba.size, 'white')
            image.paste(rgba, mask=rgba.getchannel('A'))
            image.save(image_path, 'PNG')
        job.check_cancelled()
        script = Path(__file__).with_name('quick_assets') / 'clipboard_ocr.ps1'
        powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        try:
            job.run_process([powershell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                             '-File', script, '-InputPath', image_path, '-ResultPath', result_path],
                            timeout=60, max_output_bytes=4000)
        except RuntimeError as exc:
            if 'OCR_LANGUAGE_MISSING' in str(exc):
                raise RuntimeError('请在 Windows 设置 → 时间和语言 → 语言和区域 → 语言选项中安装中文或英文的光学字符识别组件') from None
            raise
        result = json.loads(result_path.read_text('utf-8-sig'))
        text = result.get('text')
        if not isinstance(text, str):
            raise RuntimeError('Windows OCR 未返回有效文字，请重试')
        return re.sub(r'(?<=[\u3400-\u9fff]) +(?=[\u3400-\u9fff])', '', text).strip()
    finally:
        image_path.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)
