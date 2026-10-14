"""配置加载与校验。

设计原则：配置文件缺失或字段缺失时给出可用默认值，只有真正矛盾的值才报错，
这样用户升级程序时不会因为少写一个字段而整个服务起不来。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("timemsg.config")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"
EXAMPLE_CONFIG_PATH = ROOT / "config.example.json"

# 环境变量里的 API key 优先级低于 config.json 中的显式配置，但方便临时调试。
API_KEY_ENV_ORDER = ("TIMEMSG_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")


class ConfigError(RuntimeError):
    """配置文件存在明确错误。"""


def _parse_time(value: Any, name: str) -> dtime:
    if isinstance(value, dtime):
        return value
    try:
        parts = str(value).strip().split(":")
        return dtime(int(parts[0]), int(parts[1]))
    except Exception as exc:  # noqa: BLE001 - 需要把任意解析错误统一成配置错误
        raise ConfigError(f"配置项 {name} 不是合法时间（应为 HH:MM）：{value!r}") from exc


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


@dataclass
class AIConfig:
    base_url: str = "https://api.deepseek.com"
    path: str = "/chat/completions"
    model: str = "deepseek-chat"
    api_key: str = ""
    temperature: float = 1.5
    max_tokens: int = 300
    timeout: float = 40.0
    attempts: int = 3
    backoff: float = 15.0
    mock: bool = False

    @property
    def url(self) -> str:
        return self.base_url.rstrip("/") + "/" + self.path.lstrip("/")


@dataclass
class PromptConfig:
    system: str = ""
    system_file: str = ""
    min_chars: int = 12
    max_chars: int = 60
    themes: List[str] = field(default_factory=list)
    styles: List[str] = field(default_factory=list)
    keep_newlines: bool = False  # 是否保留多段落（长问候需要，一句话模式不要）
    hard_max_chars: int = 0  # 硬上限，0 表示自动取 max_chars * 3
    daily_reminder_rate: float = 0.8  # 每天允许出现一句「天气/穿衣/吃饭」这类日常提醒的概率


@dataclass
class ContextConfig:
    """个人资料目录：每天生成前读取，作为 AI 的上下文。"""

    enabled: bool = True
    directory: str = "profile"
    max_total_chars: int = 30000
    max_file_chars: int = 20000
    strip_markdown: bool = True
    extensions: List[str] = field(default_factory=lambda: [".md", ".txt"])


@dataclass
class TunnelConfig:
    """Cloudflare Tunnel 配置。

    named 模式有两种跑法：
    * 本地托管：`cloudflared tunnel login` 拿到 cert.pem，填 named_tunnel
    * 控制台托管：在 Cloudflare 后台建隧道，把隧道 token 填到 token（推荐，不用 cert.pem）
    """

    mode: str = "off"  # off | quick | named
    cloudflared: str = ""  # 留空则自动查找
    named_tunnel: str = ""  # 本地托管模式下的隧道名
    token: str = ""  # 控制台托管模式下的隧道 token
    public_url: str = ""  # 固定域名，例如 https://timemsg.example.com
    bind_localhost: bool = True  # 开隧道时只监听 127.0.0.1，不让同网段绕过 Cloudflare


@dataclass
class Config:
    path: Path
    listen_host: str = "0.0.0.0"
    listen_port: int = 8765
    token: str = ""
    window_start: dtime = dtime(6, 30)
    window_end: dtime = dtime(8, 30)
    nothing_sentinel: str = "__NOTHING__"
    once_grace_minutes: int = 15
    db_path: Path = ROOT / "data" / "timemsg.db"
    log_path: Path = ROOT / "logs" / "timemsg.log"
    ai: AIConfig = field(default_factory=AIConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    tunnel: TunnelConfig = field(default_factory=TunnelConfig)
    context_dir: Path = ROOT / "profile"

    @property
    def window_text(self) -> str:
        return f"{self.window_start.strftime('%H:%M')}~{self.window_end.strftime('%H:%M')}"


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """读取配置；文件不存在时用默认值并在调用方提示。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw: Dict[str, Any] = {}
    if cfg_path.exists():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"配置文件不是合法 JSON：{cfg_path}\n{exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"配置文件顶层必须是对象：{cfg_path}")
    elif EXAMPLE_CONFIG_PATH.exists():
        raw = json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))

    ai_raw = raw.get("ai") or {}
    prompt_raw = raw.get("prompt") or {}

    cfg = Config(path=cfg_path)
    cfg.listen_host = str(raw.get("listen_host", cfg.listen_host))
    cfg.listen_port = int(raw.get("listen_port", cfg.listen_port))
    cfg.token = str(raw.get("token", cfg.token) or "").strip()
    cfg.window_start = _parse_time(raw.get("window_start", "06:30"), "window_start")
    cfg.window_end = _parse_time(raw.get("window_end", "08:30"), "window_end")
    cfg.nothing_sentinel = str(raw.get("nothing_sentinel", cfg.nothing_sentinel))
    cfg.once_grace_minutes = int(raw.get("once_grace_minutes", cfg.once_grace_minutes))

    if raw.get("db_path"):
        cfg.db_path = (ROOT / str(raw["db_path"])).resolve()
    if raw.get("log_path"):
        cfg.log_path = (ROOT / str(raw["log_path"])).resolve()

    a = AIConfig()
    a.base_url = str(ai_raw.get("base_url", a.base_url))
    a.path = str(ai_raw.get("path", a.path))
    a.model = str(ai_raw.get("model", a.model))
    a.api_key = str(ai_raw.get("api_key", "") or "").strip()
    if not a.api_key:
        for env_name in API_KEY_ENV_ORDER:
            if os.environ.get(env_name):
                a.api_key = os.environ[env_name].strip()
                break
    a.temperature = float(ai_raw.get("temperature", a.temperature))
    a.max_tokens = int(ai_raw.get("max_tokens", a.max_tokens))
    a.timeout = float(ai_raw.get("timeout_seconds", a.timeout))
    a.attempts = max(1, int(ai_raw.get("attempts", a.attempts)))
    a.backoff = max(0.0, float(ai_raw.get("backoff_seconds", a.backoff)))
    a.mock = _as_bool(ai_raw.get("mock"), False)
    cfg.ai = a

    p = PromptConfig()
    p.system_file = str(prompt_raw.get("system_file", "") or "").strip()
    if p.system_file:
        # 提示词放在独立的纯文本文件里，改起来不用跟 JSON 转义搏斗
        sf_path = (ROOT / p.system_file).resolve()
        if not sf_path.exists():
            raise ConfigError(f"prompt.system_file 指向的文件不存在：{sf_path}")
        p.system = sf_path.read_text(encoding="utf-8").strip()
    else:
        p.system = str(prompt_raw.get("system", "") or "")
    p.min_chars = int(prompt_raw.get("min_chars", p.min_chars))
    p.max_chars = int(prompt_raw.get("max_chars", p.max_chars))
    p.themes = list(prompt_raw.get("themes") or [])
    p.styles = list(prompt_raw.get("styles") or [])
    p.keep_newlines = _as_bool(prompt_raw.get("keep_newlines"), p.keep_newlines)
    p.hard_max_chars = int(prompt_raw.get("hard_max_chars", p.hard_max_chars) or 0)
    p.daily_reminder_rate = min(
        1.0, max(0.0, float(prompt_raw.get("daily_reminder_rate", p.daily_reminder_rate)))
    )
    cfg.prompt = p

    ctx_raw = raw.get("context") or {}
    c = ContextConfig()
    c.enabled = _as_bool(ctx_raw.get("enabled"), c.enabled)
    c.directory = str(ctx_raw.get("directory", c.directory) or "profile")
    c.max_total_chars = max(500, int(ctx_raw.get("max_total_chars", c.max_total_chars)))
    c.max_file_chars = max(200, int(ctx_raw.get("max_file_chars", c.max_file_chars)))
    c.strip_markdown = _as_bool(ctx_raw.get("strip_markdown"), c.strip_markdown)
    exts = ctx_raw.get("extensions")
    if isinstance(exts, list) and exts:
        c.extensions = [str(e).lower() if str(e).startswith(".") else "." + str(e).lower() for e in exts]
    cfg.context = c
    cfg.context_dir = (ROOT / c.directory).resolve()

    tun_raw = raw.get("tunnel") or {}
    t = TunnelConfig()
    mode = str(tun_raw.get("mode", t.mode) or "off").strip().lower()
    if mode not in ("off", "quick", "named"):
        raise ConfigError(f"tunnel.mode 只能是 off / quick / named，当前为 {mode!r}")
    t.mode = mode
    t.cloudflared = str(tun_raw.get("cloudflared", "") or "").strip()
    t.named_tunnel = str(tun_raw.get("named_tunnel", "") or "").strip()
    t.token = str(tun_raw.get("token", "") or "").strip()
    t.public_url = str(tun_raw.get("public_url", "") or "").strip().rstrip("/")
    t.bind_localhost = _as_bool(tun_raw.get("bind_localhost"), True)
    cfg.tunnel = t

    if cfg.window_start >= cfg.window_end:
        raise ConfigError(
            f"window_start({cfg.window_start.strftime('%H:%M')}) 必须早于 "
            f"window_end({cfg.window_end.strftime('%H:%M')})"
        )
    return cfg


def load_system_prompt(cfg: "Config") -> str:
    """每次生成时重新读提示词文件，这样改完立刻生效，不用重启服务。

    和 profile/ 资料目录一个待遇：文件是你的，程序每次现读。
    """
    if cfg.prompt.system_file:
        path = (ROOT / cfg.prompt.system_file).resolve()
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
            log.warning("提示词文件是空的，回退到启动时加载的版本：%s", path)
        except OSError as exc:
            log.warning("提示词文件读取失败（%s），回退到启动时加载的版本", exc)
    return cfg.prompt.system


def refresh_tunables(cfg: "Config") -> "Config":
    """重新读取配置里只影响「生成」的部分：prompt / ai / context。

    这样改字数、概率、模型、上下文上限这类参数就不用重启服务了。
    监听地址、端口、隧道这些和进程绑定太紧的设置，仍然只在启动时读一次。
    配置文件写坏了就沿用启动时的值，不会把服务弄挂。
    """
    try:
        fresh = load_config(cfg.path)
    except (ConfigError, OSError) as exc:
        log.warning("重新读取配置失败，沿用启动时的设置：%s", exc)
        return cfg

    changed = []
    if fresh.prompt != cfg.prompt:
        changed.append("prompt")
    if fresh.ai != cfg.ai:
        changed.append("ai")
    if fresh.context != cfg.context:
        changed.append("context")

    cfg.prompt = fresh.prompt
    cfg.ai = fresh.ai
    cfg.context = fresh.context
    cfg.context_dir = fresh.context_dir
    cfg.once_grace_minutes = fresh.once_grace_minutes
    if changed:
        log.info("配置已热加载：%s", "、".join(changed))
    return cfg
