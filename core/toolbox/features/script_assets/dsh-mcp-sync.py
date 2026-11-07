#!/usr/bin/env python3
"""把 Codex 的 [mcp_servers] 同步成 DeepSeek Harness 的 dsh-mcp-client 插件行。

用法（由「脚本工具」运行时不需要任何参数）：
    python dsh-mcp-sync.py [--dry-run] [--source config.toml] [--target cordis.patch.yml]
                           [--exclude a,b] [--profile desktop] [--dsh-home DIR]

默认路径读环境变量 CODEX_HOME / DSH_HOME / DSH_PROFILE，未设置时用用户目录下的
.codex / .dsh 与 desktop profile。生成内容只位于下面两行标记之间，标记之外的内容
原样保留；写入前备份原文件，重复运行整段替换而不是追加。

需要改行为时直接编辑本文件（工具箱「脚本工具」页有「打开脚本文件」）；改了以后
工具箱不会再自动覆盖它。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - the toolbox ships Python 3.12
    tomllib = None

# 默认跳过的服务器：Codex 自带的 computer-use 运行时不是给 Harness 用的。
# 也可以在命令行用 --exclude a,b 覆盖（传空字符串表示一个都不跳过）。
EXCLUDE = ("node_repl",)

MARK_START = "# >>> dsh-mcp-sync"
MARK_END = "# <<< dsh-mcp-sync"
ROW_NAME = "@deepseek-ai/dsh-mcp-client"
SERVER_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
SCRUBBED_NAME = re.compile(r"KEY|PASSWORD|SECRET|TOKEN", re.I)
PROFILE_NAME = re.compile(r"[^/\\]+")


def scalar(value):
    """JSON scalars are valid YAML scalars."""
    return json.dumps(value, ensure_ascii=False)


def excluded(value):
    if value is None:
        return {name.lower() for name in EXCLUDE}
    return {item.strip().lower() for item in re.split(r"[,\s]+", value) if item.strip()}


def read_servers(source):
    if tomllib is None:
        raise SystemExit("需要 Python 3.11 及以上才能读取 config.toml")
    if not source.is_file():
        raise SystemExit(f"找不到 Codex 配置：{source}")
    try:
        document = tomllib.loads(source.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise SystemExit(f"Codex 配置无法解析：{source}（{exc}）")
    table = document.get("mcp_servers")
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise SystemExit("Codex 配置中的 mcp_servers 不是表")
    return table


def convert(name, entry, notes):
    """Return the harness config for one Codex entry, or None when it cannot work."""
    if not SERVER_NAME.fullmatch(name):
        notes.append(f"跳过 “{name}”：服务器名必须是 1-32 位字母、数字、下划线或连字符。")
        return None
    if not isinstance(entry, dict):
        notes.append(f"跳过 “{name}”：配置项不是表。")
        return None
    config = {"serverName": name}
    url, command = entry.get("url"), entry.get("command")
    if isinstance(url, str) and url.strip():
        config["transport"] = "streamable-http"
        config["url"] = url.strip()
    elif isinstance(command, str) and command.strip().lower().startswith(("http://", "https://")):
        # Codex 允许把 HTTP 端点写在 command 里，保持同样的含义。
        config["transport"] = "streamable-http"
        config["url"] = command.strip()
        notes.append(f"“{name}” 用 command 写了 HTTP 地址，已按 streamable-http 处理。")
    elif isinstance(command, str) and command.strip():
        config["transport"] = "stdio"
        config["command"] = command.strip()
        args = entry.get("args")
        if isinstance(args, list):
            config["args"] = [str(item) for item in args]
        elif args is not None:
            notes.append(f"“{name}” 的 args 不是列表，已忽略。")
        env = {}
        table = entry.get("env")
        if isinstance(table, dict):
            env.update({str(key): str(value) for key, value in table.items()})
        passthrough = entry.get("env_vars")
        if isinstance(passthrough, list):
            for key in passthrough:
                key = str(key)
                if key in env:
                    continue
                env[key] = "!!js \"process.env['%s'] ?? ''\"" % key
                if SCRUBBED_NAME.search(key):
                    notes.append(f"“{name}” 透传 {key}：该名称会被 Harness 清洗，请改写成固定值。")
        elif passthrough is not None:
            notes.append(f"“{name}” 的 env_vars 不是列表，已忽略。")
        if env:
            config["env"] = env
    else:
        notes.append(f"跳过 “{name}”：既没有 url 也没有 command。")
        return None
    headers = entry.get("http_headers", entry.get("headers"))
    if isinstance(headers, dict) and headers:
        config["headers"] = {str(key): str(value) for key, value in headers.items()}
    ignored = sorted(set(entry) - {"url", "command", "args", "env", "env_vars", "http_headers", "headers"})
    if ignored:
        notes.append(f"“{name}” 未转换的字段：{'、'.join(ignored)}。")
    return config


def row_id(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return "mcp-%s" % (slug or "server")


def render(configs):
    lines = [MARK_START, "# 由 WinToolbox 脚本工具生成，请勿手改本段；下次同步会整段替换。", "- insert:"]
    for config in configs:
        lines.append("    - id: %s" % row_id(config["serverName"]))
        lines.append('      name: "%s"' % ROW_NAME)
        lines.append("      config:")
        for key, value in config.items():
            if key == "env":
                lines.append("        env:")
                for env_key, env_value in value.items():
                    rendered = env_value if str(env_value).startswith("!!js ") else scalar(env_value)
                    lines.append("          %s: %s" % (env_key, rendered))
                continue
            if key == "headers":
                lines.append("        headers:")
                for header_key, header_value in value.items():
                    lines.append("          %s: %s" % (header_key, scalar(header_value)))
                continue
            lines.append("        %s: %s" % (key, scalar(value)))
    lines.append(MARK_END)
    return "\n".join(lines)


def apply_region(existing, region):
    lines = existing.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if line.strip().startswith(MARK_START)), None)
    if start is not None:
        end = next((index for index in range(start + 1, len(lines)) if lines[index].strip().startswith(MARK_END)), None)
        if end is None:
            raise SystemExit("补丁文件的 dsh-mcp-sync 起始标记没有对应的结束标记，请先修好该段")
        return "".join(lines[:start] + [f"{line}\n" for line in region.splitlines()] + lines[end + 1:])
    text = existing
    if text and not text.endswith("\n"):
        text += "\n"
    if text.strip():
        text += "\n"
    return text + region + "\n"


def paths(options):
    codex_home = Path(options.codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    source = Path(options.source or codex_home / "config.toml").expanduser()
    dsh_home = Path(options.dsh_home or os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()
    profile = str(options.profile or os.environ.get("DSH_PROFILE") or "desktop")
    if not PROFILE_NAME.fullmatch(profile) or profile in (".", "..", "node_modules"):
        raise SystemExit("DSH profile 名称不合法")
    target = Path(options.target or dsh_home / "profiles" / profile / "cordis.patch.yml").expanduser()
    return source, target


def main(argv=None):
    parser = argparse.ArgumentParser(description="把 Codex 的 MCP 服务器同步成 DeepSeek Harness 插件行")
    parser.add_argument("--dry-run", action="store_true", help="只显示将要写入的内容")
    parser.add_argument("--source", help="Codex 的 config.toml")
    parser.add_argument("--target", help="Harness profile 的 cordis.patch.yml")
    parser.add_argument("--codex-home", help="Codex 目录（默认 CODEX_HOME 或 ~/.codex）")
    parser.add_argument("--dsh-home", help="Harness 目录（默认 DSH_HOME 或 ~/.dsh）")
    parser.add_argument("--profile", help="Harness profile（默认 DSH_PROFILE 或 desktop）")
    parser.add_argument("--exclude", help="跳过的服务器，逗号分隔；空字符串表示全部同步")
    options = parser.parse_args(argv)

    source, target = paths(options)
    skip = excluded(options.exclude)
    print(f"源：{source}")
    print(f"目标：{target}")
    servers = read_servers(source)
    notes, configs, skipped = [], [], []
    for name, entry in servers.items():
        if name.lower() in skip:
            skipped.append(name)
            continue
        config = convert(name, entry, notes)
        (configs if config else skipped).append(config or name)
    configs.sort(key=lambda item: item["serverName"].lower())
    for config in configs:
        print(f"  + {config['serverName']}（{config['transport']}）")
    if skipped:
        print("跳过：" + "、".join(skipped))
    for note in notes:
        print("提示：" + note)
    if not configs:
        print("没有可同步的 MCP 服务器：补丁中的生成段会为空。")

    region = render(configs)
    existing = target.read_text(encoding="utf-8-sig") if target.is_file() else ""
    bom = target.read_bytes().startswith(b"\xef\xbb\xbf") if target.is_file() else False
    ending = "\r\n" if "\r\n" in existing else "\n"
    text = apply_region(existing, region)
    if ending != "\n":
        text = text.replace("\r\n", "\n").replace("\n", ending)

    if options.dry_run:
        print("\n--- 将要写入的内容 ---")
        print(region)
        print("--- 结束（--dry-run 未写入任何文件）---")
        return 0
    if text == existing:
        print("已是最新，无需写入。")
        return 0
    if target.is_file():
        backup = target.with_name(f"{target.name}.{time.strftime('%Y%m%d-%H%M%S')}.{uuid.uuid4().hex[:8]}.bak")
        backup.write_bytes(target.read_bytes())
        print(f"已备份：{backup.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    print(f"已写入 {len(configs)} 台服务器。Harness 重载配置后即可用。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as error:  # 给出可读失败原因，而不是堆栈
        print(f"同步失败：{error}", file=sys.stderr)
        sys.exit(1)
