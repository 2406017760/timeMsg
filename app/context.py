"""读取「个人资料目录」，拼装成给 AI 的上下文。

设计原则：
* 目录里的内容是用户自己维护的（关于我、作息、今天的安排、睡眠记录……），
  程序每天生成前重新读一遍，所以用户改完立刻在第二天生效。
* 只读，不写。会做**纯文本化**：markdown 的标题符号、加粗、列表标记、链接、
  代码围栏都去掉，反转义 `00\\.` 这类写法，并折叠多余空行。
  这样既符合"给模型看纯文本"的直觉，也能省下大量 token。
* 有明确的体量上限：防止某天把一个巨大的文件塞进提示词，把费用和延迟打爆。
  超长时保留**开头和结尾**——开头通常是身份与核心情况，结尾是最新状态。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from .config import Config

log = logging.getLogger("timemsg.context")

TRUNCATE_MARK = "……（中间省略了一部分）……"
# 模板里没替换掉的占位符，出现就提醒用户
PLACEHOLDER_MARKS = ("［", "］", "{{", "}}", "TODO", "请替换")
# 自己看的备注：行首 // 的行，以及 <!-- --> 注释，都不会发给 AI
COMMENT_LINE_PREFIX = "//"
# 单个文件超过这个大小直接跳过。资料目录是给模型看的，
# 原始导出（Apple 健康 200MB 那种）必须先提炼成小结，不能指望直接读。
MAX_READ_BYTES = 5 * 1024 * 1024


def strip_markdown(text: str) -> str:
    """把 markdown 变成纯文本。"""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    out: List[str] = []
    in_fence = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        line = raw
        if not in_fence:
            if stripped.startswith(COMMENT_LINE_PREFIX):
                continue
            if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
                continue
            line = re.sub(r"^#{1,6}\s*", "", line)  # 标题
            line = re.sub(r"^\s*>\s?", "", line)  # 引用
            line = re.sub(r"^\s*[-*+]\s+", "", line)  # 无序列表
            line = re.sub(r"^\s*\d+[.)]\s+", "", line)  # 有序列表
            line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)  # 加粗
            line = re.sub(r"__(.+?)__", r"\1", line)
            line = re.sub(r"(?<!\*)\*([^*\s][^*]*?)\*(?!\*)", r"\1", line)  # 斜体
            line = re.sub(r"`([^`]+)`", r"\1", line)  # 行内代码
            line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)  # 图片
            line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)  # 链接
            line = re.sub(r"\\([\\`*_{}\[\]()#+\-.!])", r"\1", line)  # 反转义
        out.append(line.strip())
    result = "\n".join(out)
    result = re.sub(r"\n{3,}", "\n\n", result)  # 最多留一个空行
    return result.strip()


def _truncate(text: str, limit: int) -> str:
    """超长时保留开头和结尾：开头是身份与核心情况，结尾是最新状态。"""
    head_len = int(limit * 0.6)
    tail_len = max(1, limit - head_len)
    return (
        text[:head_len].rstrip()
        + "\n\n"
        + TRUNCATE_MARK
        + "\n\n"
        + text[-tail_len:].lstrip()
    )


@dataclass
class ContextBundle:
    text: str = ""
    files: List[str] = field(default_factory=list)
    chars: int = 0
    raw_chars: int = 0
    truncated: List[str] = field(default_factory=list)
    placeholders: List[str] = field(default_factory=list)
    skipped_large: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    def summary(self) -> str:
        if self.empty:
            return "无个人资料"
        saved = self.raw_chars - self.chars
        extra = f"，纯文本化省下 {saved} 字" if saved > 0 else ""
        return f"{len(self.files)} 个文件 / {self.chars} 字（{'、'.join(self.files)}）{extra}"


def _iter_files(root: Path, extensions: List[str]) -> List[Path]:
    found: List[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        # 跳过隐藏文件和隐藏目录里的东西
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() not in extensions:
            continue
        found.append(path)
    return found


def load_context(cfg: Config, max_total_chars: int | None = None) -> ContextBundle:
    """把资料目录里的所有文本文件拼成一段上下文。"""
    bundle = ContextBundle()
    if not cfg.context.enabled:
        return bundle

    root = cfg.context_dir
    if not root.exists():
        log.warning(
            "个人资料目录不存在：%s（建一个目录并放些 .md/.txt 进去，AI 就能结合你的情况写）",
            root,
        )
        return bundle

    limit = max_total_chars or cfg.context.max_total_chars
    chunks: List[str] = []
    used = 0

    for path in _iter_files(root, cfg.context.extensions):
        rel = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > MAX_READ_BYTES:
            log.warning(
                "跳过过大的资料文件 %s（%.1f MB）：原始导出要先提炼成小结再放进来",
                rel,
                size / 1048576,
            )
            bundle.skipped_large.append(rel)
            continue

        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("读取资料文件失败：%s（%s）", path, exc)
            continue

        bundle.raw_chars += len(raw)
        text = strip_markdown(raw) if cfg.context.strip_markdown else raw.strip()
        if not text:
            continue

        if len(text) > cfg.context.max_file_chars:
            text = _truncate(text, cfg.context.max_file_chars)
            bundle.truncated.append(rel)
            log.warning(
                "资料文件 %s 内容超过 %s 字，已保留开头与结尾（建议自己精简一下）",
                rel,
                cfg.context.max_file_chars,
            )

        if any(mark in text for mark in PLACEHOLDER_MARKS):
            bundle.placeholders.append(rel)

        block = f"【文件：{rel}】\n{text}"
        if used + len(block) > limit:
            log.warning("个人资料总量超过 %s 字，已忽略后面的文件：%s", limit, path.name)
            break
        chunks.append(block)
        bundle.files.append(rel)
        used += len(block)

    bundle.text = "\n\n".join(chunks)
    bundle.chars = len(bundle.text)
    if bundle.text:
        log.info("已载入个人资料：%s", bundle.summary())
        if bundle.placeholders:
            log.warning(
                "这些资料文件里还有没替换的模板占位符：%s", "、".join(bundle.placeholders)
            )
    return bundle
