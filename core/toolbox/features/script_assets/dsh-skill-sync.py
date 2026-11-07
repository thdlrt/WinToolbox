#!/usr/bin/env python3
"""让 DeepSeek Harness 直接读取 cc-switch 维护的技能库。

用法（由「脚本工具」运行时不需要任何参数）：
    python dsh-skill-sync.py [--dry-run] [--library DIR] [--target cordis.patch.yml]
                             [--profile desktop] [--dsh-home DIR] [--agents-home DIR]

做两件事：
1. 在 Harness profile 的 cordis.patch.yml 里维护一段插件行，把技能库目录挂成
   一个独立的 skill-filesystem 提供方（providerName: cc-switch）。这样技能库由
   cc-switch 维护，Harness 只当读取端，增删技能不需要同步或复制。
2. 检查技能库里每个技能的 frontmatter：Harness 对名字不是 kebab-case 或缺
   description 的技能会静默跳过，这里把它们点名出来。

生成内容只位于下面两行标记之间，标记之外的内容原样保留；写入前备份原文件。
需要改行为时直接编辑本文件（工具箱「脚本工具」页有「打开脚本文件」）。
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

MARK_START = "# >>> dsh-skill-sync"
MARK_END = "# <<< dsh-skill-sync"
ROW_ID = "skill-ccswitch"
PROVIDER = "cc-switch"
PACKAGE = "@deepseek-ai/dsh-skill-filesystem"
# Harness 的公开技能名语法，与 dsh-skill 的 SKILL_NAME 一致。
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PROFILE_NAME = re.compile(r"[^/\\]+")
FRONTMATTER_LIMIT = 8192


def scalar(value):
    return json.dumps(value, ensure_ascii=False)


def frontmatter(path):
    """Return (name, description) as written, or (None, None) without a block."""
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            text = handle.read(FRONTMATTER_LIMIT)
    except OSError:
        return None, None, "文件无法读取"
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None, "缺少 frontmatter（文件应以 --- 开头）"
    block = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        block.append(line)
    else:
        return None, None, "frontmatter 没有结束的 ---"
    body = "\n".join(block)
    name = re.search(r"^name:\s*(.+?)\s*$", body, re.M)
    description = re.search(r"^description:\s*(.*?)\s*$", body, re.M)
    value = name.group(1).strip().strip('"\'') if name else None
    described = description.group(1).strip().strip('"\'') if description else ""
    return value, described, None


def scan(library):
    """Every entry the provider would see, plus the ones Harness would drop."""
    skills, broken, ignored = [], [], []
    for child in sorted(library.iterdir(), key=lambda item: item.name.lower()):
        if child.is_dir():
            path = child / "SKILL.md"
            if not path.is_file():
                ignored.append(child.name)
                continue
        elif child.is_file() and child.suffix.lower() == ".md":
            path = child
        else:
            ignored.append(child.name)
            continue
        name, description, problem = frontmatter(path)
        label = name or (child.stem if child.is_file() else child.name)
        if problem:
            broken.append(f"{label}：{problem}")
            continue
        if not SKILL_NAME.match(label):
            broken.append(f"{label}：name 必须是 kebab-case（小写字母、数字与单个连字符）")
            continue
        if not description:
            broken.append(f"{label}：缺少 description")
            continue
        skills.append(label)
    return skills, broken, ignored


def render(library):
    return "\n".join([
        MARK_START,
        "# 由 WinToolbox 脚本工具生成，请勿手改本段；下次同步会整段替换。",
        "- insert:",
        f"    - id: {ROW_ID}",
        f"      name: {scalar(PACKAGE)}",
        "      config:",
        f"        providerName: {scalar(PROVIDER)}",
        "        includeDefaultRoots: false",
        "        customSkillDirs:",
        f"          - {scalar(library)}",
        MARK_END,
    ])


def apply_region(existing, region):
    lines = existing.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if line.strip().startswith(MARK_START)), None)
    if start is not None:
        end = next((index for index in range(start + 1, len(lines)) if lines[index].strip().startswith(MARK_END)), None)
        if end is None:
            raise SystemExit("补丁文件的 dsh-skill-sync 起始标记没有对应的结束标记，请先修好该段")
        return "".join(lines[:start] + [f"{line}\n" for line in region.splitlines()] + lines[end + 1:])
    text = existing
    if text and not text.endswith("\n"):
        text += "\n"
    if text.strip():
        text += "\n"
    return text + region + "\n"


def duplicate_line(existing):
    """A hand-written row with the same id would mount the provider twice."""
    inside = False
    for number, line in enumerate(existing.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(MARK_START):
            inside = True
            continue
        if stripped.startswith(MARK_END):
            inside = False
            continue
        if not inside and re.match(rf"-\s*id:\s*{re.escape(ROW_ID)}\s*$", stripped):
            return number
    return None


def paths(options):
    library = Path(
        options.library
        or os.environ.get("DSH_SKILL_LIBRARY")
        or Path.home() / ".cc-switch" / "skills"
    ).expanduser()
    dsh_home = Path(options.dsh_home or os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()
    profile = str(options.profile or os.environ.get("DSH_PROFILE") or "desktop")
    if not PROFILE_NAME.fullmatch(profile) or profile in (".", "..", "node_modules"):
        raise SystemExit("DSH profile 名称不合法")
    target = Path(options.target or dsh_home / "profiles" / profile / "cordis.patch.yml").expanduser()
    agents = Path(options.agents_home or os.environ.get("DSH_AGENTS_HOME") or Path.home() / ".agents").expanduser()
    return library, target, agents


def main(argv=None):
    parser = argparse.ArgumentParser(description="让 DeepSeek Harness 读取 cc-switch 维护的技能库")
    parser.add_argument("--dry-run", action="store_true", help="只显示将要写入的内容")
    parser.add_argument("--library", help="技能库目录（默认 ~/.cc-switch/skills 或 DSH_SKILL_LIBRARY）")
    parser.add_argument("--target", help="Harness profile 的 cordis.patch.yml")
    parser.add_argument("--dsh-home", help="Harness 目录（默认 DSH_HOME 或 ~/.dsh）")
    parser.add_argument("--agents-home", help="共享技能目录（默认 ~/.agents），仅用于统计")
    parser.add_argument("--profile", help="Harness profile（默认 DSH_PROFILE 或 desktop）")
    options = parser.parse_args(argv)

    library, target, agents = paths(options)
    print(f"技能库：{library}")
    print(f"目标：{target}")
    if not library.is_dir():
        raise SystemExit(f"找不到技能库：{library}。请在 cc-switch 里确认技能库位置，或用 --library 指定。")
    skills, broken, ignored = scan(library)
    print(f"发现 {len(skills) + len(broken)} 个技能，其中 {len(skills)} 个可用。")
    for item in broken:
        print("  ! " + item)
    if ignored:
        print(f"另外 {len(ignored)} 项不是技能目录（没有 SKILL.md），已忽略。")
    other = agents / "skills"
    if other.is_dir():
        print(f"Harness 还会读取的另一个来源：{other}（{len(list(other.iterdir()))} 项）")

    region = render(str(library).replace("\\", "/"))
    existing = target.read_text(encoding="utf-8-sig") if target.is_file() else ""
    bom = target.read_bytes().startswith(b"\xef\xbb\xbf") if target.is_file() else False
    conflict = duplicate_line(existing)
    if conflict is not None:
        raise SystemExit(
            f"补丁文件第 {conflict} 行已经有一个标记段外的 {ROW_ID} 条目，"
            "再写一份会把这个提供方挂载两次。请先删除那一行所在的整段，再运行本脚本。"
        )
    text = apply_region(existing, region)
    ending = "\r\n" if "\r\n" in existing else "\n"
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
    print(f"已写入提供方 {PROVIDER}。Harness 重载配置后，技能目录里会多出这 {len(skills)} 个技能。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as error:  # 给出可读失败原因，而不是堆栈
        print(f"同步失败：{error}", file=sys.stderr)
        sys.exit(1)
