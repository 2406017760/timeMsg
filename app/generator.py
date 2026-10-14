"""文案生成：调用 OpenAI 兼容的 Chat Completions 接口，失败时用本地句子兜底。

支持两种形态：
* 一句话模式（keep_newlines = false）：只保留第一行，适合每日一句
* 长问候模式（keep_newlines = true）：保留段落，适合两三百字的早安留言

为什么要有兜底：这个程序每天早上只跑一次，如果那一刻 API 抽风或断网，
宁可发一句本地的句子，也不要让你某天早上什么都收不到。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import Config, load_system_prompt
from .context import ContextBundle, load_context
from .store import Store

log = logging.getLogger("timemsg.generator")

WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]

# 断网/接口异常时的兜底句子（全部为本项目原创，不带出处）。
FALLBACK_LINES: List[str] = [
    "把今天最难的那件事放在最前面，剩下的时间都会轻一点。",
    "慢一点没关系，路不会因为你走得快就变短。",
    "你今天不需要成为更好的人，只需要把水烧开，把饭吃好。",
    "有些答案不在想清楚之后出现，而在动手之后。",
    "清晨最好的部分，是还没有人对你提出要求。",
    "如果你不知道该往哪走，就先走到窗边看看外面。",
    "把担心写下来，它们多半撑不过一张纸。",
    "今天可以让一件小事做得比昨天认真一点，就够了。",
    "别急着给今天下结论，它才刚开始。",
    "允许自己有一段什么都做不完的日子。",
    "你以为的拖延，可能只是身体在要求休息。",
    "把手机放远一点，世界不会因此漏掉你。",
    "真正让人踏实的，往往是重复了千百遍的小事。",
    "有些门不是锁着，只是你今天还没走到它面前。",
    "如果你在等一个信号，那就把这句当成信号。",
    "今天的天气和你今天的心情，是两件可以分开的事。",
    "不必每件事都解释给别人听，也不必每件事都解释给自己听。",
    "把要做的三件事写下来，剩下的交给时间。",
    "你不需要热爱所有正在做的事，只需要知道为什么做它。",
    "有些疲惫睡一觉就好了，有些需要一次长谈，先分清是哪种。",
    "今天可以少证明一点，多感受一点。",
    "如果心里很吵，就去洗个碗，水声能帮上忙。",
    "真正的进步常常平淡到你察觉不到，直到某天回头。",
    "别忘了你今天也可能成为别人一天里最好的那部分。",
    "把该说的话说出来，把不该说的话咽下去，两者都需要练习。",
    "你正在做的事，也许还没结果，但已经改变了你。",
    "早上的第一件事，决定这一天是你在过它，还是它在过你。",
    "给自己留一点没用的时间，它常常比有用的时间更养人。",
    "不是所有努力都要被看见，但所有休息都不需要理由。",
    "如果今天只能完成一件事，就选那件让你晚上睡得着的。",
    "世界很吵，你的节奏不需要跟它调成一致。",
    "你不必每天都有进步，允许有些日子只是用来维持。",
    "把窗打开，让今天有一点新鲜的空气进来。",
    "你今天要面对的，多半比你想的轻。",
    "担心的事十有八九不会发生，剩下的一两件也不会要命。",
    "认真做好一件小事，是抵抗焦虑最便宜的方式。",
    "允许自己偶尔只想躺着，人不是机器，机器也需要停机。",
    "今天的任务不是变强，是别把自己弄丢。",
    "要记得吃饭，这话听起来没用，但你今天可能真会忘。",
    "路还长，先把鞋带系好。",
]

# 长问候模式下的兜底：结构完整、语气温暖，长度与目标接近，能独立成一条消息。
FALLBACK_GREETINGS: List[str] = [
    "早。醒来先别急着摸手机，给自己十秒钟，感受一下今天的身体到底是什么状态。"
    "要是还有点沉，那就慢一点开始，今天没有哪件事急到需要你从床上弹起来。\n\n"
    "把今天最重要的一件事挑出来，先做它，做完这一天就算立住了。剩下的时间允许自己松一点，"
    "效率低一点真的没关系。人不是每一天都得满血，能维持住也算本事。\n\n"
    "记得吃早饭，出门前看一眼天气。今天也会过去的，希望它过去得温柔一点，"
    "希望你回头看的时候，觉得今天没有白过。",
    "早上好。不管昨天睡得怎么样，今天都是新的一页，先别急着给它下结论。\n\n"
    "如果这会儿还有点困，那就允许自己慢半拍开始，没必要一睁眼就把自己绷紧。"
    "今天做到你力所能及的程度就已经很好了，剩下的交给时间，它比你会安排。\n\n"
    "如果今天只能完成一件事，就选那件能让你晚上睡得安稳的。其他的往后放，"
    "明天再说也来得及，世界不会因为你晚一天就塌掉。\n\n"
    "记得喝水，记得吃饭，累的时候允许自己停一下。你已经在前进了，"
    "不用每一步都证明给别人看。",
    "早。今天可能会有点忙，也可能有点乱，但你能应付的，这一点你自己心里清楚。\n\n"
    "真的忙不过来的时候，就把眼前最小的一件事做完，别去想整座山。"
    "清单上的事情可以往后挪，人不能往后挪。\n\n"
    "累了就停一停，喝口水，站起来走两步，看看窗外。你不需要在所有人面前都表现得游刃有余，"
    "偶尔手忙脚乱也很正常。忙不是问题，乱才是问题，先把顺序理出来。\n\n"
    "照顾好自己，其他都好商量。愿你今天遇到的麻烦都不大，遇到的人都好说话。",
    "早上好呀。今天不用把每件事都做漂亮，能往前推进一点点就够了。\n\n"
    "真正拉开差距的从来不是某一天的爆发，而是你还愿意继续。累了就歇，歇够了再走，"
    "这不是懈怠，这是可持续。别拿今天的状态去否定你之前所有的努力，"
    "状态本来就有起伏，你只是刚好在低处。今天只要你还在往前走，就不算浪费。\n\n"
    "给自己留十分钟什么都不干的时间，坐着喝点热的。光进来之后，"
    "事情往往会显得比昨天晚上小一些。",
    "早。今天的第一件事可以是给自己倒杯水，或者把窗帘拉开。\n\n"
    "别急着看消息，别急着回那些其实可以晚一点回的话。你的一天不需要从别人的节奏开始，"
    "先让自己站稳，再去处理世界。急着回应所有人，最后往往没顾上自己。\n\n"
    "如果今天有什么必须做完的事，那就先做那一件，其他的能拖就拖。"
    "记得吃早饭，别拿咖啡当饭。愿你今天顺利，也愿你今天不太累，"
    "晚上躺下的时候心里是踏实的。",
]

SENTENCE_ENDS = ("。", "！", "？", "…", "\n")


@dataclass
class GenResult:
    text: str
    source: str  # api | fallback
    model: str
    error: Optional[str] = None
    context_files: List[str] = field(default_factory=list)
    context_chars: int = 0


def _strip_wrapping(text: str) -> str:
    pairs = [("「", "」"), ("《", "》"), ("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'"), ("【", "】")]
    for left, right in pairs:
        if text.startswith(left) and text.endswith(right) and len(text) > len(left) + len(right):
            return text[len(left) : -len(right)].strip()
    return text


def _strip_label(text: str) -> str:
    for prefix in ("一句话：", "今日一句话：", "今日一句：", "句子：", "文案：", "问候：", "早安：", "留言：", "内容："):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text


def _clean(text: str, keep_newlines: bool = False) -> str:
    """清洗模型输出。

    keep_newlines=False：只取第一段有效内容（每日一句场景，模型爱加的解说要去掉）
    keep_newlines=True ：保留段落结构，仅去掉代码围栏、markdown 强调符号和首行标签
    """
    if not text:
        return ""
    out = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    # 去掉 ``` 围栏
    if out.startswith("```"):
        out = re.sub(r"^```[a-zA-Z0-9]*\n?", "", out)
        out = re.sub(r"\n?```\s*$", "", out).strip()

    if not keep_newlines:
        for line in out.splitlines():
            if line.strip():
                out = line.strip()
                break
        return _strip_wrapping(_strip_label(out))

    out = re.sub(r"\*\*(.+?)\*\*", r"\1", out)  # 去 **加粗**
    out = re.sub(r"^#{1,6}\s*", "", out, flags=re.MULTILINE)  # 去 ## 标题
    lines = [line.rstrip() for line in out.splitlines()]
    if lines and lines[0].strip():
        lines[0] = _strip_label(lines[0].strip())
    out = "\n".join(lines).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)  # 最多保留一个空行
    return _strip_wrapping(out)


def _enforce_hard_cap(text: str, hard: int) -> str:
    """硬上限：超长时在句子边界截断，避免发出去一条失控的长文。"""
    if hard <= 0 or len(text) <= hard:
        return text
    cut = text[:hard]
    best = -1
    for punct in SENTENCE_ENDS:
        idx = cut.rfind(punct)
        if idx > best:
            best = idx
    if best >= hard // 2:
        return cut[: best + 1].strip()
    return cut.rstrip() + "…"


def _sample(rng: random.Random, pool: List[str], count: int) -> List[str]:
    if not pool:
        return []
    count = max(1, min(count, len(pool)))
    return rng.sample(pool, count)


LENGTH_PLACEHOLDER_RE = re.compile(r"\{(min_chars|max_chars)([+-]\d+)?\}")


def _fill_length_placeholders(system: str, min_chars: int, max_chars: int) -> str:
    """替换提示词里的字数占位符，支持偏移写法。

    {min_chars} / {max_chars} / {min_chars-50} / {max_chars+100} 都认。
    不这样写的话，{max_chars+100} 会被原样送给模型，字数约束就静默失效了。
    """

    def repl(match: "re.Match[str]") -> str:
        base = min_chars if match.group(1) == "min_chars" else max_chars
        offset = int(match.group(2) or 0)
        return str(max(1, base + offset))

    return LENGTH_PLACEHOLDER_RE.sub(repl, system)


def build_messages(
    cfg: Config,
    recent: List[str],
    rng: random.Random,
    context_text: str = "",
) -> List[Dict[str, str]]:
    themes = _sample(rng, cfg.prompt.themes, rng.randint(1, 3))
    styles = _sample(rng, cfg.prompt.styles, rng.randint(1, 2))

    # 现读提示词文件：改完风格立刻生效，不需要重启服务
    system = load_system_prompt(cfg) or (
        "你是「每日一句话」的作者。只输出内容本身，不要标题、引号、解释或署名，"
        f"长度 {cfg.prompt.min_chars}~{cfg.prompt.max_chars} 个汉字。"
    )
    system = _fill_length_placeholders(system, cfg.prompt.min_chars, cfg.prompt.max_chars)

    now = datetime.now()
    today = f"{now.year}年{now.month}月{now.day}日 星期{WEEKDAYS[now.weekday()]}"

    parts: List[str] = []
    if context_text.strip():
        parts.append(
            "【我的个人资料与作息】\n"
            "下面是用户自己维护的资料。请自然地结合其中相关的细节，"
            "不要生硬罗列、不要逐条复述，也不要编造资料里没有的信息。\n\n"
            + context_text
        )
    else:
        parts.append("（用户还没有提供个人资料，请写一段通用的温暖问候。）")

    parts.append(f"【今天】{today}")

    # 日常提醒（天气、穿衣、吃饭这类）用代码掷骰子控制概率。
    # 指望模型自己把握「35% 概率」不现实——它每次都会觉得"今天正好该提醒一句"。
    if rng.random() < cfg.prompt.daily_reminder_rate:
        parts.append(
            "【今天的额外许可】今天可以带一句日常的照看（天气、加减衣服、吃饭、早点睡这类），"
            "整个消息里最多一句，而且要说得自然，像是顺口提的。"
        )
        log.info("本次生成：允许日常提醒（命中 %.0f%% 概率）", cfg.prompt.daily_reminder_rate * 100)
    else:
        parts.append(
            "【今天的额外限制】今天不要写任何天气、加减衣服、吃饭、早点睡这类日常提醒，"
            "一句都不要，也别换个说法变相写（比如「最近降温了」也算）。这次就聊点别的。"
        )
        log.info("本次生成：禁止日常提醒（未命中 %.0f%% 概率）", cfg.prompt.daily_reminder_rate * 100)

    if themes:
        parts.append("可参考的主题倾向：" + "、".join(themes))
    if styles:
        parts.append("可参考的语气风格：" + "、".join(styles))
    if recent:
        recent_block = "\n\n".join(f"- {t}" for t in recent)
        parts.append(
            "【你自己前些天发给他的话】\n"
            "注意：这些话是你说的，不是他说的。默认请换个角度，不要重复。\n"
            "但你也可以偶尔回顾其中某一句——承认当时说重了、想偏了，或者现在有了别的看法。\n\n"
            + recent_block
        )
    parts.append("请直接给出这一次的内容。")

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def _call_api(cfg: Config, messages: List[Dict[str, str]]) -> str:
    payload = {
        "model": cfg.ai.model,
        "messages": messages,
        "temperature": cfg.ai.temperature,
        "max_tokens": cfg.ai.max_tokens,
        "stream": False,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(cfg.ai.url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {cfg.ai.api_key}")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=cfg.ai.timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    parsed = json.loads(body)
    choices = parsed.get("choices") or []
    if not choices:
        raise RuntimeError(f"接口没有返回 choices：{body[:300]}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        raise RuntimeError(f"接口返回内容为空：{body[:300]}")
    return str(content)


def generate(
    cfg: Config,
    store: Store,
    rng: Optional[random.Random] = None,
    bundle: Optional[ContextBundle] = None,
) -> GenResult:
    """生成今天的内容。永远返回结果，最差也是本地兜底句子。"""
    rng = rng or random.Random()
    long_form = cfg.prompt.keep_newlines or cfg.prompt.max_chars > 120
    bundle = bundle if bundle is not None else load_context(cfg)

    # 长问候模式下少放几条历史，避免提示词里塞进几千字
    recent = store.recent_texts(10 if long_form else 30)

    if cfg.ai.mock:
        log.info("mock 模式，使用本地兜底内容")
        return _fallback_result(recent, rng, long_form, bundle, error=None)

    if not cfg.ai.api_key:
        log.warning("未配置 API key，使用本地兜底内容")
        return _fallback_result(recent, rng, long_form, bundle, error="缺少 api_key")

    messages = build_messages(cfg, recent, rng, bundle.text)
    hard = cfg.prompt.hard_max_chars or cfg.prompt.max_chars * 3
    last_error = ""

    for attempt in range(1, cfg.ai.attempts + 1):
        try:
            raw = _call_api(cfg, messages)
            text = _enforce_hard_cap(_clean(raw, cfg.prompt.keep_newlines), hard)
            if not text:
                raise RuntimeError("清洗后内容为空")

            # 模型普遍写不够字数。与其在提示词里喊一嗓子，不如实测太短就纠正重试一次。
            if len(text) < cfg.prompt.min_chars and attempt < cfg.ai.attempts:
                log.warning(
                    "生成结果只有 %s 字（目标 %s~%s），追加纠正指令后重试",
                    len(text),
                    cfg.prompt.min_chars,
                    cfg.prompt.max_chars,
                )
                messages = messages + [
                    {"role": "assistant", "content": raw[:4000]},
                    {
                        "role": "user",
                        "content": (
                            f"这一版只有 {len(text)} 个字，太短了。请扩写到 "
                            f"{cfg.prompt.min_chars}~{cfg.prompt.max_chars} 个汉字："
                            "补充更具体的细节（时间、动作、感受），保持完全相同的语气和分段方式，"
                            "不要新增资料里没有的事实，直接输出扩写后的完整内容。"
                        ),
                    },
                ]
                continue

            # 反过来，写太长也要压回来（长问候很容易越写越多）。
            # 只留 10 个字容差：模型超一点点就要求它压缩，否则很容易越放越松。
            soft_max = cfg.prompt.max_chars + 10
            if len(text) > soft_max and attempt < cfg.ai.attempts:
                log.warning(
                    "生成结果 %s 字，超过上限 %s（含 25%% 余量），追加压缩指令后重试",
                    len(text),
                    soft_max,
                )
                messages = messages + [
                    {"role": "assistant", "content": raw[:4000]},
                    {
                        "role": "user",
                        "content": (
                            f"这一版有 {len(text)} 个字，太长了。请压缩到 "
                            f"{cfg.prompt.min_chars}~{cfg.prompt.max_chars} 个汉字："
                            "删掉最不重要的细节和重复的叮嘱，保留最具体的那一两个，"
                            "语气和分段方式完全不变，不要新增任何事实，直接输出压缩后的完整内容。"
                        ),
                    },
                ]
                continue

            log.info(
                "AI 生成成功（第 %s 次尝试，%s 字，资料 %s 字）：%s",
                attempt,
                len(text),
                bundle.chars,
                text.replace("\n", " / "),
            )
            return GenResult(
                text=text,
                source="api",
                model=cfg.ai.model,
                context_files=bundle.files,
                context_chars=bundle.chars,
            )
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            last_error = f"HTTP {exc.code} {exc.reason} {detail}"
        except Exception as exc:  # noqa: BLE001 - 网络类异常种类多，统一重试
            last_error = f"{type(exc).__name__}: {exc}"
        log.warning("第 %s/%s 次调用失败：%s", attempt, cfg.ai.attempts, last_error)
        if attempt < cfg.ai.attempts:
            time.sleep(cfg.ai.backoff * attempt)

    log.error("AI 调用全部失败，改用本地兜底内容。最后错误：%s", last_error)
    return _fallback_result(recent, rng, long_form, bundle, error=last_error)


def _fallback_result(
    recent: List[str],
    rng: random.Random,
    long_form: bool,
    bundle: ContextBundle,
    error: Optional[str],
) -> GenResult:
    text = _pick_fallback(recent, rng, long_form)
    return GenResult(
        text=text,
        source="fallback",
        model="local",
        error=error,
        context_files=bundle.files,
        context_chars=bundle.chars,
    )


def _pick_fallback(recent: List[str], rng: random.Random, long_form: bool) -> str:
    pool = FALLBACK_GREETINGS if long_form else FALLBACK_LINES
    fresh = [item for item in pool if item not in recent]
    return rng.choice(fresh or pool)
