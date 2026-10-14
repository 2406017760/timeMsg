"""自检脚本：不联网、不需要 API key、不需要硬件。

运行：  python tests/smoke_test.py
全部通过会打印 "全部通过"，退出码 0。
"""

from __future__ import annotations

import json
import re
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import server as server_mod  # noqa: E402
from app.config import (  # noqa: E402
    AIConfig,
    Config,
    ConfigError,
    PromptConfig,
    load_config,
    load_system_prompt,
    refresh_tunables,
)
from app.context import load_context, strip_markdown  # noqa: E402
from app.generator import FALLBACK_GREETINGS, FALLBACK_LINES, _clean, generate  # noqa: E402
from app.main import ensure_today  # noqa: E402
from app.store import Store  # noqa: E402

PASSED = 0


def ok(label: str) -> None:
    global PASSED
    PASSED += 1
    print(f"  [通过] {label}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_config(
    tmp: Path, port: int, grace: int = 15, token: str = "TESTTOKEN", tag: str = "test"
) -> Config:
    """每个用例用独立的数据库文件，避免互相干扰。"""
    cfg = Config(path=tmp / "config.json")
    cfg.listen_host = "127.0.0.1"
    cfg.listen_port = port
    cfg.token = token
    cfg.window_start = dtime(0, 0)
    cfg.window_end = dtime(23, 59)
    cfg.once_grace_minutes = grace
    cfg.db_path = tmp / f"{tag}.db"
    cfg.log_path = tmp / f"{tag}.log"
    cfg.context_dir = tmp / f"{tag}-profile"
    cfg.ai = AIConfig(mock=True)
    cfg.prompt = PromptConfig(
        system="", min_chars=6, max_chars=40, themes=["时间"], styles=["短句"]
    )
    return cfg


def test_clean() -> None:
    print("清洗模型输出：")
    assert _clean("  “把鞋带系好。”  ") == "把鞋带系好。"
    assert _clean("一句话：慢一点没关系。") == "慢一点没关系。"
    assert _clean("第一行\n第二行") == "第一行"
    assert _clean("") == ""
    ok("去引号 / 去前缀 / 只取首行")


def test_example_config() -> None:
    print("示例配置可被正确解析：")
    cfg = load_config(ROOT / "config.example.json")
    assert cfg.window_start == dtime(6, 30), cfg.window_start
    assert cfg.window_end == dtime(8, 30), cfg.window_end
    assert cfg.listen_port == 8765
    # 主题/风格只要够随机抽取就行，别绑死数量——用户随时会精简这两个列表
    assert len(cfg.prompt.themes) >= 3, cfg.prompt.themes
    assert len(cfg.prompt.styles) >= 3, cfg.prompt.styles
    # 提示词里只要有字数占位符就行，别绑死具体写法（{max_chars} / {max_chars+100} 都算）
    assert re.search(r"\{(?:min_chars|max_chars)[^}]*\}", cfg.prompt.system), cfg.prompt.system[:80]
    ok("config.example.json 字段齐全且时间正确")


def test_store(tmp: Path) -> None:
    print("数据库与去重逻辑：")
    store = Store(tmp / "store.db")
    day = datetime.now().strftime("%Y-%m-%d")
    row = store.ensure_plan(day, dtime(6, 30), dtime(8, 30))
    target = datetime.strptime(row["target_at"], "%Y-%m-%d %H:%M:%S")
    assert target.date().isoformat() == day
    assert dtime(6, 30) <= target.time() <= dtime(8, 30), target
    again = store.ensure_plan(day, dtime(6, 30), dtime(8, 30))
    assert again["target_at"] == row["target_at"], "重启后随机时刻不应改变"
    ok(f"随机目标时刻落在窗口内且持久稳定（{target.strftime('%H:%M')}）")

    store.save_text(day, "测试内容", "api", "test-model")
    assert store.claim(day, 15) == "测试内容"
    assert store.claim(day, 15) == "测试内容", "宽限期内应允许重试"
    assert store.claim(day, 0) is None, "宽限期外不应再次返回"
    ok("once 取走 + 宽限期重试 + 超期不再返回")

    store.save_text(day, "第二版", "api", "test-model")
    assert store.recent_texts(10)[0] == "第二版"
    assert store.stats()["generated"] == 1
    ok("历史查询 / 统计正常")

    # 重新生成后必须能再次被取走，否则 --force 换掉的内容手机永远收不到
    assert store.claim(day, 15) == "第二版"
    store.save_text(day, "第三版", "api", "test-model")
    assert store.get(day)["delivered_at"] is None, "重新生成后应清空取走标记"
    assert store.claim(day, 15) == "第三版"
    ok("重新生成会重置取走状态（--force 后手机仍能收到）")
    store.close()


def test_tunnel(tmp: Path) -> None:
    print("Cloudflare Tunnel 配置与地址解析：")
    from app.tunnel import QUICK_URL_RE, find_cloudflared

    assert QUICK_URL_RE.search("https://example.com") is None
    real_log = "\n".join(
        [
            "2026-10-14T06:40:00Z INF Thank you for trying Cloudflare Tunnel.",
            "2026-10-14T06:40:01Z INF Requesting new quick Tunnel on trycloudflare.com...",
            "2026-10-14T06:40:03Z INF |  https://silly-otter-42.trycloudflare.com  |",
        ]
    )
    match = QUICK_URL_RE.search(real_log)
    assert match and match.group(0) == "https://silly-otter-42.trycloudflare.com"
    ok("能从 cloudflared 日志里抓到公网地址")

    cfg = load_config(ROOT / "config.example.json")
    assert cfg.tunnel.mode == "off", cfg.tunnel.mode
    assert cfg.tunnel.bind_localhost is True
    ok("示例配置的 tunnel 段解析正确")

    bad = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    bad["tunnel"]["mode"] = "banana"
    bad_path = tmp / "bad-tunnel.json"
    bad_path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    try:
        load_config(bad_path)
        raise AssertionError("非法 tunnel.mode 应该报配置错误")
    except ConfigError:
        pass
    ok("非法的 tunnel.mode 会被拒绝")

    # 找到时返回路径；找不到时返回 None，而不是抛异常
    found = find_cloudflared("C:/definitely/not/here.exe")
    assert found is None or isinstance(found, str)
    ok("cloudflared 查找逻辑不会抛异常")


def test_tunnel_args() -> None:
    print("隧道启动命令拼装：")
    from app.tunnel import TunnelManager

    cfg = load_config(ROOT / "config.example.json")

    cfg.tunnel.mode = "quick"
    args = TunnelManager(cfg)._args()
    assert args[1] == "tunnel" and "--url" in args and args[-1].startswith("http://127.0.0.1")
    ok("quick 模式：--url 指向本机")

    cfg.tunnel.mode = "named"
    cfg.tunnel.named_tunnel = "timemsg"
    cfg.tunnel.token = ""
    args = TunnelManager(cfg)._args()
    assert args[1:3] == ["tunnel", "--no-autoupdate"]
    assert args[-2:] == ["--url", f"http://127.0.0.1:{cfg.listen_port}"] or args[-1] == "timemsg"
    assert "timemsg" in args and "--url" in args
    ok("named 本地托管：带隧道名 + --url")

    cfg.tunnel.token = "FAKE-TUNNEL-TOKEN-FOR-TEST"
    args = TunnelManager(cfg)._args()
    assert "--token" in args and args[-1] == "FAKE-TUNNEL-TOKEN-FOR-TEST"
    assert "timemsg" not in args, "token 模式下不需要隧道名"
    ok("named 控制台托管：只带 token，不依赖 cert.pem")


def test_clean_multiline() -> None:
    print("长问候与一句话的清洗差异：")
    raw = "问候：\n**早安**\n\n第一段。\n\n\n第二段。\n"
    out = _clean(raw, keep_newlines=True)
    assert out.startswith("早安"), repr(out)
    assert "**" not in out and "##" not in out, repr(out)
    assert "第一段。\n\n第二段。" in out, repr(out)
    ok("保留段落 / 去加粗 / 去首行标签 / 折叠多余空行")

    assert _clean("```\n你好。\n```", keep_newlines=True) == "你好。"
    ok("去掉代码围栏")

    assert _clean("## 标题\n正文。", keep_newlines=True) == "标题\n正文。"
    ok("去掉 markdown 标题符号")

    # 一句话模式的行为不能被改坏
    assert _clean("第一行\n第二行", keep_newlines=False) == "第一行"
    assert _clean("  “把鞋带系好。”  ", keep_newlines=False) == "把鞋带系好。"
    assert _clean("一句话：慢一点没关系。", keep_newlines=False) == "慢一点没关系。"
    ok("一句话模式仍然只取首行、去引号去前缀")


def test_context_skips_large(tmp: Path) -> None:
    print("超大资料文件保护：")
    import app.context as ctx_mod

    cfg = make_config(tmp, free_port(), tag="big")
    root = cfg.context_dir
    root.mkdir(parents=True)
    (root / "正常.md").write_text("正常内容", encoding="utf-8")
    (root / "巨大.md").write_text("x" * 5000, encoding="utf-8")

    original = ctx_mod.MAX_READ_BYTES
    ctx_mod.MAX_READ_BYTES = 1000  # 临时压低阈值，免得测试里真写 5MB
    try:
        bundle = load_context(cfg)
    finally:
        ctx_mod.MAX_READ_BYTES = original

    assert bundle.files == ["正常.md"], bundle.files
    assert bundle.skipped_large == ["巨大.md"], bundle.skipped_large
    assert "x" * 100 not in bundle.text
    ok("超过大小上限的资料文件被跳过，不会把原始导出喂给模型")


def test_health_summary(tmp: Path) -> None:
    print("Apple 健康提炼：")
    from datetime import date as ddate

    # 直接把 tools/ 加进搜索路径再常规导入。
    # 不用 importlib.util.module_from_spec——那样 @dataclass 会因为模块没注册进
    # sys.modules 而报 AttributeError。
    tools_dir = str(ROOT / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import health_summary as mod

    # 单位换算（最容易出错的地方）
    assert abs(mod._km(1, "mi") - 1.609344) < 1e-6
    assert abs(mod._km(1609.344, "m") - 1.609344) < 1e-6
    assert abs(mod._km(5, "km") - 5) < 1e-9
    assert abs(mod._kg(150, "lb") - 68.0389) < 0.001
    assert abs(mod._minutes(90, "min") - 90) < 1e-9
    assert abs(mod._minutes(3600, "sec") - 60) < 1e-9
    ok("距离 / 重量 / 时长的单位换算正确（mi、m、lb、sec 都会换算）")

    xml = tmp / "导出.xml"
    xml.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="zh_CN">
 <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="1000"
         startDate="2026-10-01 08:00:00 +0800" endDate="2026-10-01 09:00:00 +0800"/>
 <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="2000"
         startDate="2026-10-01 18:00:00 +0800" endDate="2026-10-01 19:00:00 +0800"/>
 <Record type="HKQuantityTypeIdentifierDistanceWalkingRunning" unit="mi" value="1"
         startDate="2026-10-01 18:00:00 +0800" endDate="2026-10-01 19:00:00 +0800"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisAsleepCore"
         startDate="2026-10-01 23:00:00 +0800" endDate="2026-10-02 07:00:00 +0800"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisInBed"
         startDate="2026-10-01 22:30:00 +0800" endDate="2026-10-01 23:00:00 +0800"/>
 <Record type="HKQuantityTypeIdentifierBodyMass" unit="kg" value="64.5"
         startDate="2026-10-01 07:30:00 +0800" endDate="2026-10-01 07:30:00 +0800"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min"
          totalDistance="5" totalDistanceUnit="km" totalEnergyBurned="300" totalEnergyBurnedUnit="kcal"
          startDate="2026-10-01 18:00:00 +0800" endDate="2026-10-01 18:30:00 +0800"/>
 <ActivitySummary dateComponents="2026-10-01" activeEnergyBurned="500" appleExerciseTime="30" appleStandHours="12"/>
</HealthData>""",
        encoding="utf-8",
    )

    data = mod.parse_export(xml)
    assert data.steps[ddate(2026, 10, 1)] == 3000, data.steps
    assert abs(data.distance[ddate(2026, 10, 1)] - 1.609344) < 1e-6, data.distance
    # 睡眠只统计"睡着"的阶段，IInBed 那半小时不算
    assert abs(data.sleep[ddate(2026, 10, 2)] - 8 * 3600) < 1, data.sleep
    assert len(data.workouts) == 1 and data.workouts[0]["km"] == 5.0
    assert data.workouts[0]["kind"] == "跑步"
    assert data.body_mass == [(ddate(2026, 10, 1), 64.5)]
    ok("逐条解析正确：步数累加、睡眠只算睡着阶段、运动与体重入库")

    text = mod.build_summary(data, 30, xml)
    assert "步数" in text and "睡眠" in text and "锻炼" in text
    # 今天的半天数据不能算进"低活动日"，统计口径必须排除今天
    today = ddate.today()
    assert f"统计范围：{today - timedelta(days=30)} ~ {today - timedelta(days=1)}" in text, text[:300]
    ok("统计口径排除今天，避免把半天数据当成低活动日")


def test_prompt_versions(tmp: Path) -> None:
    print("提示词版本管理（list / save / use）：")
    from app.main import cmd_prompt

    versions = ROOT / "prompts" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    tmp_version = versions / "_smoke_tmp.txt"
    tmp_copy = versions / "_smoke_copy.txt"
    conf = tmp / "version-conf.json"

    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    conf.write_text(json.dumps(example, ensure_ascii=False), encoding="utf-8")

    try:
        tmp_version.write_text("测试提示词内容，含中文与换行\n第二行\n", encoding="utf-8")
        cfg = load_config(conf)

        assert cmd_prompt(cfg, "list") == 0
        ok("prompt list 可运行")

        # use：只改 config 里的 system_file，并且写回的是合法 JSON
        assert cmd_prompt(cfg, "use", "_smoke_tmp") == 0
        updated = json.loads(conf.read_text(encoding="utf-8"))
        assert updated["prompt"]["system_file"] == "prompts/versions/_smoke_tmp.txt", updated["prompt"]
        assert updated["token"] == example["token"], "切换版本不能破坏其它配置"
        ok("prompt use 切换生效，且其它配置字段保持不变")

        # 切换后再读，内容应当就是那个版本
        assert load_system_prompt(load_config(conf)).startswith("测试提示词内容")

        # save：必须是逐字节复制，不能被换行规范化或 strip 掉首尾
        cfg2 = load_config(conf)
        assert cmd_prompt(cfg2, "save", "_smoke_copy") == 0
        assert tmp_copy.read_bytes() == tmp_version.read_bytes(), "备份必须逐字节一致"
        ok("prompt save 逐字节备份（不受 Windows 换行转换影响）")

        # 同名再存一次应当被拒绝，而不是静默覆盖
        assert cmd_prompt(cfg2, "save", "_smoke_copy") == 1
        ok("同名版本不会被静默覆盖")

        # 找不到的版本要报错
        assert cmd_prompt(cfg2, "use", "_not_exist") == 1
        ok("切换不存在的版本会报错")
    finally:
        for path in (tmp_version, tmp_copy):
            if path.exists():
                path.unlink()


def test_refresh_tunables(tmp: Path) -> None:
    print("配置热加载（改了不用重启）：")
    src = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    src["prompt"]["daily_reminder_rate"] = 0.35
    path = tmp / "hot.json"
    path.write_text(json.dumps(src, ensure_ascii=False), encoding="utf-8")

    cfg = load_config(path)
    assert cfg.prompt.daily_reminder_rate == 0.35

    src["prompt"]["daily_reminder_rate"] = 0.8
    src["ai"]["model"] = "deepseek-reasoner"
    path.write_text(json.dumps(src, ensure_ascii=False), encoding="utf-8")
    refresh_tunables(cfg)
    assert cfg.prompt.daily_reminder_rate == 0.8, cfg.prompt.daily_reminder_rate
    assert cfg.ai.model == "deepseek-reasoner"
    ok("改 config.json 后无需重启即可生效（prompt / ai 都会被热加载）")

    # 文件写坏时必须保留旧值，不能把每天早上的生成弄崩
    path.write_text("{ 这不是合法 JSON", encoding="utf-8")
    refresh_tunables(cfg)
    assert cfg.prompt.daily_reminder_rate == 0.8, "损坏时应当保留旧设置"
    assert cfg.ai.model == "deepseek-reasoner"
    ok("配置文件损坏时沿用旧设置，服务不受影响")


def test_reminder_probability(tmp: Path) -> None:
    print("日常提醒的概率控制：")
    import random as _random

    from app.generator import build_messages

    cfg = make_config(tmp, free_port(), tag="reminder")
    cfg.prompt.keep_newlines = True
    cfg.prompt.max_chars = 280
    cfg.prompt.daily_reminder_rate = 0.35
    store = Store(cfg.db_path)
    try:
        rng = _random.Random(20261014)
        hits = 0
        runs = 2000
        for _ in range(runs):
            msgs = build_messages(cfg, [], rng, "")
            user = msgs[-1]["content"]
            has_permission = "今天的额外许可" in user
            has_ban = "今天的额外限制" in user
            assert has_permission != has_ban, "每天必须明确二选一"
            hits += 1 if has_permission else 0
        rate = hits / runs
        assert 0.31 < rate < 0.39, f"命中率 {rate:.3f} 偏离 0.35 太多"
        ok(f"2000 次采样命中率 {rate:.1%}（配置 35%），且每次都有明确指令")
    finally:
        store.close()

    # 关掉概率时也必须始终给禁令，不能两边都不说
    cfg.prompt.daily_reminder_rate = 0.0
    rng = _random.Random(1)
    store = Store(cfg.db_path)
    try:
        for _ in range(50):
            user = build_messages(cfg, [], rng, "")[-1]["content"]
            assert "今天的额外限制" in user, user
        cfg.prompt.daily_reminder_rate = 1.0
        for _ in range(50):
            user = build_messages(cfg, [], rng, "")[-1]["content"]
            assert "今天的额外许可" in user, user
        ok("设为 0 时永远禁止，设为 1 时永远允许（边界正确）")
    finally:
        store.close()


def test_length_placeholders() -> None:
    print("字数占位符（含偏移写法）：")
    from app.generator import _fill_length_placeholders

    out = _fill_length_placeholders(
        "不少于 {min_chars} 字，不超过 {max_chars} 字", 170, 280
    )
    assert out == "不少于 170 字，不超过 280 字", out
    ok("基础占位符 {min_chars} / {max_chars}")

    out = _fill_length_placeholders(
        "不少于 {min_chars-50} 字，不超过 {max_chars+100} 字", 170, 280
    )
    assert out == "不少于 120 字，不超过 380 字", out
    ok("偏移写法 {min_chars-50} / {max_chars+100}（否则会原样送给模型）")

    # 不能把别的花括号内容当占位符吃掉
    out = _fill_length_placeholders("约 {min_chars} 字，还有 {其他} 和 {max_chars-10}", 200, 300)
    assert out == "约 200 字，还有 {其他} 和 290", out
    ok("只替换认识的占位符，其它花括号原样保留")


def test_prompt_file(tmp: Path) -> None:
    print("提示词文件：")
    cfg = load_config(ROOT / "config.example.json")
    assert cfg.prompt.system_file == "prompts/greeting.txt", cfg.prompt.system_file

    # 不要去断言提示词里有哪些句子——那文件是用户的地盘，随时会被重写。
    # 只断言结构和机制：读得到、够长、占位符能填干净。
    prompt = load_system_prompt(cfg)
    assert len(prompt) > 200, f"提示词太短，可能没读到：{len(prompt)}"
    from app.generator import _fill_length_placeholders

    filled = _fill_length_placeholders(prompt, cfg.prompt.min_chars, cfg.prompt.max_chars)
    import re as _re

    leftovers = _re.findall(r"\{(?:min_chars|max_chars)[^}]*\}", filled)
    assert not leftovers, f"有没被替换的字数占位符：{leftovers}"
    ok(f"提示词读入正常（{len(prompt)} 字），字数占位符全部替换成功")

    bad = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    bad["prompt"]["system_file"] = "prompts/does-not-exist.txt"
    bad_path = tmp / "bad-prompt.json"
    bad_path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    try:
        load_config(bad_path)
        raise AssertionError("system_file 不存在时应当报配置错误")
    except ConfigError:
        pass
    ok("prompt.system_file 指向不存在的文件会被明确拒绝")

    # 提示词文件应当在每次生成时现读（改完立刻生效，不用重启）
    cfg2 = load_config(ROOT / "config.example.json")
    original = load_system_prompt(cfg2)
    tmp_prompt = ROOT / "prompts" / "_smoke_tmp.txt"
    tmp_prompt.write_text("临时提示词内容", encoding="utf-8")
    try:
        cfg2.prompt.system_file = "prompts/_smoke_tmp.txt"
        assert load_system_prompt(cfg2) == "临时提示词内容"
        tmp_prompt.unlink()
        assert load_system_prompt(cfg2) == original, "文件消失后应回退到已加载的版本"
    finally:
        if tmp_prompt.exists():
            tmp_prompt.unlink()
    ok("提示词文件每次现读，文件缺失时回退而不是崩溃")


def test_strip_markdown() -> None:
    print("资料纯文本化：")
    raw = (
        "# 标题\n\n"
        "- 项目一\n"
        "* 项目二\n"
        "1. 项目三\n\n"
        "**加粗**和*斜体*还有`代码`\n\n"
        "> 引用一行\n\n"
        "链接 [文字](https://example.com)\n\n"
        "00\\. 转义编号\n\n"
        "// 这是只给我自己看的备注\n"
        "<!-- 注释也不该出现 -->\n\n\n\n"
        "---\n"
        "结尾。\n"
    )
    out = strip_markdown(raw)
    assert "标题" in out and "#" not in out, out
    assert "- 项目一" not in out and "项目一" in out
    assert "* 项目二" not in out and "项目二" in out
    assert "1. 项目三" not in out and "项目三" in out
    assert "加粗和斜体还有代码" in out, out
    assert "> " not in out and "引用一行" in out
    assert "文字" in out and "https://example.com" not in out
    assert "00. 转义编号" in out, out
    assert "只给我自己看的备注" not in out
    assert "注释也不该出现" not in out
    assert "---" not in out
    assert "\n\n\n" not in out
    ok("去标题/列表/强调/引用/链接、反转义、去注释与 // 备注、折叠空行")

    # 折叠空行能省下大量 token（这类档案文件常常一半是空行）
    padded = "A" + "\n" * 40 + "B"
    assert len(strip_markdown(padded)) < 20
    ok("连续空行被折叠成一个")


def test_context(tmp: Path) -> None:
    print("个人资料目录：")
    cfg = make_config(tmp, free_port(), tag="ctx")

    bundle = load_context(cfg)
    assert bundle.empty and bundle.files == [], bundle
    ok("目录不存在时不报错，返回空资料")

    root = cfg.context_dir
    root.mkdir(parents=True)
    (root / "关于我.md").write_text("# 关于我\n喜欢跑步。", encoding="utf-8")
    (root / "每日作息.md").write_text("# 作息\n07:30 起床。", encoding="utf-8")
    (root / "睡眠记录.md").write_text("# 睡眠\n［示例］2026-10-14 | 01:20", encoding="utf-8")
    (root / "忽略我.json").write_text("{}", encoding="utf-8")
    (root / ".隐藏.md").write_text("不该被读到", encoding="utf-8")
    sub = root / "子目录"
    sub.mkdir()
    (sub / "备注.txt").write_text("子目录里的内容", encoding="utf-8")

    bundle = load_context(cfg)
    assert set(bundle.files) == {
        "关于我.md",
        "每日作息.md",
        "睡眠记录.md",
        "子目录/备注.txt",
    }, bundle.files
    assert "【文件：关于我.md】" in bundle.text
    assert "喜欢跑步。" in bundle.text and "子目录里的内容" in bundle.text
    assert "不该被读到" not in bundle.text and "忽略我" not in bundle.text
    assert bundle.placeholders == ["睡眠记录.md"], bundle.placeholders
    assert bundle.chars == len(bundle.text) and bundle.raw_chars > 0
    ok("只读 .md/.txt、含子目录、跳过隐藏与非目标后缀、识别模板占位符")

    # 单文件超长 → 只留末尾并标注
    cfg2 = make_config(tmp, free_port(), tag="ctx-long")
    cfg2.context.max_file_chars = 1000
    cfg2.context_dir.mkdir(parents=True)
    (cfg2.context_dir / "长记录.md").write_text("前" * 50 + "尾" * 4950, encoding="utf-8")
    b2 = load_context(cfg2)
    assert b2.truncated == ["长记录.md"], b2.truncated
    assert "省略" in b2.text, "应有省略标记"
    assert "前" * 50 in b2.text, "开头必须保留（身份与核心情况在这里）"
    assert b2.text.rstrip().endswith("尾"), "结尾必须保留（最新状态在这里）"
    ok("单文件超长时保留开头与结尾，而不是只留一端")

    # 总量超限 → 后面的文件被丢弃
    cfg3 = make_config(tmp, free_port(), tag="ctx-total")
    cfg3.context.max_total_chars = 500
    cfg3.context.max_file_chars = 400
    cfg3.context_dir.mkdir(parents=True)
    (cfg3.context_dir / "a.md").write_text("A" * 400, encoding="utf-8")
    (cfg3.context_dir / "b.md").write_text("B" * 400, encoding="utf-8")
    b3 = load_context(cfg3)
    assert b3.files == ["a.md"], b3.files
    assert b3.chars <= 600, b3.chars
    ok("总量超上限时停止加载后续文件，避免提示词失控")


def test_store_migration(tmp: Path) -> None:
    print("老数据库自动迁移：")
    import sqlite3

    path = tmp / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE daily (
            day TEXT PRIMARY KEY, target_at TEXT, text TEXT, source TEXT, model TEXT,
            generated_at TEXT, delivered_at TEXT,
            delivery_count INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO daily (day, target_at, text) VALUES ('2026-01-01','2026-01-01 07:00:00','旧内容')"
    )
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        row = store.get("2026-01-01")
        assert row is not None and row["text"] == "旧内容", row
        assert "context_files" in row.keys() and "context_chars" in row.keys(), row.keys()
        store.save_text("2026-01-01", "新内容", "api", "m", None, ["关于我.md"], 42)
        row = store.get("2026-01-01")
        assert row["context_files"] == "关于我.md" and row["context_chars"] == 42, row
    finally:
        store.close()
    ok("缺列的老库自动补列，历史数据不丢")


def test_long_form_fallback(tmp: Path) -> None:
    print("长问候模式：")
    cfg = make_config(tmp, free_port(), tag="greet")
    cfg.prompt.keep_newlines = True
    cfg.prompt.min_chars = 200
    cfg.prompt.max_chars = 320
    store = Store(cfg.db_path)
    try:
        result = generate(cfg, store)  # mock 模式 → 走本地兜底
        assert result.text in FALLBACK_GREETINGS, result.text[:40]
        assert "\n" in result.text, "长问候兜底应当是多段落的"
        assert len(result.text) > 100
        assert result.source == "fallback"
        ok(f"长问候走长兜底（{len(result.text)} 字，含段落）")
    finally:
        store.close()


def test_ensure_today(tmp: Path) -> None:
    print("当日生成流程：")
    cfg = make_config(tmp, free_port(), tag="ensure")
    # 把窗口设成今天最早的一分钟，确保「已到点」条件一定成立。
    cfg.window_start = dtime(0, 0)
    cfg.window_end = dtime(0, 1)
    store = Store(cfg.db_path)
    try:
        first = ensure_today(cfg, store)
        assert first, "已过目标时刻时应立即生成（含开机补生成）"
        assert first in FALLBACK_LINES or len(first) > 0
        assert ensure_today(cfg, store) is None, "同一天不应重复生成"
        forced = ensure_today(cfg, store, force=True)
        assert forced, "force=True 应重新生成"
    finally:
        store.close()
    ok("到点生成 / 当日不重复 / force 重新生成")

    # 目标时刻还没到（窗口落在今天最后一分钟）时不应提前生成。
    cfg2 = make_config(tmp, free_port(), tag="ensure_future")
    cfg2.window_start = dtime(23, 58)
    cfg2.window_end = dtime(23, 59)
    store2 = Store(cfg2.db_path)
    try:
        row = store2.ensure_plan(
            datetime.now().strftime("%Y-%m-%d"), cfg2.window_start, cfg2.window_end
        )
        target = datetime.strptime(row["target_at"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() < target:
            assert ensure_today(cfg2, store2) is None, "未到目标时刻不应提前生成"
            ok("未到随机时刻不提前生成")
        else:
            ok("当前时间已过 23:58，跳过「不提前生成」用例")
    finally:
        store2.close()


def test_server(tmp: Path) -> None:
    print("HTTP 接口：")
    port = free_port()
    cfg = make_config(tmp, port, tag="server")
    # 窗口设在今天最早的一分钟，避免用例受「随机时刻还没到」影响而随机失败
    cfg.window_start = dtime(0, 0)
    cfg.window_end = dtime(0, 1)
    store = Store(cfg.db_path)
    httpd = server_mod.start_server(cfg, store, generate_now=lambda force: ensure_today(cfg, store, force))
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        assert health["ok"] is True
        # 公网可见的 /health 不能泄露作息与用量
        assert "target_at" not in health and "stats" not in health, health
        ok("/health 未鉴权时只给最小信息")

        with urllib.request.urlopen(f"{base}/health?k=TESTTOKEN", timeout=5) as resp:
            full = json.loads(resp.read().decode("utf-8"))
        assert "target_at" in full and "stats" in full and "shortcut_url" in full
        ok("/health 带 token 时给出完整状态与快捷指令 URL")

        # /setup 只允许「本机浏览器」打开；带 Cloudflare 头部的请求必须被拒
        with urllib.request.urlopen(f"{base}/setup", timeout=5) as resp:
            setup = resp.read().decode("utf-8")
        assert "TESTTOKEN" in setup and "/today?" in setup
        ok("/setup 本机可读，包含待复制的 URL")

        tunneled = urllib.request.Request(f"{base}/setup", headers={"CF-Connecting-IP": "1.2.3.4"})
        try:
            urllib.request.urlopen(tunneled, timeout=5)
            raise AssertionError("经隧道的 /setup 应被拒绝")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403, exc.code
        ok("/setup 拒绝来自隧道的请求（避免 token 泄到公网）")

        # 未带 token
        try:
            urllib.request.urlopen(f"{base}/today?plain=1", timeout=5)
            raise AssertionError("缺少 token 时应返回 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, exc.code
        ok("未带 token 被拒绝（401）")

        with urllib.request.urlopen(f"{base}/today?k=TESTTOKEN&plain=1", timeout=5) as resp:
            body = resp.read().decode("utf-8")
        assert body == cfg.nothing_sentinel, body
        ok("未生成时返回哨兵文本而不是报错")

        text = ensure_today(cfg, store, force=True)
        assert text, "force 生成不应返回空"
        with urllib.request.urlopen(
            f"{base}/today?k=TESTTOKEN&once=1&plain=1", timeout=5
        ) as resp:
            body = resp.read().decode("utf-8")
        assert body == text, (body, text)
        with urllib.request.urlopen(
            f"{base}/today?k=TESTTOKEN&once=1&plain=1", timeout=5
        ) as resp:
            again = resp.read().decode("utf-8")
        assert again == text, "宽限期内重试应拿到同一句话"
        ok("once=1 取走并可重试，返回纯文本")

        with urllib.request.urlopen(f"{base}/today?k=TESTTOKEN", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["ready"] is True and payload["text"] == text
        assert payload["delivered"] is True
        ok("/today JSON 模式字段完整")

        req = urllib.request.Request(f"{base}/generate?k=TESTTOKEN&force=1", method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            generated = json.loads(resp.read().decode("utf-8"))
        assert generated["ok"] is True and generated["text"]
        ok("POST /generate 可强制重生成（调试用）")

        # /pull 是 /today?once=1&plain=1 的短别名（给手机快捷指令用，URL 更短）
        with urllib.request.urlopen(f"{base}/pull?k=TESTTOKEN", timeout=5) as resp:
            pulled = resp.read().decode("utf-8")
        assert pulled == generated["text"], (pulled, generated["text"])
        with urllib.request.urlopen(f"{base}/pull?k=TESTTOKEN", timeout=5) as resp:
            again_pull = resp.read().decode("utf-8")
        assert again_pull == pulled, "宽限期内重试应返回同一句"
        try:
            urllib.request.urlopen(f"{base}/pull?k=WRONG", timeout=5)
            raise AssertionError("错误 token 不该通过")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, exc.code
        ok("/pull 短别名等价于 once=1&plain=1 且同样校验 token")
    finally:
        httpd.shutdown()
        store.close()


def test_serialization(tmp: Path) -> None:
    print("并发安全与 JSON 序列化：")
    import threading

    cfg = make_config(tmp, free_port(), tag="concurrency")
    store = Store(cfg.db_path)
    day = datetime.now().strftime("%Y-%m-%d")
    store.ensure_plan(day, dtime(0, 0), dtime(23, 59))
    store.save_text(day, "并发测试", "api", "m")
    results: list[str | None] = []
    lock = threading.Lock()

    def worker() -> None:
        value = store.claim(day, 15)
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r == "并发测试" for r in results), results
    assert store.get(day)["delivery_count"] == 20
    store.close()
    ok("20 个并发取走请求都拿到内容且计数正确")


def main() -> int:
    print(f"TimeMsg 自检（Python {sys.version.split()[0]}）\n")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        test_clean()
        test_clean_multiline()
        test_example_config()
        test_tunnel(tmp)
        test_tunnel_args()
        test_context(tmp)
        test_context_skips_large(tmp)
        test_health_summary(tmp)
        test_prompt_file(tmp)
        test_prompt_versions(tmp)
        test_refresh_tunables(tmp)
        test_reminder_probability(tmp)
        test_length_placeholders()
        test_strip_markdown()
        test_store_migration(tmp)
        test_long_form_fallback(tmp)
        test_store(tmp)
        test_ensure_today(tmp)
        test_server(tmp)
        test_serialization(tmp)
    print(f"\n全部通过（{PASSED} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
