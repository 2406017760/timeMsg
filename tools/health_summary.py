"""把 Apple 健康的原始导出压缩成一份简短摘要，供每日问候使用。

为什么需要这一步：Apple 健康的「导出.xml」动辄一两百 MB，
直接塞进提示词有两个问题——读不动，而且毫无重点（截断取到的都是 schema 声明）。

所以这里做一次离线提炼：流式扫描（不会把 200MB 读进内存），
只保留最近一段时间的趋势数字，写成 profile/健康数据.md。
之后程序每天照常读那个小结就够了。

用法：
    python tools\\health_summary.py                  # 默认最近 30 天
    python tools\\health_summary.py --days 60
    python tools\\health_summary.py --input "profile/apple_health_export/导出.xml"
    python tools\\health_summary.py --out profile/健康数据.md
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "profile" / "健康数据.md"

ASLEEP_VALUES = {
    "HKCategoryValueSleepAnalysisAsleep",
    "HKCategoryValueSleepAnalysisAsleepCore",
    "HKCategoryValueSleepAnalysisAsleepDeep",
    "HKCategoryValueSleepAnalysisAsleepREM",
    "HKCategoryValueSleepAnalysisAsleepUnspecified",
}

WORKOUT_NAMES = {
    "HKWorkoutActivityTypeRunning": "跑步",
    "HKWorkoutActivityTypeWalking": "步行",
    "HKWorkoutActivityTypeCycling": "骑行",
    "HKWorkoutActivityTypeSwimming": "游泳",
    "HKWorkoutActivityTypeTraditionalStrengthTraining": "力量训练",
    "HKWorkoutActivityTypeFunctionalStrengthTraining": "功能训练",
    "HKWorkoutActivityTypeHighIntensityIntervalTraining": "HIIT",
    "HKWorkoutActivityTypeYoga": "瑜伽",
    "HKWorkoutActivityTypeHiking": "徒步",
    "HKWorkoutActivityTypeRowing": "划船",
    "HKWorkoutActivityTypeElliptical": "椭圆机",
    "HKWorkoutActivityTypeJumpRope": "跳绳",
    "HKWorkoutActivityTypeBadminton": "羽毛球",
    "HKWorkoutActivityTypeBasketball": "篮球",
    "HKWorkoutActivityTypeSoccer": "足球",
    "HKWorkoutActivityTypeTableTennis": "乒乓球",
    "HKWorkoutActivityTypeTennis": "网球",
    "HKWorkoutActivityTypeCoreTraining": "核心训练",
    "HKWorkoutActivityTypeMindAndBody": "身心练习",
    "HKWorkoutActivityTypeOther": "其他训练",
}


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _to_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _km(value: float, unit: str) -> float:
    unit = (unit or "").lower()
    if unit in ("mi", "mile", "miles"):
        return value * 1.609344
    if unit in ("m", "meter", "meters"):
        return value / 1000.0
    return value  # 默认按 km


def _kg(value: float, unit: str) -> float:
    unit = (unit or "").lower()
    if unit in ("lb", "lbs", "pound", "pounds"):
        return value * 0.45359237
    return value


def _minutes(value: float, unit: str) -> float:
    unit = (unit or "").lower()
    if unit.startswith("sec"):
        return value / 60.0
    if unit.startswith("h"):
        return value * 60.0
    return value


@dataclass
class HealthData:
    steps: Dict[date, float] = field(default_factory=lambda: defaultdict(float))
    distance: Dict[date, float] = field(default_factory=lambda: defaultdict(float))
    resting_hr: Dict[date, List[float]] = field(default_factory=lambda: defaultdict(list))
    hr: Dict[date, List[float]] = field(default_factory=lambda: defaultdict(list))
    sleep: Dict[date, float] = field(default_factory=lambda: defaultdict(float))
    body_mass: List[Tuple[date, float]] = field(default_factory=list)
    workouts: List[dict] = field(default_factory=list)
    rings: Dict[date, dict] = field(default_factory=dict)
    earliest: Optional[date] = None
    latest: Optional[date] = None
    record_count: int = 0

    def note_date(self, day: Optional[date]) -> None:
        if not day:
            return
        if self.earliest is None or day < self.earliest:
            self.earliest = day
        if self.latest is None or day > self.latest:
            self.latest = day


def _workout_stat(elem: ET.Element, suffixes: Tuple[str, ...]) -> Tuple[Optional[float], str]:
    for child in elem:
        if _localname(child.tag) != "WorkoutStatistics":
            continue
        stat_type = child.get("type") or ""
        if any(stat_type.endswith(s) for s in suffixes):
            value = _to_float(child.get("sum") or child.get("average"))
            if value is not None:
                return value, child.get("unit") or ""
    return None, ""


def parse_export(path: Path, log_every: int = 500_000) -> HealthData:
    """流式解析 Apple 健康导出。

    用 iterparse 逐个元素处理并立刻 clear()，200MB 的文件也不会把内存吃满。
    """
    data = HealthData()
    seen = 0
    for _event, elem in ET.iterparse(str(path), events=("end",)):
        tag = _localname(elem.tag)
        if tag == "Record":
            data.record_count += 1
            seen += 1
            if log_every and seen % log_every == 0:
                print(f"  …已扫描 {seen:,} 条记录")
            rtype = elem.get("type") or ""
            value = _to_float(elem.get("value"))
            start = _parse_dt(elem.get("startDate"))
            end = _parse_dt(elem.get("endDate")) or start
            day = end.date() if end else None
            if value is not None and day:
                if rtype == "HKQuantityTypeIdentifierStepCount":
                    data.steps[day] += value
                elif rtype == "HKQuantityTypeIdentifierDistanceWalkingRunning":
                    data.distance[day] += _km(value, elem.get("unit") or "km")
                elif rtype == "HKQuantityTypeIdentifierRestingHeartRate":
                    data.resting_hr[day].append(value)
                elif rtype == "HKQuantityTypeIdentifierHeartRate":
                    data.hr[day].append(value)
                elif rtype == "HKQuantityTypeIdentifierBodyMass":
                    data.body_mass.append((day, _kg(value, elem.get("unit") or "kg")))
                data.note_date(day)
            elif rtype == "HKCategoryTypeIdentifierSleepAnalysis" and start and end:
                if (elem.get("value") or "") in ASLEEP_VALUES:
                    data.sleep[end.date()] += (end - start).total_seconds()
                data.note_date(end.date())
            elem.clear()

        elif tag == "Workout":
            start = _parse_dt(elem.get("startDate"))
            end = _parse_dt(elem.get("endDate"))
            duration = _to_float(elem.get("duration"))
            duration_unit = elem.get("durationUnit") or "min"
            if duration is None and start and end:
                duration, duration_unit = (end - start).total_seconds(), "sec"
            distance = _to_float(elem.get("totalDistance"))
            distance_unit = elem.get("totalDistanceUnit") or "km"
            energy = _to_float(elem.get("totalEnergyBurned"))
            if distance is None:
                distance, distance_unit = _workout_stat(
                    elem, ("DistanceWalkingRunning", "DistanceCycling", "DistanceSwimming")
                )
            if energy is None:
                energy, _ = _workout_stat(elem, ("ActiveEnergyBurned", "BasalEnergyBurned"))
            if start and duration is not None:
                data.workouts.append(
                    {
                        "day": start.date(),
                        "kind": WORKOUT_NAMES.get(
                            elem.get("workoutActivityType") or "", "训练"
                        ),
                        "minutes": round(_minutes(duration, duration_unit), 1),
                        "km": round(_km(distance, distance_unit), 2) if distance else None,
                        "kcal": round(energy) if energy else None,
                    }
                )
                data.note_date(start.date())
            elem.clear()

        elif tag == "ActivitySummary":
            day = _parse_dt(elem.get("dateComponents"))
            if day:
                data.rings[day.date()] = {
                    "energy": _to_float(elem.get("activeEnergyBurned")),
                    "exercise": _to_float(elem.get("appleExerciseTime")),
                    "stand": _to_float(elem.get("appleStandHours")),
                }
                data.note_date(day.date())
            elem.clear()

    return data


def parse_gpx_distances(folder: Path) -> List[Tuple[str, float, int]]:
    """算每条 GPS 轨迹的大致里程（用 haversine 逐点累加）。"""
    out: List[Tuple[str, float, int]] = []
    if not folder.exists():
        return out
    for path in sorted(folder.glob("*.gpx")):
        try:
            points: List[Tuple[float, float]] = []
            for _event, elem in ET.iterparse(str(path), events=("end",)):
                if _localname(elem.tag) == "trkpt":
                    lat, lon = _to_float(elem.get("lat")), _to_float(elem.get("lon"))
                    if lat is not None and lon is not None:
                        points.append((lat, lon))
                    elem.clear()
            km = 0.0
            for (lat1, lon1), (lat2, lon2) in zip(points, points[1:]):
                dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
                a = (
                    math.sin(dlat / 2) ** 2
                    + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
                )
                km += 6371.0 * 2 * math.asin(math.sqrt(a))
            out.append((path.name, km, len(points)))
        except ET.ParseError:
            out.append((path.name, 0.0, 0))
    return out


def parse_cda(path: Path) -> Tuple[int, List[str]]:
    """临床记录导出：只取条目数量和最近的几个标题。"""
    if not path.exists():
        return 0, []
    titles: List[str] = []
    count = 0
    try:
        for _event, elem in ET.iterparse(str(path), events=("end",)):
            name = _localname(elem.tag)
            if name == "entry":
                count += 1
            elif name == "title" and elem.text and len(titles) < 200:
                text = elem.text.strip()
                if text and text not in titles:
                    titles.append(text)
            elem.clear()
    except ET.ParseError:
        pass
    return count, titles[-8:]


def _fmt_duration(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    return f"{minutes // 60} 小时 {minutes % 60} 分" if minutes >= 60 else f"{minutes} 分钟"


def _avg(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def build_summary(data: HealthData, days: int, source: Path) -> str:
    today = date.today()
    # 统计口径不含今天：今天还没过完，把它算进去会把"半天数据"当成低活动日、
    # 也会拉低平均值。今天的数据单独在逐日明细里标出来。
    since = today - timedelta(days=days)
    until = today - timedelta(days=1)

    def in_window(day: date) -> bool:
        return since <= day <= until

    lines: List[str] = []

    lines.append("健康数据摘要（由 tools/health_summary.py 从 Apple 健康导出自动提炼）")
    lines.append(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}　统计范围：{since} ~ {until}（{days} 个完整日）")
    if data.earliest and data.latest:
        lines.append(f"导出数据覆盖：{data.earliest} ~ {data.latest}（共 {data.record_count:,} 条记录）")
    lines.append("")

    # ---- 概览
    window_steps = {d: v for d, v in data.steps.items() if in_window(d)}
    if window_steps:
        avg_steps = sum(window_steps.values()) / len(window_steps)
        top_day = max(window_steps.items(), key=lambda kv: kv[1])
        low_days = sorted(d for d, v in window_steps.items() if v < 3000)
        lines.append(f"步数：最近 {days} 天平均 {avg_steps:,.0f} 步/天，"
                     f"最高 {top_day[1]:,.0f} 步（{top_day[0]}）")
        if low_days:
            lines.append(f"　　　其中不足 3000 步的有 {len(low_days)} 天，最近一次是 {low_days[-1]}")

    window_dist = {d: v for d, v in data.distance.items() if in_window(d)}
    if window_dist:
        total_km = sum(window_dist.values())
        lines.append(f"步行+跑步里程：最近 {days} 天合计 {total_km:.1f} km")

    window_rhr = {d: v for d, v in data.resting_hr.items() if in_window(d)}
    if window_rhr:
        daily = {d: _avg(v) for d, v in window_rhr.items()}
        last_day = max(daily)
        lines.append(f"静息心率：最近 {days} 天平均 {_avg(list(daily.values())):.0f} bpm，"
                     f"最近一次 {daily[last_day]:.0f} bpm（{last_day}）")
    window_hr = {d: v for d, v in data.hr.items() if in_window(d)}
    if window_hr:
        flat = [x for vs in window_hr.values() for x in vs]
        lines.append(f"日常心率：最近 {days} 天记录 {len(flat):,} 次，"
                     f"区间 {min(flat):.0f}–{max(flat):.0f} bpm，平均 {_avg(flat):.0f}")

    window_sleep = {d: v for d, v in data.sleep.items() if in_window(d)}
    if window_sleep:
        avg_sleep = sum(window_sleep.values()) / len(window_sleep)
        shortest = min(window_sleep.items(), key=lambda kv: kv[1])
        longest = max(window_sleep.items(), key=lambda kv: kv[1])
        short_nights = [d for d, v in window_sleep.items() if v < 6 * 3600]
        lines.append(f"睡眠：最近 {days} 天里 {len(window_sleep)} 天有记录，"
                     f"平均 {_fmt_duration(avg_sleep)}/晚")
        lines.append(f"　　　最短 {_fmt_duration(shortest[1])}（{shortest[0]}），"
                     f"最长 {_fmt_duration(longest[1])}（{longest[0]}）")
        if short_nights:
            lines.append(f"　　　不足 6 小时的有 {len(short_nights)} 天，"
                         f"最近一次是 {max(short_nights)}")

    if data.body_mass:
        ordered = sorted(data.body_mass)
        last_day, last_kg = ordered[-1]
        line = f"体重：最近一次 {last_kg:.1f} kg（{last_day}）"
        within = [x for x in ordered[:-1] if (last_day - x[0]).days <= days]
        if within:
            prev_day, prev_kg = within[-1]
            line += f"，比 {prev_day} 的 {prev_kg:.1f} kg {last_kg - prev_kg:+.1f} kg"
        elif len(ordered) >= 2:
            prev_day, prev_kg = ordered[-2]
            line += f"，上一次记录是 {prev_kg:.1f} kg（{prev_day}，距今 {(last_day - prev_day).days} 天）"
        lines.append(line)

    window_workouts = [w for w in data.workouts if in_window(w["day"])]
    if window_workouts:
        total_min = sum(w["minutes"] for w in window_workouts)
        by_kind: Dict[str, List[dict]] = defaultdict(list)
        for w in window_workouts:
            by_kind[w["kind"]].append(w)
        parts = []
        for kind, items in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
            km = sum(i["km"] or 0 for i in items)
            parts.append(f"{kind} {len(items)} 次" + (f"（{km:.1f} km）" if km else ""))
        lines.append(f"锻炼：最近 {days} 天 {len(window_workouts)} 次，"
                     f"合计 {_fmt_duration(total_min * 60)}；" + "，".join(parts))
    else:
        lines.append(f"锻炼：最近 {days} 天没有运动记录")

    lines.append("")

    # ---- 最近 7 个完整日
    # 这里刻意不用「今天/昨天/前天」这种相对词：摘要文件是一次生成、之后每天被读，
    # 相对词第二天就过期了，模型会拿过期标签误配日期。
    # 而且今天的数据没走完，列出来会被当成「低活动日」，直接不列。
    lines.append(f"最近 7 个完整日（数据截止 {until}，此后的数据尚未完整）：")
    for offset in range(7, 0, -1):
        day = today - timedelta(days=offset)
        bits = []
        if day in data.steps:
            bits.append(f"步数 {data.steps[day]:,.0f}")
        if day in data.resting_hr:
            bits.append(f"静息心率 {_avg(data.resting_hr[day]):.0f}")
        if day in data.sleep:
            bits.append(f"睡眠 {_fmt_duration(data.sleep[day])}")
        rings = data.rings.get(day)
        if rings and rings.get("exercise") is not None:
            bits.append(f"锻炼 {rings['exercise']:.0f} 分钟")
        if day in data.distance:
            bits.append(f"走跑 {data.distance[day]:.1f} km")
        lines.append(f"　{day}　" + ("　".join(bits) if bits else "（无记录）"))

    lines.append("")
    lines.append(
        "（提醒：这个文件是离线提炼的快照。数字只到上面那个日期为止，"
        "不要引用更晚的、也不要引用今天的数据——今天还没结束。）"
    )
    lines.append("")

    # ---- 最近锻炼
    recent_workouts = sorted(data.workouts, key=lambda w: w["day"])[-6:]
    if recent_workouts:
        lines.append("最近的锻炼记录：")
        for w in reversed(recent_workouts):
            detail = f"{w['minutes']:.0f} 分钟"
            if w["km"]:
                detail += f"，{w['km']:.2f} km"
            if w["kcal"]:
                detail += f"，{w['kcal']} 千卡"
            lines.append(f"　{w['day']}　{w['kind']}　{detail}")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="从 Apple 健康导出提炼一份简短摘要")
    parser.add_argument("--input", help="导出.xml 的路径；不给就自动找 apple_health_export 下最大的 xml")
    parser.add_argument("--days", type=int, default=30, help="统计最近多少天，默认 30")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出文件，默认 profile/健康数据.md")
    args = parser.parse_args()

    export_dir = ROOT / "profile" / "apple_health_export"
    if args.input:
        source = Path(args.input)
        if not source.is_absolute():
            source = ROOT / source
    else:
        candidates = [p for p in export_dir.glob("*.xml") if p.stat().st_size > 1024 * 1024]
        if not candidates:
            print(f"没找到健康导出文件，请把 Apple 健康的导出放在：{export_dir}", file=sys.stderr)
            return 1
        source = max(candidates, key=lambda p: p.stat().st_size)

    if not source.exists():
        print(f"文件不存在：{source}", file=sys.stderr)
        return 1

    size_mb = source.stat().st_size / 1048576
    print(f"解析：{source.name}（{size_mb:.1f} MB）…")
    started = datetime.now()
    data = parse_export(source)
    print(f"  完成，用时 {(datetime.now() - started).total_seconds():.1f} 秒")

    text = build_summary(data, args.days, source)

    # 附上 GPS 轨迹与临床记录（有才算）
    routes = parse_gpx_distances(export_dir / "workout-routes")
    if routes:
        text += "\nGPS 轨迹文件：\n"
        for name, km, points in routes:
            text += f"　{name}　约 {km:.2f} km（{points} 个点）\n"
        text += "\n"

    cda = export_dir / "export_cda.xml"
    if cda.exists():
        count, titles = parse_cda(cda)
        text += f"临床记录导出（export_cda.xml）：{count} 条条目"
        if titles:
            text += "，包含例如：" + "、".join(titles[:5])
        text += "\n"

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"已写入：{out}（{len(text)} 字）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
