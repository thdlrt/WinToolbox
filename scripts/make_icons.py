"""Generate the toolbox's geometric application icon (no external assets)."""
from pathlib import Path
import struct
import zlib

root = Path(__file__).resolve().parents[1] / 'desktop/src-tauri/icons'
root.mkdir(parents=True, exist_ok=True)

def png(size):
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            xx, yy = x / size, y / size
            color = (31, 87, 231, 255)
            for left, top in ((.22, .22), (.55, .22), (.22, .55), (.55, .55)):
                if left < xx < left + .23 and top < yy < top + .23:
                    color = (255, 255, 255, 255) if top < .5 or left < .5 else (126, 229, 208, 255)
            raw.extend(color)
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>2I5B', size, size, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')

for size in (32, 128, 256):
    (root / f'{size}x{size}.png').write_bytes(png(size))
data = png(256)
(root / 'icon.ico').write_bytes(struct.pack('<3H', 0, 1, 1) + struct.pack('<4B2H2I', 0, 0, 0, 0, 1, 32, len(data), 22) + data)
