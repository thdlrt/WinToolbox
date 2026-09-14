"""Managed, isolated optional runtimes. No import from the original AiText environment."""
import hashlib
import json
import os
import shutil
import sys
import threading
import tempfile
import zipfile
from pathlib import Path

import httpx
from .settings import atomic_json

CATALOG = [
    {"id":"faster-whisper-small","name":"Whisper small","engine":"faster-whisper","repo":"Systran/faster-whisper-small","size_hint":"500 MB","description":"轻量 CPU 转写，int8 低内存模式"},
    {"id":"faster-whisper-turbo","name":"Whisper large-v3-turbo","engine":"faster-whisper","repo":"mobiuslabsgmbh/faster-whisper-large-v3-turbo","size_hint":"1.6 GB","description":"快速多语种转写，词级时间戳；支持 CPU / NVIDIA"},
    {"id":"faster-whisper-large-v3","name":"Whisper large-v3","engine":"faster-whisper","repo":"Systran/faster-whisper-large-v3","size_hint":"3.1 GB","description":"高质量本地转写，较高内存需求"},
    {"id":"qwen-asr-0.6b","name":"Qwen3-ASR 0.6B","engine":"qwen-asr","repo":"Qwen/Qwen3-ASR-0.6B","size_hint":"3 GB + 对齐器","description":"轻量新一代多语种识别；含强制对齐器"},
    {"id":"qwen-asr-1.7b","name":"Qwen3-ASR 1.7B","engine":"qwen-asr","repo":"Qwen/Qwen3-ASR-1.7B","size_hint":"5 GB + 对齐器","description":"多语种识别；Windows 使用分段推理"},
    {"id":"sensevoice-small","name":"SenseVoiceSmall","engine":"sensevoice","repo":"FunAudioLLM/SenseVoiceSmall","size_hint":"1 GB","description":"中文、英文、日语、韩语及粤语快速识别"},
    {"id":"pyannote","name":"Pyannote 说话人区分","engine":"pyannote","repo":"pyannote/speaker-diarization-community-1","size_hint":"1 GB","description":"需在 Hugging Face 接受模型条款并配置访问令牌"},
    {"id":"demucs","name":"Demucs 人声 / 背景分离","engine":"demucs","size_hint":"2 GB","description":"用于配音保留背景音乐；首次运行下载官方 htdemucs 权重"},
    {"id":"cosyvoice","name":"CosyVoice3 参考音频配音","engine":"cosyvoice","repo":"FunAudioLLM/Fun-CosyVoice3-0.5B-2512","size_hint":"8 GB","description":"本地参考音色生成；独立 Python 3.10 环境"},
    {"id":"realesrgan","name":"Real-ESRGAN Vulkan","engine":"realesrgan","size_hint":"100 MB","description":"Windows Vulkan GPU 视频增强，2× / 4×，最高 4K"},
    {"id":"local-qwen35-08b","name":"Qwen3.5 0.8B","engine":"ollama","ollama_model":"qwen3.5:0.8b","size_hint":"约 1 GB","description":"轻量本地翻译、问答和图片理解"},
    {"id":"local-qwen35-4b","name":"Qwen3.5 4B","engine":"ollama","ollama_model":"qwen3.5:4b","size_hint":"约 3.4 GB","description":"均衡本地翻译、问答和图片理解"},
    {"id":"local-qwen35-9b","name":"Qwen3.5 9B","engine":"ollama","ollama_model":"qwen3.5:9b","size_hint":"约 6.6 GB","description":"高质量本地翻译、问答和图片理解"},
    {"id":"local-embedding","name":"EmbeddingGemma","engine":"ollama","ollama_model":"embeddinggemma:latest","size_hint":"约 622 MB","description":"本地多语言资料检索向量模型"},
]
PACKAGES = {
    "faster-whisper": ["faster-whisper==1.2.1", "nvidia-cublas-cu12==12.8.4.1", "nvidia-cudnn-cu12==9.10.2.21"],
    "qwen-asr": ["qwen-asr==0.0.6", "torch==2.8.0", "torchaudio==2.8.0"],
    "sensevoice": ["funasr==1.4.14", "torch==2.8.0", "torchaudio==2.8.0", "modelscope>=1.20,<2"],
    "pyannote": ["pyannote.audio==4.0.7", "torch==2.8.0", "torchaudio==2.8.0", "torchcodec==0.7.0", "soundfile>=0.13,<1"],
    "demucs": ["demucs==4.1.0", "torch==2.8.0", "torchaudio==2.8.0", "soundfile>=0.13,<1"],
    "cosyvoice": ["torch==2.8.0", "torchaudio==2.8.0", "numpy==1.26.4", "conformer==0.3.2", "diffusers==0.29.0", "hydra-core==1.3.2", "HyperPyYAML==1.2.3", "inflect==7.3.1", "librosa==0.10.2", "lightning==2.2.4", "modelscope==1.20.0", "onnx==1.16.0", "onnxruntime==1.18.0", "openai-whisper==20231117", "soundfile==0.12.1", "transformers==4.51.3", "x-transformers==2.11.24", "wetext==0.0.4", "wget==3.2", "pyarrow==18.1.0", "rich==13.7.1"],
}
ALIASES = {"small":"faster-whisper-small","large-v3-turbo":"faster-whisper-turbo","large-v3":"faster-whisper-large-v3","0.6B":"qwen-asr-0.6b","1.7B":"qwen-asr-1.7b","SenseVoiceSmall":"sensevoice-small"}


class Models:
    def __init__(self, app):
        self.app = app
        self.root = app.data_dir / "models"
        self.runtimes = app.data_dir / "runtimes"
        self.root.mkdir(exist_ok=True)
        self.runtimes.mkdir(exist_ok=True)
        self.install_lock = threading.Lock()

    def list(self):
        models = []
        for spec in CATALOG:
            if spec["engine"] == "ollama":
                status = self.app.local_llm.status(spec["ollama_model"])
                models.append({**spec, "installed": status["installed"], "path": status.get("path") if status["installed"] else None})
                continue
            marker = self.root / spec["id"] / "installed.json"
            installed = marker.exists()
            models.append({**spec, "installed": installed, "path": str(marker.parent) if installed else None})
        return {"models": models, "runtimes": [{"engine":p.name,"path":str(p)} for p in self.runtimes.iterdir() if p.is_dir()]}

    def identify(self, model_id, engine=None):
        model_id = ALIASES.get(model_id, model_id)
        if not model_id:
            model_id = {"faster-whisper":"faster-whisper-turbo","qwen-asr":"qwen-asr-0.6b","sensevoice":"sensevoice-small"}.get(engine, engine)
        spec = next((s for s in CATALOG if s["id"] == model_id or s.get("repo") == model_id), None)
        if not spec:
            raise ValueError(f"未知本地模型：{model_id}，请从模型中心选择")
        return spec

    def runtime_python(self, engine):
        path = self.runtimes / engine / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not path.exists():
            raise RuntimeError(f"尚未安装 {engine} 运行包，请打开模型中心安装")
        return str(path)

    def require(self, model_id=None, engine=None):
        spec = self.identify(model_id, engine)
        if spec["engine"] == "ollama":
            status = self.app.local_llm.status(spec["ollama_model"])
            if not status["installed"]:
                raise RuntimeError(f"请先安装“{spec['name']}”")
            return {**status, "engine": "ollama", "model": spec["id"]}
        marker = self.root / spec["id"] / "installed.json"
        if not marker.exists():
            raise RuntimeError(f"请先在模型中心安装“{spec['name']}”")
        result = json.loads(marker.read_text("utf-8"))
        result.update(path=str(marker.parent), model=spec["id"], engine=spec["engine"])
        if spec["engine"] != "realesrgan":
            result["python"] = self.runtime_python(spec["engine"])
        return result

    def access_token(self):
        token = self.app.settings.secret("huggingface")
        if token:
            return token
        # The UI generates provider IDs; users can still configure gated model access by name.
        for provider in self.app.settings.get().get("providers", []):
            if provider.get("name", "").lower().replace(" ", "").replace("-", "") in ("huggingface", "hf"):
                return self.app.settings.secret(provider["id"])
        return None

    def install(self, job):
        spec = self.identify(job.params["model_id"])
        with self.install_lock:
            if spec["engine"] == "ollama":
                return self.app.local_llm.install(job, spec["ollama_model"])
            dest = self.root / spec["id"]
            dest.mkdir(parents=True, exist_ok=True)
            metadata = dict(spec)
            job.progress(2, f"准备 {spec['name']} 独立运行包")
            if spec["engine"] == "realesrgan":
                url = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip"
                archive = dest / "runtime.zip"
                metadata["archive_sha256"] = self.download(url, archive, job)
                self.extract(archive, dest)
                binaries = list(dest.rglob("realesrgan-ncnn-vulkan.exe"))
                if not binaries:
                    raise RuntimeError("下载运行包中未找到增强程序")
                metadata["binary"] = str(binaries[0].relative_to(dest))
            else:
                envdir = self.runtimes / spec["engine"]
                bundled_uv = Path(os.getenv("WINTOOLBOX_TOOLS", "")) / "uv.exe"
                uv = str(bundled_uv) if bundled_uv.is_file() else shutil.which("uv")
                uv_cmd = [uv] if uv else [sys.executable, "-m", "uv"]
                py_version = "3.10" if spec["engine"] == "cosyvoice" else "3.12"
                if not (envdir / "pyvenv.cfg").exists():
                    runtime_env = dict(os.environ)
                    runtime_env["UV_PYTHON_INSTALL_DIR"] = str(self.runtimes / "python")
                    # Keep base interpreters inside app data, so include-model backups migrate them too.
                    job.run_process(uv_cmd + ["python", "install", py_version, "--no-bin", "--no-registry"], env=runtime_env)
                    base_python = job.run_process(uv_cmd + ["python", "find", "--managed-python", py_version], env=runtime_env).strip().splitlines()[-1]
                    job.run_process(uv_cmd + ["venv", "--python", base_python, str(envdir)], env=runtime_env)
                python = self.runtime_python(spec["engine"])
                job.progress(8, "安装固定版本依赖，首次下载可能较大")
                if spec["engine"] in ("qwen-asr", "sensevoice", "demucs", "cosyvoice", "pyannote"):
                    # CUDA 12.8 is required for Blackwell GPUs; wheels also run on CPUs.
                    job.run_process(uv_cmd + ["pip", "install", "--python", python, "torch==2.8.0", "torchaudio==2.8.0", "--index-url", "https://download.pytorch.org/whl/cu128"])
                packages = PACKAGES[spec["engine"]]
                if spec["engine"] == "faster-whisper" and job.params.get("cpu_only"):
                    packages = [package for package in packages if not package.startswith("nvidia-")]
                job.run_process(uv_cmd + ["pip", "install", "--python", python] + packages)
                metadata["packages"] = packages
                if spec.get("repo"):
                    job.progress(35, "获取模型固定版本并下载权重")
                    metadata["revision"] = self.snapshot(spec["repo"], dest, job)
                if spec["engine"] == "qwen-asr":
                    metadata["aligner_revision"] = self.snapshot("Qwen/Qwen3-ForcedAligner-0.6B", dest / "aligner", job)
                if spec["engine"] == "cosyvoice":
                    # GitHub source archive + exact Matcha submodule commit, no system Git needed.
                    with httpx.Client(follow_redirects=True, timeout=60) as client:
                        commit = client.get("https://api.github.com/repos/FunAudioLLM/CosyVoice/commits/main").json()["sha"]
                        sub = client.get(f"https://api.github.com/repos/FunAudioLLM/CosyVoice/contents/third_party/Matcha-TTS?ref={commit}").json()
                    source_zip = dest / "source.zip"
                    self.download(f"https://codeload.github.com/FunAudioLLM/CosyVoice/zip/{commit}", source_zip, job)
                    self.extract(source_zip, dest / "source")
                    source = dest / "source" / f"CosyVoice-{commit}"
                    matcha_zip = dest / "matcha.zip"
                    self.download(f"https://codeload.github.com/shivammehta25/Matcha-TTS/zip/{sub['sha']}", matcha_zip, job)
                    self.extract(matcha_zip, dest / "matcha")
                    metadata.update(source=str(source.relative_to(dest)), matcha=str((dest / "matcha" / f"Matcha-TTS-{sub['sha']}").relative_to(dest)), source_revision=commit)
                job.progress(90, "检查运行包导入与安装记录")
                imports = {"faster-whisper":"faster_whisper", "qwen-asr":"qwen_asr", "sensevoice":"funasr", "pyannote":"pyannote.audio", "demucs":"demucs", "cosyvoice":"torch, torchaudio, onnxruntime"}
                job.run_process([python, "-c", f"import {imports[spec['engine']]}"])
                freeze = job.run_process(uv_cmd + ["pip", "freeze", "--python", python])
                (dest / "requirements.lock.txt").write_text(freeze, encoding="utf-8")
            atomic_json(dest / "installed.json", metadata)
            job.artifact(dest / "installed.json", "model", spec["name"])
            return metadata

    def snapshot(self, repo, dest, job):
        from huggingface_hub import HfApi, hf_hub_download
        from tqdm.auto import tqdm
        from .jobs import Cancelled
        class CancellableProgress(tqdm):
            def update(self, n=1):
                job.check_cancelled()
                return super().update(n)
        token = self.access_token()
        try:
            info = HfApi(token=token).model_info(repo, files_metadata=True)
            revision = info.sha
            dest.mkdir(parents=True, exist_ok=True)
            for index, entry in enumerate(info.siblings):
                job.check_cancelled()
                if entry.rfilename.startswith(".git"):
                    continue
                job.progress(35 + 50*(index/max(1,len(info.siblings))), f"下载 {repo}: {entry.rfilename}")
                path = hf_hub_download(repo, entry.rfilename, revision=revision, local_dir=dest, token=token, tqdm_class=CancellableProgress)
                lfs = entry.lfs
                expected = lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None)
                if expected:
                    actual = self.sha256(path)
                    if actual != expected:
                        raise RuntimeError(f"模型文件校验失败：{entry.rfilename}")
            return revision
        except Cancelled:
            raise
        except Exception as exc:
            raise RuntimeError(f"模型下载失败：{repo}。请检查 Hugging Face 网络与模型授权；可添加名称为 Hugging Face 的服务，保存访问令牌为 API Key。{type(exc).__name__}: {str(exc)[:300]}") from exc

    @staticmethod
    def sha256(path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            while block := handle.read(1024*1024):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def download(url, dest, job):
        temp = dest.with_suffix(dest.suffix + ".partial")
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
            response.raise_for_status()
            with temp.open("wb") as handle:
                for block in response.iter_bytes(1024*1024):
                    job.check_cancelled()
                    handle.write(block)
        temp.replace(dest)
        return Models.sha256(dest)

    @staticmethod
    def extract(archive, dest):
        dest = Path(dest).resolve()
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                target = (dest / member.filename).resolve()
                if not target.is_relative_to(dest) or ((member.external_attr >> 16) & 0o170000) == 0o120000:
                    raise ValueError("运行包包含非法路径或链接")
            bundle.extractall(dest)

    def remove(self, model_id):
        spec = self.identify(model_id)
        with self.app.jobs.lock:
            self.app.model_setup.ensure_idle()
        if spec["engine"] == "ollama":
            raise ValueError("本地问答模型使用共享运行包，暂不支持单项移除；迁移请使用包含模型的数据备份")
        if any(j["status"] in ("running", "queued") and j["tool"] in ("media", "models.install") for j in self.app.jobs.list()):
            raise ValueError("请等待媒体/模型任务结束后再移除模型")
        target = (self.root / spec["id"]).resolve()
        if not target.is_relative_to(self.root.resolve()) or target == self.root.resolve():
            raise ValueError("非法模型路径")
        if target.exists():
            shutil.rmtree(target)
        return {"ok": True}

    def export_package(self, job):
        """Include runtime and its base Python, so import does not require an existing Python."""
        if self.identify(job.params["model_id"])["engine"] == "ollama":
            raise ValueError("本地问答模型请在数据备份中勾选“包含本地模型”迁移")
        installed = self.require(job.params["model_id"])
        spec = self.identify(job.params["model_id"])
        destination = Path(job.params["path"]).resolve()
        if destination.exists():
            raise ValueError("导出文件已存在，请选择新的文件名")
        source = Path(installed["path"])
        roots = [("model", source)]
        if spec["engine"] != "realesrgan":
            runtime = self.runtimes / spec["engine"]
            roots.append(("runtime", runtime))
            cfg = dict(line.split("=",1) for line in (runtime/"pyvenv.cfg").read_text("utf-8").splitlines() if "=" in line)
            home = next((value.strip() for key,value in cfg.items() if key.strip()=="home"),None)
            if not home or not Path(home).is_dir():
                raise RuntimeError("未找到运行包基础 Python，无法生成完整离线包")
            roots.append(("python", Path(home)))
        files=[]
        for prefix,root in roots:
            for path in root.rglob("*"):
                if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts and ".cache" not in path.parts:
                    files.append((path,f"{prefix}/{path.relative_to(root).as_posix()}"))
        destination.parent.mkdir(parents=True,exist_ok=True)
        temporary=destination.with_suffix(destination.suffix+".partial")
        checksums={}
        try:
            with zipfile.ZipFile(temporary,"w",zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as archive:
                for index,(path,name) in enumerate(files):
                    job.progress(index/max(1,len(files))*95,f"导出运行包：{path.name}")
                    archive.write(path,name)
                    checksums[name]=self.sha256(path)
                archive.writestr("package.json",json.dumps({"format":"wintoolbox-runtime","version":1,"model_id":spec["id"],"engine":spec["engine"],"platform":sys.platform,"files":checksums},ensure_ascii=False))
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        job.artifact(destination,"runtime-package",spec["name"]+" 离线包")
        return {"path":str(destination)}

    def import_package(self, job):
        source=Path(job.params["path"]).resolve()
        with self.install_lock, tempfile.TemporaryDirectory(prefix="runtime-import-",dir=self.app.data_dir) as staging:
            staging=Path(staging)
            with zipfile.ZipFile(source) as archive:
                metadata=json.loads(archive.read("package.json"))
                if metadata.get("format")!="wintoolbox-runtime" or metadata.get("version")!=1 or metadata.get("platform")!=sys.platform:
                    raise ValueError("离线包版本或平台不兼容")
                spec=self.identify(metadata["model_id"])
                if spec["engine"] == "ollama":
                    raise ValueError("本地问答模型请通过包含模型的数据备份恢复")
                if metadata["engine"]!=spec["engine"]:
                    raise ValueError("离线包模型引擎不匹配")
                declared=set(metadata.get("files",{}))
                actual={name for name in archive.namelist() if not name.endswith("/") and name!="package.json"}
                if declared!=actual:
                    raise ValueError("离线包文件清单不完整")
            self.extract(source,staging)
            for index,(name,expected) in enumerate(metadata["files"].items()):
                job.progress(index/max(1,len(metadata["files"]))*80,f"校验运行包：{Path(name).name}")
                if self.sha256(staging/name)!=expected:
                    raise ValueError(f"离线包文件校验失败：{name}")
            destination=self.root/spec["id"]
            if (destination/"installed.json").exists():
                raise ValueError("该模型已安装，请先移除现有模型再导入")
            if not (staging/"model"/"installed.json").exists():
                raise ValueError("离线包缺少模型安装标识")
            engine=spec["engine"]
            if engine!="realesrgan":
                runtime=self.runtimes/engine
                base=self.runtimes/(engine+"-python")
                # Existing shared runtime remains in use; import only missing runtime.
                if not (runtime/"pyvenv.cfg").exists():
                    if runtime.exists() or base.exists():
                        raise ValueError("存在不完整运行包目录，请先通过模型中心重新安装清理")
                    shutil.move(str(staging/"python"),str(base))
                    shutil.move(str(staging/"runtime"),str(runtime))
                    cfg=runtime/"pyvenv.cfg"
                    lines=cfg.read_text("utf-8").splitlines()
                    lines=[f"home = {base}" if line.partition("=")[0].strip()=="home" else line for line in lines]
                    cfg.write_text("\n".join(lines)+"\n",encoding="utf-8")
                    job.run_process([self.runtime_python(engine),"-c","import sys; print(sys.version)"])
            if destination.exists():
                # Only a catalog-owned failed installation may be replaced.
                resolved=destination.resolve()
                if not resolved.is_relative_to(self.root.resolve()) or resolved==self.root.resolve():
                    raise ValueError("非法目标路径")
                shutil.rmtree(resolved)
            shutil.move(str(staging/"model"),str(destination))
            return {"model_id":spec["id"],"installed":True}
