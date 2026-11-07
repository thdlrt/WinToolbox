# Third-party components

QQ bridge local runtime: Node.js 24.14.0 (MIT and bundled third-party notices in tools/qqbot-runtime/node-LICENSE.txt), @tencent-connect/qqbot-nodejs 1.0.5 (MIT, https://github.com/tencent-connect/qqbot-nodejs), qrcode 1.5.4 (MIT), and @tencent-connect/qqbot-connector 1.2.0 (published official package declares UNLICENSED; retained unmodified for this owner's local integration, no public redistribution grant inferred). Exact dependencies and integrity hashes: tools/qqbot-runtime/package-lock.json. QQ binding and gateway requests go to Tencent endpoints. DSH's SDK installation and secrets are not copied.

WinToolbox uses the following third-party components. Their respective license terms remain applicable. Dependency versions are recorded in Cargo.lock, package-lock.json and core/uv.lock. Python wheel distribution metadata includes license files where provided by upstream.

| Component | Project and license information |
|---|---|
| Tauri | https://github.com/tauri-apps/tauri (MIT / Apache-2.0) |
| drag-rs | https://github.com/crabnebula-dev/drag-rs (MIT / Apache-2.0; native file drag-out) |
| React | https://github.com/facebook/react (MIT) |
| Python | https://www.python.org/psf/license/ |
| uv | https://github.com/astral-sh/uv (MIT / Apache-2.0) |
| FFmpeg | https://ffmpeg.org/legal.html ; builds may include GPL components, check bundled binary `-L` and `-buildconf` output |
| ncmdump 1.5.1 | https://github.com/taurusxin/ncmdump/tree/1.5.1 (MIT; unmodified Windows converter, full LICENSE.txt and SOURCE.json in tools/ncmdump) |
| python-qrcode | https://github.com/lincolnloop/python-qrcode (BSD-3-Clause; local NetEase login QR generation) |
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
| rclone 1.75.2 | https://rclone.org (MIT; unmodified Windows executable, pinned by scripts/prepare_netmount.py, license text in tools/netmount) |

Model artifacts are obtained only from the corresponding upstream sources when the user installs them. Model installers record revisions and validate available upstream integrity hashes. The release bundle includes FFmpeg build and license metadata in `tools`.

The ncmdump Windows release ZIP is pinned to SHA-256 `bb849221c06b8fdbff42aefb86baea9c07256568658d80f4be72a39a2a1632dc` by scripts/prepare_ncmdump.py. NCM decoding and embedded metadata writing are performed by that unmodified executable. FFmpeg handles optional FLAC-to-MP3 conversion. NetEase request encoding interoperates with the protocol documented by https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/blob/main/util/crypto.js ; account requests go directly to music.163.com. No third-party API server receives credentials.


The unmodified CMUdict data is bundled at `core/toolbox/data/cmudict/cmudict.dict`, pinned to upstream commit `74790861f652b15e4ac49015a90074ad62a27690`. Its complete copyright notice and BSD-2-Clause terms are retained in the adjacent `LICENSE`; `SOURCE.json` records download URLs, sizes and SHA-256 checksums. Copyright (C) 1993-2015 Carnegie Mellon University. The application converts ARPABET to broad American IPA locally. This conversion preserves dictionary pronunciation variants, places stress conservatively because CMUdict provides no syllable boundaries, and does not predict pronunciations for missing words. It is labeled “美式音标（词典转换）”.

The 45 short MP3 examples under `desktop/public/phonetics/audio` contain only WinToolbox-authored English words and practice sentences. They were synthesized with the Microsoft voice identifier `en-US-AriaNeural` through Edge Read Aloud; they contain no user recordings. The adjacent `manifest.json` records the voice, synthetic type and exact text of every clip. Use of the online voice service and its output remains subject to Microsoft's applicable terms.


Mihomo 1.19.30 (GPL-3.0), Windows amd64: https://github.com/MetaCubeX/mihomo/tree/v1.19.30 . The unmodified executable is bundled for optional TUN routing. Official release ZIP SHA-256: 22c09fd67673895ef7cd6b1820563918275c3d316f2462b306208675118db3c0. Source and license: https://github.com/MetaCubeX/mihomo .

Memory cleanup and automatic scheduling are implemented inside WinToolbox and reuse its fixed-purpose native helper. Mem Reduct is no longer downloaded, bundled or launched. Its Windows memory-region and threshold/interval behavior was reviewed as a reference: https://github.com/henrypp/memreduct/blob/v.3.5.2/src/main.c . No upstream source code is copied or linked; the existing WinToolbox helper calls Windows native memory interfaces directly.

Bambu cloud telemetry uses a WinToolbox-authored isolated adapter that loads the owner's installed Bambu Studio network plugin. The proprietary network DLL and certificates are not redistributed. Interface reference: https://github.com/bambulab/BambuStudio/blob/master/src/slic3r/Utils/NetworkAgent.hpp . Use of the installed plugin and cloud service remains subject to Bambu Lab's terms. Only subscription and read-only pushall requests are exposed by the adapter.
