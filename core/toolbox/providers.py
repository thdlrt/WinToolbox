"""Small HTTP adapters: custom endpoints remain first-class, no SDK lock-in."""
import base64
import json
import mimetypes
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class ProviderError(RuntimeError):
    pass


def content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


class Providers:
    def __init__(self, settings, client=None, local_llm=None, usage=None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=httpx.Timeout(180, connect=15), follow_redirects=True)
        self.local_llm = local_llm
        self.usage = usage

    @staticmethod
    def response_usage(body):
        if not isinstance(body, dict):
            return {}
        return body.get("usage") or body.get("usageMetadata") or body.get("usage_metadata") or {}

    def record_usage(self, provider, model, role, operation, started, status="success", *, usage=None, audio_seconds=0):
        if not self.usage or provider.get("kind") == "local":
            return
        try:
            self.usage.record_usage(provider.get("id", "unknown"), model, role, operation, status,
                                    usage=usage, audio_seconds=audio_seconds,
                                    latency_ms=round((time.monotonic() - started) * 1000))
        except Exception:
            # Accounting must never break the model operation it observes.
            pass

    def resolve(self, role="chat", provider_id=None, model=None):
        values = self.settings.get()
        selected = values["roles"].get(role, {})
        if values.get("preferences", {}).get("model_mode") == "local":
            if provider_id not in (None, "local"):
                raise ProviderError("本地模式不会调用云端服务；请先切换到 API 模式")
            if role not in ("chat", "translate", "vision", "embedding"):
                raise ProviderError("本地模式请使用本地转写或 CosyVoice 配音；不会回退到云端 API")
            if selected.get("provider_id") != "local" or not selected.get("model"):
                raise ProviderError("本地预设配置不完整，请在设置中重新应用本地预设")
            if self.local_llm is None:
                raise ProviderError("本地模型运行服务不可用，请重新启动工具箱")
            return {"id": "local", "kind": "local", "name": "本地模型"}, model or selected["model"], ""
        provider_id = provider_id or selected.get("provider_id")
        provider = next((p for p in values["providers"] if p["id"] == provider_id), None)
        if not provider:
            raise ProviderError(f"请在设置中为 {role} 选择供应商")
        model = model or selected.get("model") or provider.get("model")
        if not model:
            raise ProviderError(f"请为 {role} 填写模型 ID")
        key = self.settings.secret(provider_id)
        is_loopback = urlsplit(provider.get("base_url", "")).hostname in ("localhost", "127.0.0.1", "::1")
        if not key and not provider.get("allow_no_key", False) and not is_loopback:
            raise ProviderError(f"请在设置中为“{provider.get('name', provider_id)}”填写 API 密钥并测试连接")
        return provider, model, key

    @staticmethod
    def native_base(provider):
        base = provider.get("native_url") or provider.get("base_url", "")
        if "/compatible-mode/" in base:
            base = base.split("/compatible-mode/")[0]
        if base.endswith("/api/v1"):
            return base
        if "maas.aliyuncs.com" in base or "dashscope" in base:
            return base.rstrip("/") + "/api/v1"
        return "https://dashscope-intl.aliyuncs.com/api/v1" if provider.get("region") in ("intl", "sg") else "https://dashscope.aliyuncs.com/api/v1"

    def _check(self, response):
        if response.status_code >= 400:
            # Error bodies can echo input/audio; expose only provider error text, never entire request.
            try:
                body = response.json()
                err = body.get("error", body)
                message = err.get("message", err.get("code", "")) if isinstance(err, dict) else str(err)
            except Exception:
                message = response.reason_phrase
            raise ProviderError(f"API HTTP {response.status_code}：{str(message)[:1200]}")
        return response

    def _json(self, method, url, **kwargs):
        try:
            data = self._check(self.client.request(method, url, **kwargs)).json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"API 连接失败：{type(exc).__name__}；请检查网络、代理和 API 地址") from exc
        if isinstance(data, dict) and (data.get("error") or data.get("code") and not data.get("output")):
            error = data.get("error", data)
            raise ProviderError(str(error.get("message", error))[:1200] if isinstance(error, dict) else str(error)[:1200])
        return data

    def chat(self, messages, role="chat", stream_callback=None, *, model=None, provider_id=None, cancel=None):
        provider, model, key = self.resolve(role, provider_id, model)
        if cancel:
            cancel()
        if provider["kind"] == "local":
            return self.local_llm.chat(messages, model=model, stream_callback=stream_callback, cancel=cancel)
        started, status, used = time.monotonic(), "error", {}
        if provider["kind"] == "gemini":
            try:
                result, used = self._gemini(messages, provider, model, key, stream_callback, cancel)
                status = "success"
                return result
            except Exception as exc:
                status = "cancelled" if "取消" in str(exc) else "error"
                raise
            finally:
                self.record_usage(provider, model, role, "chat", started, status, usage=used)
        url = provider["base_url"].rstrip("/") + "/chat/completions"
        payload = {"model": model, "messages": messages, "stream": bool(stream_callback)}
        if provider["kind"] == "dashscope":
            payload["enable_thinking"] = False
            if stream_callback:
                payload["stream_options"] = {"include_usage": True}
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            if not stream_callback:
                body = self._json("POST", url, json=payload, headers=headers)
                used = self.response_usage(body)
                if cancel:
                    cancel()
                choices = body.get("choices", [])
                if not choices:
                    raise ProviderError("模型未返回内容；检查模型能力、过滤结果及额度")
                if choices[0].get("finish_reason") == "length":
                    raise ProviderError("模型输出被长度上限截断；请使用更大的输出上限或缩短处理分段")
                status = "success"
                return content_text(choices[0]["message"].get("content"))
            result = []
            with self.client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    response.read()
                self._check(response)
                for line in response.iter_lines():
                    if cancel:
                        cancel()
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    if not raw:
                        continue
                    data = json.loads(raw)
                    if self.response_usage(data):
                        used = self.response_usage(data)
                    if data.get("error"):
                        raise ProviderError(str(data["error"])[:1000])
                    for choice in data.get("choices", []):
                        if choice.get("finish_reason") == "length":
                            raise ProviderError("模型流式输出被长度上限截断；请增大输出上限或缩短问题")
                        text = content_text(choice.get("delta", {}).get("content"))
                        if text:
                            result.append(text)
                            stream_callback(text)
            status = "success"
            return "".join(result)
        except httpx.HTTPError as exc:
            raise ProviderError(f"流式 API 连接中断：{type(exc).__name__}") from exc
        except Exception as exc:
            status = "cancelled" if "取消" in str(exc) else "error"
            raise
        finally:
            self.record_usage(provider, model, role, "chat", started, status, usage=used)

    def _gemini(self, messages, provider, model, key, callback, cancel):
        system, contents = [], []
        for message in messages:
            if message["role"] == "system":
                system.append({"text": content_text(message["content"])})
                continue
            parts = []
            content = message["content"]
            for part in ([{"type": "text", "text": content}] if isinstance(content, str) else content):
                if part.get("type") == "text":
                    parts.append({"text": part["text"]})
                elif part.get("type") == "image_url":
                    url = part["image_url"]["url"]
                    if url.startswith("data:"):
                        meta, data = url.split(",", 1)
                        parts.append({"inlineData": {"mimeType": meta[5:].split(";")[0], "data": data}})
                    else:
                        raise ProviderError("Gemini 视觉输入请使用内嵌图片，暂不接受外部图片 URL")
            contents.append({"role": "model" if message["role"] == "assistant" else "user", "parts": parts})
        payload = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": system}
        base = provider.get("base_url", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        action = "streamGenerateContent?alt=sse" if callback else "generateContent"
        url = f"{base}/models/{model.removeprefix('models/')}:{action}"
        headers = {"x-goog-api-key": key}
        def parse(body):
            return "".join(p.get("text", "") for c in body.get("candidates", []) for p in c.get("content", {}).get("parts", []) if not p.get("thought"))
        if not callback:
            body = self._json("POST", url, json=payload, headers=headers)
            return parse(body), self.response_usage(body)
        values = []
        used = {}
        with self.client.stream("POST", url, json=payload, headers=headers) as response:
            if response.status_code >= 400:
                response.read()
            self._check(response)
            for line in response.iter_lines():
                if cancel:
                    cancel()
                if line.startswith("data:"):
                    body = json.loads(line[5:])
                    if self.response_usage(body):
                        used = self.response_usage(body)
                    text = parse(body)
                    if text:
                        values.append(text)
                        callback(text)
        return "".join(values), used

    def embed(self, texts):
        provider, model, key = self.resolve("embedding")
        if provider["kind"] == "local":
            return self.local_llm.embed(texts, model=model)
        values = []
        for offset in range(0, len(texts), 10):
            batch = texts[offset:offset + 10]
            started, status, used = time.monotonic(), "error", {}
            try:
                if provider["kind"] == "gemini":
                    body = self._json("POST", provider["base_url"].rstrip("/") + f"/models/{model}:batchEmbedContents",
                        headers={"x-goog-api-key": key}, json={"requests": [{"model": f"models/{model}", "content": {"parts": [{"text": t}]}} for t in batch]})
                    vectors = [e["values"] for e in body.get("embeddings", [])]
                else:
                    body = self._json("POST", provider["base_url"].rstrip("/") + "/embeddings",
                        headers={"Authorization": f"Bearer {key}"}, json={"model": model, "input": batch})
                    vectors = [e["embedding"] for e in sorted(body.get("data", []), key=lambda e: e["index"])]
                used = self.response_usage(body)
                if len(vectors) != len(batch):
                    raise ProviderError("向量接口返回数量与输入不一致")
                values.extend(vectors)
                status = "success"
            finally:
                self.record_usage(provider, model, "embedding", "embedding", started, status, usage=used)
        return values

    def models(self, provider_id):
        settings = self.settings.get()
        provider = next((p for p in settings["providers"] if p["id"] == provider_id), None)
        if not provider:
            raise ValueError("供应商不存在")
        key = self.settings.secret(provider_id)
        headers = {"x-goog-api-key": key} if provider["kind"] == "gemini" else {"Authorization": f"Bearer {key}"}
        body = self._json("GET", provider["base_url"].rstrip("/") + "/models", headers=headers)
        rows = body if isinstance(body, list) else body.get("data", body.get("models", []))
        return {"models": [{"id": m.get("id", m.get("name", "")).removeprefix("models/"), "name": m.get("displayName", m.get("id", m.get("name")))} for m in rows]}

    def test(self, provider_id):
        started = time.monotonic()
        provider = next((p for p in self.settings.get()["providers"] if p["id"] == provider_id), None)
        if provider and urlsplit(provider.get("base_url", "")).hostname == "huggingface.co":
            self._json("GET", "https://huggingface.co/api/whoami-v2", headers={"Authorization": "Bearer " + self.settings.secret(provider_id)})
            return {"ok": True, "message": "Hugging Face 访问令牌有效", "latency_ms": round((time.monotonic() - started) * 1000)}
        result = self.models(provider_id)
        return {"ok": True, "message": f"连接成功，发现 {len(result['models'])} 个模型", "latency_ms": round((time.monotonic()-started)*1000)}

    def transcribe(self, path, *, duration, language=None, model=None, cancel=None):
        provider, model, key = self.resolve("transcribe", model=model)
        path = Path(path)
        if provider["kind"] == "local":
            raise ProviderError("文件转写的本地引擎由媒体任务直接调用")
        started, status, used = time.monotonic(), "error", {}
        try:
            if provider["kind"] == "gemini":
                raise ProviderError("Gemini 原生接口用于问答/视觉；文件转写请选择百炼或 OpenAI 兼容语音接口")
            if provider["kind"] == "dashscope" and ("qwen-audio-3.0-asr" in model or "fun-asr" in model):
                result = self._dash_asr(provider, model, key, path, duration, language, cancel)
            elif provider["kind"] == "dashscope" and "qwen3-asr" in model:
                uri = "data:audio/wav;base64," + base64.b64encode(path.read_bytes()).decode()
                body = self._json("POST", provider["base_url"].rstrip("/") + "/chat/completions", headers={"Authorization": f"Bearer {key}"},
                    json={"model": model, "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": uri}}]}], "asr_options": {"enable_itn": False, **({"language": language} if language and language != "auto" else {})}})
                used = self.response_usage(body)
                text = content_text(body["choices"][0]["message"]["content"])
                result = [{"start": 0, "end": duration, "text": text, "timestamp_source": "chunk"}] if text.strip() else []
            else:
                with path.open("rb") as handle:
                    data = {"model": model, "response_format": "verbose_json"}
                    if language and language != "auto":
                        data["language"] = language
                    body = self._json("POST", provider["base_url"].rstrip("/") + "/audio/transcriptions",
                        headers={"Authorization": f"Bearer {key}"}, files={"file": (path.name, handle, "audio/wav")}, data=data)
                used = self.response_usage(body)
                if cancel:
                    cancel()
                result = body.get("segments") or ([{"start": 0, "end": duration, "text": body["text"], "timestamp_source": "chunk"}] if body.get("text", "").strip() else [])
            status = "success"
            return result
        except Exception as exc:
            status = "cancelled" if "取消" in str(exc) else "error"
            raise
        finally:
            self.record_usage(provider, model, "transcribe", "file_asr", started, status, usage=used, audio_seconds=duration)

    def _dash_asr(self, provider, model, key, path, duration, language, cancel):
        uri = "data:audio/wav;base64," + base64.b64encode(path.read_bytes()).decode()
        payload = {"model": model, "input": {"messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": uri}}]}]}, "parameters": {"format": "wav", "sample_rate": "16000"}}
        if language and language != "auto":
            payload["parameters"]["language_hints"] = [language]
        sentences, full_text = {}, ""
        with self.client.stream("POST", self.native_base(provider) + "/services/aigc/multimodal-generation/generation", json=payload,
                headers={"Authorization": f"Bearer {key}", "X-DashScope-SSE": "enable"}) as response:
            if response.status_code >= 400:
                response.read()
            self._check(response)
            if "text/event-stream" in response.headers.get("content-type", ""):
                events = (json.loads(line[5:]) for line in response.iter_lines() if line.startswith("data:"))
            else:
                response.read()
                events = [response.json()]
            for event in events:
                if cancel:
                    cancel()
                if event.get("code"):
                    raise ProviderError(str(event.get("message", event["code"])))
                output = event.get("output", {})
                full_text = output.get("text", full_text)
                sentence = output.get("sentence")
                if sentence and sentence.get("sentence_end", True):
                    sentences[sentence.get("sentence_id", len(sentences))] = {"start": sentence.get("begin_time", 0)/1000, "end": sentence.get("end_time", duration*1000)/1000, "text": sentence.get("text", ""), "timestamp_source": "model"}
        result = [sentences[k] for k in sorted(sentences)]
        # Some non-streaming endpoints return only last sentence plus full text.
        joined = "".join(s["text"] for s in result)
        if full_text.strip() and len(joined) < len(full_text.strip()) * .85:
            return [{"start": 0, "end": duration, "text": full_text, "timestamp_source": "chunk"}]
        return result

    def tts(self, text, path, voice="Cherry", *, instructions=None):
        provider, model, key = self.resolve("tts")
        started, status, used = time.monotonic(), "error", {}
        try:
            if provider["kind"] == "dashscope":
                body = self._json("POST", self.native_base(provider) + "/services/aigc/multimodal-generation/generation", headers={"Authorization": f"Bearer {key}"},
                    json={"model": model, "input": {"text": text, "voice": voice, "language_type": "Auto", **({"instructions": instructions} if instructions else {})}})
                used = self.response_usage(body)
                audio = body.get("output", {}).get("audio", {})
                if audio.get("data"):
                    Path(path).write_bytes(base64.b64decode(audio["data"]))
                elif audio.get("url"):
                    response = self._check(self.client.get(audio["url"]))
                    Path(path).write_bytes(response.content)
                else:
                    raise ProviderError("语音合成未返回音频")
            elif provider["kind"] == "openai":
                response = self._check(self.client.post(provider["base_url"].rstrip("/") + "/audio/speech", headers={"Authorization": f"Bearer {key}"}, json={"model": model, "input": text, "voice": voice, "response_format": "wav"}))
                Path(path).write_bytes(response.content)
            else:
                raise ProviderError("此语音合成适配器需要百炼或 OpenAI 兼容供应商")
            status = "success"
            return str(path)
        finally:
            self.record_usage(provider, model, "tts", "tts", started, status, usage=used)
