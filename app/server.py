"""给 iPhone 快捷指令用的极简 HTTP 接口。

只有两个调用方：你自己的 iPhone 快捷指令，和你自己在浏览器里排查问题。
所以接口故意做得很少，并且默认要求带 token。
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from . import __version__
from .config import Config
from .store import Store

log = logging.getLogger("timemsg.server")

VERSION = __version__


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = f"TimeMsg/{VERSION}"
    protocol_version = "HTTP/1.1"

    # 由工厂注入
    cfg: Config
    store: Store
    generate_now: Callable[[bool], Optional[str]]
    get_public_url: Callable[[], str]

    # ------------------------------------------------------------- 工具
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, obj: Any) -> None:
        self._send(status, _json_bytes(obj), "application/json; charset=utf-8")

    def _send_text(self, status: int, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _auth_ok(self, params: Dict[str, list]) -> bool:
        if not self.cfg.token:
            return True  # 未设置 token 时不校验，但启动时已经警告过
        supplied = (params.get("k") or params.get("token") or [""])[0]
        if not supplied:
            supplied = self.headers.get("X-Token", "") or ""
        # 常数时间比较，避免通过响应时间猜 token
        return hmac.compare_digest(supplied.strip(), self.cfg.token)

    def _from_tunnel(self) -> bool:
        """判断请求是否经过 Cloudflare。隧道请求的来源地址也是 127.0.0.1，
        所以必须靠 Cloudflare 注入的头部来区分。"""
        return bool(
            self.headers.get("CF-Connecting-IP")
            or self.headers.get("CF-Ray")
            or self.headers.get("X-Forwarded-For")
        )

    def _is_trusted_local(self) -> bool:
        host = (self.client_address[0] or "").strip()
        local = host.startswith("127.") or host in ("::1", "localhost")
        return local and not self._from_tunnel()

    def _shortcut_url(self) -> str:
        base = self.get_public_url() or f"http://127.0.0.1:{self.cfg.listen_port}"
        query = urlencode({"k": self.cfg.token})
        return f"{base}/pull?{query}"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # 记 UA 和 Cloudflare 来源 IP：排障时要能分清是快捷指令发的还是浏览器发的。
        ua = self.headers.get("User-Agent", "-") if getattr(self, "headers", None) else "-"
        cf = self.headers.get("CF-Connecting-IP", "-") if getattr(self, "headers", None) else "-"
        log.info(
            "%s - %s | UA=%s CF=%s",
            self.address_string(),
            fmt % args,
            str(ua)[:70],
            cf,
        )

    # ------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 手机端提前断开，属正常情况
        except Exception as exc:  # noqa: BLE001 - 任何异常都必须变成一次响应
            log.exception("处理 GET %s 失败", self.path)
            self._safe_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            log.exception("处理 POST %s 失败", self.path)
            self._safe_error(exc)

    def _safe_error(self, exc: Exception) -> None:
        try:
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
        except Exception:  # noqa: BLE001 - 连响应都发不出去就只能放弃
            pass

    def _route_get(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if path in ("/", "/index"):
            return self._send_json(
                200,
                {
                    "name": "TimeMsg",
                    "version": VERSION,
                    "window": self.cfg.window_text,
                    "endpoints": ["/health", "/today?k=TOKEN&once=1&plain=1", "/history?k=TOKEN"],
                },
            )

        if path == "/health":
            # 走公网隧道时，未鉴权的请求只得到最小信息，避免泄露作息与用量。
            if not self._auth_ok(params):
                return self._send_json(200, {"ok": True, "name": "TimeMsg", "version": VERSION})
            day = datetime.now().strftime("%Y-%m-%d")
            row = self.store.get(day)
            return self._send_json(
                200,
                {
                    "ok": True,
                    "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "today": day,
                    "window": self.cfg.window_text,
                    "target_at": (row or {}).get("target_at"),
                    "ready": bool((row or {}).get("text")),
                    "delivered": bool((row or {}).get("delivered_at")),
                    "public_url": self.get_public_url() or None,
                    "shortcut_url": self._shortcut_url(),
                    "stats": self.store.stats(),
                },
            )

        if path == "/setup":
            # 只在「本机浏览器、且不是从隧道进来」时可用，否则会把 token 泄到公网。
            if not self._is_trusted_local():
                return self._send_json(403, {"error": "该页面只能在本机浏览器打开"})
            return self._send_text(200, self._setup_text())

        if path in ("/today", "/pull"):
            # /pull 是给手机快捷指令用的短别名：等价于 /today?once=1&plain=1。
            # URL 短一半、少两组 & 和 =，在手机上手动输入时不容易打错。
            if path == "/pull":
                params = dict(params)
                params.setdefault("once", ["1"])
                params.setdefault("plain", ["1"])
            return self._today(params)

        if path == "/history":
            if not self._auth_ok(params):
                return self._send_json(401, {"error": "token 不正确"})
            limit = int((params.get("limit") or ["30"])[0])
            return self._send_json(200, {"items": self.store.history(max(1, min(limit, 365)))})

        return self._send_json(404, {"error": f"未知路径 {path}"})

    def _route_post(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"
        if path == "/generate":
            if not self._auth_ok(params):
                return self._send_json(401, {"error": "token 不正确"})
            force = (params.get("force") or ["0"])[0] in ("1", "true", "yes")
            text = self.generate_now(force)
            if text is None:
                return self._send_json(409, {"error": "今天已有内容，未重新生成（可用 force=1 强制）"})
            return self._send_json(200, {"ok": True, "text": text})
        return self._send_json(404, {"error": f"未知路径 {path}"})

    # ------------------------------------------------------------- /today
    def _setup_text(self) -> str:
        """给用户看的「填进快捷指令的 URL」，只在本机可见。"""
        public = self.get_public_url()
        lines = [
            "把下面这一整行复制到 iPhone 快捷指令的「获取 URL 内容」里：",
            "",
            self._shortcut_url(),
            "",
            f"公网地址来源：{'Cloudflare Tunnel（' + self.cfg.tunnel.mode + '）' if public else '未启用隧道，当前是本机地址'}",
            f"发送窗口：{self.cfg.window_text}",
            "",
            "快捷指令逻辑：",
            "  1) 获取 URL 内容（GET，超时 15 秒）",
            "  2) 如果 URL内容 不包含 __NOTHING__ → 发送信息给自己（内容用「URL 内容」变量）",
            "  3) 结束如果",
            "",
            f"（/pull 是 /today?once=1&plain=1 的短别名，两种写法等价）",
            f"本机状态页：http://127.0.0.1:{self.cfg.listen_port}/health?k={self.cfg.token}",
        ]
        return "\n".join(lines)

    def _today(self, params: Dict[str, list]) -> None:
        if not self._auth_ok(params):
            return self._send_json(401, {"error": "token 不正确"})

        date_arg = (params.get("date") or [""])[0].strip()
        day = date_arg or datetime.now().strftime("%Y-%m-%d")
        once = (params.get("once") or ["0"])[0] in ("1", "true", "yes")
        plain = (params.get("plain") or ["0"])[0] in ("1", "true", "yes")

        row = self.store.get(day)
        text = None
        if row and row.get("text"):
            text = self.store.claim(day, self.cfg.once_grace_minutes) if once else row["text"]

        if plain:
            # 快捷指令最好处理纯文本：没内容时返回一个固定标记，方便「如果」判断。
            return self._send_text(200, text if text else self.cfg.nothing_sentinel)

        return self._send_json(
            200,
            {
                "date": day,
                "ready": bool(row and row.get("text")),
                "text": text if text else None,
                "target_at": (row or {}).get("target_at"),
                "generated_at": (row or {}).get("generated_at"),
                "source": (row or {}).get("source"),
                "model": (row or {}).get("model"),
                "delivered": bool((row or {}).get("delivered_at")),
                "nothing_sentinel": self.cfg.nothing_sentinel,
            },
        )


def make_handler(
    cfg: Config,
    store: Store,
    generate_now: Callable[[bool], Optional[str]],
    get_public_url: Optional[Callable[[], str]] = None,
) -> type:
    # generate_now 必须包成 staticmethod，否则作为类属性访问时会绑定 self，
    # 调用时就会多出一个参数。
    return type(
        "BoundHandler",
        (_Handler,),
        {
            "cfg": cfg,
            "store": store,
            "generate_now": staticmethod(generate_now),
            "get_public_url": staticmethod(get_public_url or (lambda: "")),
        },
    )


def start_server(
    cfg: Config,
    store: Store,
    generate_now: Callable[[bool], Optional[str]],
    get_public_url: Optional[Callable[[], str]] = None,
) -> ThreadingHTTPServer:
    handler = make_handler(cfg, store, generate_now, get_public_url)
    httpd = ThreadingHTTPServer((cfg.listen_host, cfg.listen_port), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    thread.start()
    log.info("HTTP 服务已启动：http://%s:%s/", cfg.listen_host, cfg.listen_port)
    return httpd
