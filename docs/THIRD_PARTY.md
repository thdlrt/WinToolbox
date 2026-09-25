# Third-party components

WinToolbox uses the following third-party components. Their respective license terms remain applicable. Dependency versions are recorded in Cargo.lock, package-lock.json and core/uv.lock. Python wheel distribution metadata includes license files where provided by upstream.

| Component | Project and license information |
|---|---|
| Tauri | https://github.com/tauri-apps/tauri (MIT / Apache-2.0) |
| drag-rs | https://github.com/crabnebula-dev/drag-rs (MIT / Apache-2.0; native file drag-out) |
| Mem Reduct 3.5.2 | https://github.com/henrypp/memreduct (GPL-3.0-or-later; unmodified standalone x64 helper, license/signature/source reference in tools/memreduct) |
| React | https://github.com/facebook/react (MIT) |
| Python | https://www.python.org/psf/license/ |
| uv | https://github.com/astral-sh/uv (MIT / Apache-2.0) |
| FFmpeg | https://ffmpeg.org/legal.html ; builds may include GPL components, check bundled binary `-L` and `-buildconf` output |
| LibreOffice | https://www.libreoffice.org/about-us/licenses/ (MPL-2.0; optional runtime) |
| faster-whisper | https://github.com/SYSTRAN/faster-whisper (MIT; optional runtime) |
| Qwen3-ASR | https://github.com/QwenLM/Qwen3-ASR (Apache-2.0; optional model) |
| SenseVoice | https://github.com/FunAudioLLM/SenseVoice (model and runtime terms in upstream repository) |
| CosyVoice | https://github.com/FunAudioLLM/CosyVoice (Apache-2.0; optional model) |
| Real-ESRGAN | https://github.com/xinntao/Real-ESRGAN (BSD-3-Clause; optional runtime) |
| Demucs | https://github.com/facebookresearch/demucs (MIT; optional runtime) |
| Pyannote | https://github.com/pyannote/pyannote-audio (MIT runtime; model access terms separate) |
| CMUdict | https://github.com/cmusphinx/cmudict (BSD-2-Clause; bundled offline American-English pronunciation dictionary) |
| edge-tts | https://github.com/rany2/edge-tts (LGPL-3.0; client library used for optional speech synthesis) |
| Android Debug Bridge | https://developer.android.com/tools/releases/platform-tools ; official Platform-Tools 37.0.1 runtime, upstream notices retained in tools/adb/NOTICE.txt |

Model artifacts are obtained only from the corresponding upstream sources when the user installs them. Model installers record revisions and validate available upstream integrity hashes. The release bundle includes FFmpeg build and license metadata in `tools`.

The Windows ADB runtime is staged by scripts/prepare_adb.py from the pinned official platform-tools_r37.0.1-win.zip archive. tools/adb/SOURCE.json records its URL, SHA-256 and extracted file hashes. Only ADB and its runtime DLLs/notices are included; Shizuku itself must be installed on the user's phone. Host ADB pairing credentials use the Android tools' normal local storage and are not included in WinToolbox backup/WebDAV snapshots.

The unmodified CMUdict data is bundled at `core/toolbox/data/cmudict/cmudict.dict`, pinned to upstream commit `74790861f652b15e4ac49015a90074ad62a27690`. Its complete copyright notice and BSD-2-Clause terms are retained in the adjacent `LICENSE`; `SOURCE.json` records download URLs, sizes and SHA-256 checksums. Copyright (C) 1993-2015 Carnegie Mellon University. The application converts ARPABET to broad American IPA locally. This conversion preserves dictionary pronunciation variants, places stress conservatively because CMUdict provides no syllable boundaries, and does not predict pronunciations for missing words. It is labeled “美式音标（词典转换）”.

The 45 short MP3 examples under `desktop/public/phonetics/audio` contain only WinToolbox-authored English words and practice sentences. They were synthesized with the Microsoft voice identifier `en-US-AriaNeural` through Edge Read Aloud; they contain no user recordings. The adjacent `manifest.json` records the voice, synthetic type and exact text of every clip. Use of the online voice service and its output remains subject to Microsoft's applicable terms.


Mihomo 1.19.30 (GPL-3.0), Windows amd64: https://github.com/MetaCubeX/mihomo/tree/v1.19.30 . The unmodified executable is bundled for optional TUN routing. Official release ZIP SHA-256: 22c09fd67673895ef7cd6b1820563918275c3d316f2462b306208675118db3c0. Source and license: https://github.com/MetaCubeX/mihomo .

Mem Reduct 3.5.2 (GPL-3.0-or-later) is bundled as an unmodified, independent optional application for its interactive settings and automatic cleaning. Source: https://github.com/henrypp/memreduct/tree/v.3.5.2 . tools/memreduct retains its license, upstream source link and signature. WinToolbox's manual silent cleaner is separately implemented using Windows native memory interfaces; no Mem Reduct source code is incorporated.
