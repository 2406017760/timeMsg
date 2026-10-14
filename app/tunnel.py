"""Cloudflare Tunnel 管理：让 iPhone 在任何网络（含蜂窝）下都能访问本机接口。

两种模式：
* quick：  cloudflared tunnel --url http://127.0.0.1:8765
           免账号、免域名，但**每次重启 cloudflared 域名都会变**，
           快捷指令里的 URL 就得跟着改。适合先试通。
* named：  cloudflared tunnel run --url http://127.0.0.1:8765 <隧道名>
           需要 Cloudflare 账号 + 一个托管在 Cloudflare 的域名，
           域名固定不变，适合长期无人值守。

设计要点：隧道进程由 TimeMsg 自己托管。程序被计划任务拉起时，
隧道跟着起来；隧道崩了就按指数退避重启；拿到的公网地址会写进
data/public_url.txt，并出现在 /health 里，省得你去翻日志。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, List, Optional

from .config import Config

log = logging.getLogger("timemsg.tunnel")

QUICK_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# named 模式没有公网地址可以解析，只能靠这些关键字判断连接状态，
# 所以它们必须打在中等级别以上，否则排障时什么都看不到。
INFO_HINTS = (
    "Registered tunnel connection",
    "Initial protocol",
    "Updated to new configuration",
    "Generated Connector ID",
    "connIndex=",
    "ICMP proxy",
    "Starting tunnel",
)
WARN_HINTS = ("ERR", "WRN", "error", "failed", "retry")


def _candidate_paths() -> List[Path]:
    paths: List[Path] = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            paths.append(Path(base) / "cloudflared" / "cloudflared.exe")
    return paths


def find_cloudflared(explicit: str = "") -> Optional[str]:
    """按显式配置 → PATH → 常见安装目录 → winget 包目录的顺序找可执行文件。"""
    if explicit:
        p = Path(explicit)
        if p.exists():
            return str(p)
        log.warning("配置里指定的 cloudflared 不存在：%s", explicit)

    found = shutil.which("cloudflared")
    if found:
        return found

    for path in _candidate_paths():
        if path.exists():
            return str(path)

    packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if packages.exists():
        for exe in sorted(packages.glob("Cloudflare.cloudflared*/**/cloudflared.exe")):
            return str(exe)
    return None


class TunnelManager:
    """托管一个 cloudflared 子进程，并在拿到公网地址时回调。"""

    def __init__(self, cfg: Config, on_url: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.on_url = on_url
        self.exe = find_cloudflared(cfg.tunnel.cloudflared)
        self._stop = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self.public_url: str = (cfg.tunnel.public_url or "").rstrip("/")

    # ------------------------------------------------------------- 生命周期
    @property
    def enabled(self) -> bool:
        return self.cfg.tunnel.mode in ("quick", "named")

    def start(self) -> bool:
        if not self.enabled:
            log.info("隧道模式为 off，仅本机/局域网可访问")
            return False
        if not self.exe:
            log.error(
                "找不到 cloudflared，请先安装：winget install -e --id Cloudflare.cloudflared"
            )
            return False
        if self.cfg.tunnel.mode == "named" and not (self.cfg.tunnel.named_tunnel or self.cfg.tunnel.token):
            log.error("named 模式必须配置 tunnel.named_tunnel（本地托管）或 tunnel.token（控制台托管）")
            return False

        self._thread = threading.Thread(target=self._supervise, name="tunnel", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        # 给日志一点时间收尾
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    # --------------------------------------------------------------- 内部
    def _args(self) -> List[str]:
        local = f"http://127.0.0.1:{self.cfg.listen_port}"
        exe = self.exe or "cloudflared"
        base = [exe, "tunnel", "--no-autoupdate"]
        if self.cfg.tunnel.mode == "quick":
            return base + ["--url", local]
        # 控制台托管：隧道配置（公网域名 -> 本地服务）在 Cloudflare 后台，
        # 本地只需要 token，不依赖 cert.pem。
        if self.cfg.tunnel.token:
            return base + ["run", "--token", self.cfg.tunnel.token]
        # 本地托管：cert.pem + 隧道名，用 --url 覆盖 ingress，省掉 config.yml
        return base + ["run", "--url", local, self.cfg.tunnel.named_tunnel]

    def _supervise(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            try:
                self._run_once()
                backoff = 5
            except FileNotFoundError:
                log.error("cloudflared 无法启动：%s", self.exe)
                return
            except Exception as exc:  # noqa: BLE001 - 隧道不能因为单次异常彻底停掉
                log.exception("隧道进程异常：%s", exc)
            if self._stop.is_set():
                break
            log.warning("%s 秒后重启隧道", backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 120)

    def _run_once(self) -> None:
        args = self._args()
        log.info("启动隧道：%s", " ".join(args))
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        self._proc = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            if self._stop.is_set():
                break
            line = line.rstrip()
            if not line:
                continue
            match = QUICK_URL_RE.search(line)
            if match:
                self._set_url(match.group(0))
            elif any(hint in line for hint in INFO_HINTS):
                log.info("cloudflared | %s", line)
            elif any(hint in line for hint in WARN_HINTS):
                log.warning("cloudflared | %s", line)
            else:
                log.debug("cloudflared | %s", line)
        proc.wait()
        if not self._stop.is_set():
            log.warning("隧道进程退出，返回码 %s", proc.returncode)

    def _set_url(self, url: str) -> None:
        if url == self.public_url:
            return
        self.public_url = url
        try:
            out = self.cfg.db_path.parent / "public_url.txt"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(url + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001 - 写不进去不影响主流程
            log.debug("写入 public_url.txt 失败", exc_info=True)
        log.info("公网地址已就绪：%s", url)
        log.info("快捷指令 URL：%s", self.shortcut_url())
        if self.on_url:
            try:
                self.on_url(url)
            except Exception:  # noqa: BLE001
                log.debug("on_url 回调失败", exc_info=True)

    # --------------------------------------------------------------- 辅助
    def shortcut_url(self, path: str = "/today") -> str:
        base = self.public_url or f"http://127.0.0.1:{self.cfg.listen_port}"
        from urllib.parse import urlencode

        query = urlencode({"k": self.cfg.token, "once": "1", "plain": "1"})
        return f"{base}{path}?{query}"
