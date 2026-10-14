"""从 config.example.json 生成 config.json，并自动填入一串随机 token。

用法：  python tools\\init_config.py
已存在 config.json 时不会覆盖（要重建请加 --force）。
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.example.json"
TARGET = ROOT / "config.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 TimeMsg 配置文件")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 config.json")
    parser.add_argument("--api-key", default="", help="顺便写入 API key")
    args = parser.parse_args()

    if TARGET.exists() and not args.force:
        print(f"{TARGET.name} 已存在，未覆盖（要重建请加 --force）")
        return 0

    if not EXAMPLE.exists():
        print(f"找不到 {EXAMPLE}", file=sys.stderr)
        return 2

    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["token"] = secrets.token_urlsafe(18)
    if args.api_key:
        data.setdefault("ai", {})["api_key"] = args.api_key
    TARGET.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"已生成 {TARGET}")
    print(f"  token   = {data['token']}")
    print(f"  窗口    = {data['window_start']} ~ {data['window_end']}")
    print(f"  端口    = {data['listen_port']}")
    if not data.get("ai", {}).get("api_key"):
        print("  提醒    : ai.api_key 还是空的，需要填上你的 key（或先加 --api-key 重跑）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
