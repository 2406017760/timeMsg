"""TimeMsg 主程序：常驻调度 + HTTP 接口。

用法：
    python -m app.main serve             # 常驻运行（正常用法）
    python -m app.main generate --force  # 立刻生成今天的内容（调试用）
    python -m app.main peek              # 看今天的状态
    python -m app.main test-api          # 单独测一次 AI 接口
    python -m app.main history           # 看历史记录
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import socket
import sys
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Optional

from . import __version__
from .config import ROOT, Config, ConfigError, load_config, load_system_prompt, refresh_tunables
from .context import load_context
from .generator import generate
from .server import start_server
from .store import Store
from .tunnel import TunnelManager

log = logging.getLogger("timemsg")

TICK_SECONDS = 20
TIME_FMT = "%Y-%m-%d %H:%M:%S"


# --------------------------------------------------------------------- 日志
def setup_logging(cfg: Config, verbose: bool = False) -> None:
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")

    file_handler = RotatingFileHandler(
        cfg.log_path, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # pythonw 启动时没有控制台，sys.stderr 为 None，此时不要加流处理器。
    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)


# ----------------------------------------------------------------- 网络信息
def local_ips() -> list[str]:
    ips: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:  # noqa: BLE001 - 拿不到就算了，仅用于打印提示
        pass
    return sorted(ips)


# ------------------------------------------------------------------- 调度
def ensure_today(cfg: Config, store: Store, force: bool = False) -> Optional[str]:
    """如果今天该发了还没生成，就生成一次。返回新生成的文本，否则 None。"""
    # 每天生成前重新读一次「可调参数」，这样改 config.json 不用重启服务
    refresh_tunables(cfg)

    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    row = store.ensure_plan(day, cfg.window_start, cfg.window_end)

    if row.get("text") and not force:
        return None

    target = datetime.strptime(row["target_at"], TIME_FMT)
    if not force and now < target:
        return None

    result = generate(cfg, store)
    store.save_text(
        day,
        result.text,
        result.source,
        result.model,
        result.error,
        context_files=result.context_files,
        context_chars=result.context_chars,
    )
    if result.error:
        log.warning("今日内容已生成（兜底），来源 %s：%s", result.source, result.text.replace("\n", " / "))
    else:
        log.info("今日内容已生成，来源 %s：%s", result.source, result.text.replace("\n", " / "))
    return result.text


def scheduler_loop(cfg: Config, store: Store, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            ensure_today(cfg, store)
        except Exception:  # noqa: BLE001 - 调度线程绝不能因为单次异常退出
            log.exception("调度循环出现异常，20 秒后重试")
        stop_event.wait(TICK_SECONDS)


# ------------------------------------------------------------------- 命令
def cmd_serve(cfg: Config) -> int:
    store = Store(cfg.db_path)
    stop_event = threading.Event()

    # 开了隧道就默认只监听本机：请求统一从 cloudflared 进来，
    # 免得同一个 Wi-Fi 下的人绕过 Cloudflare 直接怼这个端口。
    if cfg.tunnel.mode in ("quick", "named") and cfg.tunnel.bind_localhost and cfg.listen_host == "0.0.0.0":
        log.info("隧道已启用：HTTP 改为只监听 127.0.0.1（bind_localhost=true）")
        cfg.listen_host = "127.0.0.1"

    tunnel = TunnelManager(cfg)
    try:
        httpd = start_server(
            cfg,
            store,
            generate_now=lambda force: ensure_today(cfg, store, force),
            get_public_url=lambda: tunnel.public_url,
        )
    except OSError as exc:
        # 最常见的原因：已经有一个 TimeMsg 在后台跑着（比如计划任务 + 手动启动各一份）
        log.error("无法监听 %s:%s —— %s", cfg.listen_host, cfg.listen_port, exc)
        log.error("端口被占用。可能已经有一个实例在运行，检查：Get-Process pythonw")
        store.close()
        return 3

    day = datetime.now().strftime("%Y-%m-%d")
    row = store.ensure_plan(day, cfg.window_start, cfg.window_end)

    log.info("TimeMsg %s 已启动", __version__)
    log.info("发送窗口：%s（今天随机到 %s）", cfg.window_text, row["target_at"][11:16])
    log.info("数据库：%s", cfg.db_path)
    log.info("资料目录：%s（%s）", cfg.context_dir, "已启用" if cfg.context.enabled else "已关闭")
    if cfg.context.enabled and not cfg.context_dir.exists():
        log.warning("资料目录不存在，AI 只能写通用内容。跑 `python -m app.main context` 看详情")
    if not cfg.token:
        log.warning("配置里没有设置 token，接口对任何拿到地址的人都开放，请尽快补上")
    if not cfg.ai.api_key and not cfg.ai.mock:
        log.warning("没有配置 API key，将使用本地兜底句子")

    if tunnel.enabled:
        log.info("隧道模式：%s", cfg.tunnel.mode)
        tunnel.start()
    else:
        for ip in local_ips():
            log.info(
                "局域网访问地址：http://%s:%s/today?k=%s&once=1&plain=1",
                ip, cfg.listen_port, cfg.token or "TOKEN",
            )
    log.info("本机配置页（会显示该填进快捷指令的 URL）：http://127.0.0.1:%s/setup", cfg.listen_port)
    if tunnel.public_url:
        log.info("快捷指令 URL：%s", tunnel.shortcut_url())

    try:
        scheduler_loop(cfg, store, stop_event)
    except KeyboardInterrupt:
        log.info("收到退出信号，正在停止……")
    finally:
        stop_event.set()
        tunnel.stop()
        httpd.shutdown()
        store.close()
    return 0


def cmd_generate(cfg: Config, force: bool) -> int:
    store = Store(cfg.db_path)
    text = ensure_today(cfg, store, force=force)
    if text is None:
        row = store.get(datetime.now().strftime("%Y-%m-%d"))
        print(f"今天已有内容，未重新生成。当前目标时刻 {row['target_at'] if row else '?'}")
        print(f"当前内容：{row['text'] if row else ''}")
    else:
        print(text)
    store.close()
    return 0


def cmd_context(cfg: Config) -> int:
    """打印 AI 每天实际会读到的个人资料，用来确认资料目录有没有生效。"""
    bundle = load_context(cfg)
    print(f"资料目录 : {cfg.context_dir}")
    if not cfg.context_dir.exists():
        print("状态     : ❌ 目录不存在，请创建它并放入 .md / .txt 文件")
        return 1
    print(f"命中文件 : {len(bundle.files)} 个")
    for name in bundle.files:
        print(f"           - {name}")
    print(f"总字数   : {bundle.chars}（上限 {cfg.context.max_total_chars}，单文件上限 {cfg.context.max_file_chars}）")
    if bundle.truncated:
        print(f"⚠️ 被截断 : {'、'.join(bundle.truncated)}（保留了开头与结尾）")
    if bundle.skipped_large:
        print(f"⚠️ 已跳过 : {'、'.join(bundle.skipped_large)}（文件过大，原始导出请先提炼成小结）")
    if bundle.placeholders:
        print(f"⚠️ 占位符 : {'、'.join(bundle.placeholders)}（模板还没替换成你自己的内容）")
    print(f"模式     : {'长问候（保留段落）' if cfg.prompt.keep_newlines else '一句话'}"
          f"，目标 {cfg.prompt.min_chars}~{cfg.prompt.max_chars} 字")
    print(f"提示词   : {cfg.prompt.system_file or '（内联在 config.json 的 prompt.system 里）'}"
          f"，{len(cfg.prompt.system)} 字")
    print("\n=========== 以下是 AI 实际会看到的内容 ===========\n")
    print(bundle.text or "（空：没有可用的资料文件）")
    return 0


def cmd_prompt(cfg: Config, action: str, name: str = "") -> int:
    """管理提示词版本：列表 / 存档 / 切换。

    版本文件放在 prompts/versions/ 下，切换只改 config.json 里的 system_file。
    因为提示词是每次生成现读的，切完下一次生成就生效，不用重启。
    """
    versions_dir = ROOT / "prompts" / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    current_rel = cfg.prompt.system_file

    if action == "list":
        print(f"当前生效：{current_rel or '（config.json 里的内联 prompt.system）'}")
        print(f"版本目录：{versions_dir}")
        files = sorted(versions_dir.glob("*.txt"))
        if not files:
            print("  （还没有存档版本，用 `python -m app.main prompt save <名字>` 存档当前提示词）")
        for path in files:
            size = len(path.read_text(encoding="utf-8"))
            active = "  ← 当前生效" if path.name in current_rel else ""
            print(f"  {path.name}（{size} 字）{active}")
        return 0

    if action == "save":
        if not name:
            print("用法：python -m app.main prompt save <名字>", file=sys.stderr)
            return 2
        target = versions_dir / (name if name.endswith(".txt") else name + ".txt")
        if target.exists():
            print(f"已存在同名版本：{target.name}（换个名字，或先删掉它）", file=sys.stderr)
            return 1
        # 逐字节复制，不要经过任何读写规范化：
        # read_text().strip() 会吃掉首尾空行，write_text 又会在 Windows 上把
        # \n 变成 \r\n，那样"备份"就和原件不一样了。
        source = (ROOT / cfg.prompt.system_file) if cfg.prompt.system_file else None
        if source and source.exists():
            shutil.copyfile(source, target)
        else:
            target.write_text(load_system_prompt(cfg) + "\n", encoding="utf-8", newline="\n")
        print(f"已把当前提示词存档到：prompts/versions/{target.name}")
        return 0

    if action == "use":
        if not name:
            print("用法：python -m app.main prompt use <版本文件名>", file=sys.stderr)
            return 2
        target = versions_dir / (name if name.endswith(".txt") else name + ".txt")
        if not target.exists():
            print(f"找不到版本：{name}。用 `prompt list` 看有哪些。", file=sys.stderr)
            return 1
        try:
            raw = json.loads(cfg.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"config.json 读不出来，无法切换：{exc}", file=sys.stderr)
            return 1
        raw.setdefault("prompt", {})["system_file"] = f"prompts/versions/{target.name}"
        cfg.path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        print(f"已切换到：prompts/versions/{target.name}")
        print("提示词是每次生成现读的，下一次生成就生效，不用重启。")
        return 0

    print(f"未知操作：{action}", file=sys.stderr)
    return 2


def cmd_peek(cfg: Config) -> int:
    store = Store(cfg.db_path)
    day = datetime.now().strftime("%Y-%m-%d")
    row = store.ensure_plan(day, cfg.window_start, cfg.window_end)
    print(f"日期      : {day}")
    print(f"随机时刻  : {row['target_at']}")
    print(f"是否已生成: {'是' if row.get('text') else '否'}")
    print(f"内容      : {row.get('text') or '（还没生成）'}")
    print(f"已取走    : {row.get('delivered_at') or '否'}")
    print(f"来源/模型 : {row.get('source') or '-'} / {row.get('model') or '-'}")
    if row.get("last_error"):
        print(f"最近错误  : {row['last_error']}")
    print(f"统计      : {store.stats()}")
    store.close()
    return 0


def cmd_test_api(cfg: Config) -> int:
    store = Store(cfg.db_path)
    result = generate(cfg, store)
    print(f"来源  : {result.source}")
    print(f"模型  : {result.model}")
    if result.error:
        print(f"错误  : {result.error}")
    print(f"内容  : {result.text}")
    store.close()
    return 0 if result.source == "api" else 1


def cmd_history(cfg: Config, limit: int) -> int:
    store = Store(cfg.db_path)
    items = store.history(limit)
    if not items:
        print("还没有任何记录。")
    for item in items:
        flag = "✓" if item.get("delivered_at") else " "
        print(f"[{flag}] {item['day']} 目标 {str(item.get('target_at'))[11:16]} | {item.get('text') or '（未生成）'}")
    store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="timemsg", description="每天早上随机时刻生成一句话，供 iPhone 拉取发送")
    parser.add_argument("--config", help="配置文件路径，默认 ./config.json")
    parser.add_argument("--mock", action="store_true", help="强制使用本地兜底句子，不调用 AI 接口")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    parser.add_argument(
        "--tunnel",
        choices=["off", "quick", "named"],
        help="临时覆盖配置里的隧道模式：quick=免账号随机域名，named=固定域名",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="常驻运行（默认）")
    gen = sub.add_parser("generate", help="立刻生成今天的内容")
    gen.add_argument("--force", action="store_true", help="即使已生成也重新生成")
    sub.add_parser("peek", help="查看今天的状态")
    sub.add_parser("context", help="查看 AI 会读到的个人资料（确认资料目录生效）")
    pr = sub.add_parser("prompt", help="管理提示词版本：list / save / use")
    pr.add_argument("action", choices=["list", "save", "use"])
    pr.add_argument("name", nargs="?", default="", help="版本文件名（save/use 时用）")
    sub.add_parser("test-api", help="测试 AI 接口连通性")
    hist = sub.add_parser("history", help="查看历史")
    hist.add_argument("--limit", type=int, default=30)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    if args.mock:
        cfg.ai.mock = True
    if args.tunnel:
        cfg.tunnel.mode = args.tunnel

    setup_logging(cfg, verbose=args.verbose)

    command = args.command or "serve"
    if command == "serve":
        return cmd_serve(cfg)
    if command == "generate":
        return cmd_generate(cfg, force=args.force)
    if command == "peek":
        return cmd_peek(cfg)
    if command == "context":
        return cmd_context(cfg)
    if command == "prompt":
        return cmd_prompt(cfg, args.action, args.name)
    if command == "test-api":
        return cmd_test_api(cfg)
    if command == "history":
        return cmd_history(cfg, args.limit)
    print(f"未知命令：{command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
