import streamlit as st
import io
from datetime import datetime, timedelta, date
from pathlib import Path
import os
import sys
import re
import csv
import socket
import json
from collections import Counter, defaultdict
import requests
from urllib.parse import urlparse

# 添加 src 到路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from sst_search.config import Settings
from sst_search.models import Review, SearchRequest
from sst_search.sst_client import SSTClient
from sst_search.analyzer import split_reviews_by_rating, word_freq
from sst_search.wordcloud_gen import generate_wordcloud
from sst_search.review_semantic import build_semantic_rows


def build_domain_stopwords(app_name: str, android_package: str, search_terms: str = "") -> set[str]:
    """Build dynamic stopwords from this run's search terms and identifiers."""
    words: set[str] = set()

    # Priority: tokens from the user's current search terms.
    words.update(re.findall(r"[a-zA-Z]{2,}", search_terms.lower()))

    # Backup: tokens from app name/package.
    words.update(re.findall(r"[a-zA-Z]{2,}", app_name.lower()))
    words.update(re.findall(r"[a-zA-Z]{2,}", android_package.lower()))

    # Keep common package fragments out as they are not insight-bearing.
    words.update({"com", "cn", "net", "org", "www"})

    # Chinese tokens from search terms/app name.
    zh_search = "".join(re.findall(r"[\u4e00-\u9fff]+", search_terms))
    if zh_search:
        words.add(zh_search)

    zh_name = "".join(re.findall(r"[\u4e00-\u9fff]+", app_name))
    if zh_name:
        words.add(zh_name)

    return words


def parse_stopwords(text: str) -> set[str]:
    tokens = re.split(r"[,，\n\s]+", text.strip())
    return {t.strip().lower() for t in tokens if t and t.strip()}


def parse_endgame_keywords(text: str) -> set[str]:
    """Parse grouped endgame keywords while skipping section headers."""
    keywords: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.fullmatch(r"【[^】]+】", line):
            continue

        normalized = re.sub(r"[【】\[\]{}()（）]", " ", line)
        normalized = normalized.replace("＋", "+")
        parts = re.split(r"[,，、;；/|+]", normalized)
        for part in parts:
            token = part.strip().strip(" ：:。．.；;,，")
            if not token:
                continue
            # Preserve English phrases like "power creep" and mixed terms like "roll词条".
            keywords.add(token)
    return {k for k in keywords if k}


def is_legacy_endgame_terms_text(value: str) -> bool:
    legacy = (
        "养成末端,养成后期,毕业,拉满,高投入,重复刷,高耗时,随机性,不确定性,圣遗物,词条,副词条,双暴,暴击率,暴击伤害,充能,精通,歪词条,roll,强化,替换,锁定,天赋,天赋书,天赋材料,武器突破,角色突破,突破材料,素材,刷材料,树脂,浓缩树脂,周本,深渊,历练,秘境,副本,artifact,substat,grind,farming,resin,abyss,weekly boss,ascension,talent book,rng"
    )
    normalized = re.sub(r"\s+", "", (value or "").strip())
    return normalized == re.sub(r"\s+", "", legacy)


def looks_like_api_key(token: str) -> bool:
    # SensorTower token通常是短横线/下划线/字母数字组成，且不应是URL。
    value = token.strip()
    if len(value) < 16:
        return False
    if re.search(r"[\u4e00-\u9fff]", value):
        return False
    if re.search(r"\s", value):
        return False
    if value.lower().startswith("http://") or value.lower().startswith("https://"):
        return False
    if "/" in value or "?" in value or "=" in value:
        return False
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        return False
    return True


def extract_api_key_from_input(raw_text: str) -> str | None:
    """Extract a probable SensorTower token from messy pasted text."""
    text = (raw_text or "").strip()
    if not text:
        return None

    # Preferred: explicit ST0_ token in mixed content (e.g. pasted traceback).
    match = re.search(r"\bST0_[A-Za-z0-9._-]{10,}\b", text)
    if match:
        return match.group(0)

    # Fallback: already a clean token.
    if looks_like_api_key(text):
        return text

    return None


def redact_sensitive_text(text: str) -> str:
    sanitized = text or ""
    sanitized = re.sub(r"\bST0_[A-Za-z0-9._-]{6,}\b", "ST0_[REDACTED]", sanitized)
    sanitized = re.sub(r"(?i)(auth_token=)[^&\s]+", r"\1[REDACTED]", sanitized)
    sanitized = re.sub(r"(?i)(api_key=)[^&\s]+", r"\1[REDACTED]", sanitized)
    sanitized = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1[REDACTED]", sanitized)
    return sanitized


def dns_precheck(host: str) -> tuple[bool, str]:
    try:
        socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
        return True, ""
    except socket.gaierror as exc:
        return False, str(exc)


def tcp_precheck(host: str, port: int = 443, timeout: float = 5.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except OSError as exc:
        return False, str(exc)


def https_precheck(url: str, timeout: float = 8.0) -> tuple[bool, str, int | None]:
    try:
        response = requests.get(url, timeout=timeout)
        return True, "", int(response.status_code)
    except requests.RequestException as exc:
        return False, str(exc), None


def parse_base_url_host(base_url: str) -> str | None:
    try:
        parsed = urlparse(base_url.strip())
        return parsed.hostname
    except ValueError:
        return None


def build_base_url_candidates(configured_base_url: str) -> list[str]:
    configured = configured_base_url.rstrip("/")
    candidates: list[str] = []

    def _append(url: str) -> None:
        normalized = url.rstrip("/")
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    _append(configured)

    lower = configured.lower()
    if "sensortower-china.com" in lower:
        _append("https://api.sensortower.com")
    elif "api.sensortower.com" in lower:
        _append("https://api.sensortower-china.com")

    return candidates


API_BASE_URL = "https://api.sensortower.com"
DEFAULT_IOS_COUNTRIES = ["US", "CN"]
DEFAULT_ANDROID_LANGUAGES = ["en", "zh"]
CORE_ENDGAME_TERMS = {
    "毕业", "拉满", "高投入", "满练",
    "圣遗物", "遗器", "驱动盘", "武器", "天赋",
    "定轨", "定向", "圣言自明机", "祝圣之霜", "尘脂", "自塑尘脂", "遂愿尘脂", "变量骰子", "母盘", "调律校音器", "谐振核心仪",
    "词条", "主词条", "副词条", "双暴", "暴击率", "暴击伤害", "充能", "精通", "击破特攻", "异常精通", "穿透", "有效词条", "无效词条",
    "天赋材料", "武器突破", "角色升级", "角色突破", "素材", "材料", "体力", "原萃树脂", "开拓力", "电量", "浓缩树脂", "燃料", "电池", "周本", "秘境", "副本", "刷本",
    "强化", "替换", "锁定", "分解", "拆解", "合成", "洗词条", "凹词条", "roll词条", "胚子", "升阶", "突破", "升级", "养成",
    "重复刷", "高耗时", "随机性", "不确定性", "歪词条", "毕业难", "材料缺口", "体力不足", "养成周期长", "零提升", "双倍活动",
}
CANONICAL_ENDGAME_TERMS_TEXT = (
    "毕业,拉满,高投入,满练,圣遗物,遗器,驱动盘,武器,天赋,定轨,定向,圣言自明机,祝圣之霜,尘脂,自塑尘脂,遂愿尘脂,变量骰子,母盘,调律校音器,谐振核心仪,词条,主词条,副词条,双暴,暴击率,暴击伤害,充能,精通,击破特攻,异常精通,穿透,有效词条,无效词条,天赋材料,武器突破,角色升级,角色突破,素材,材料,体力,原萃树脂,开拓力,电量,浓缩树脂,燃料,电池,周本,秘境,副本,刷本,强化,替换,锁定,分解,拆解,合成,洗词条,凹词条,roll词条,胚子,升阶,突破,升级,养成,重复刷,高耗时,随机性,不确定性,歪词条,毕业难,材料缺口,体力不足,养成周期长,零提升,双倍活动"
)
UI_MEMORY_PATH = Path("outputs_ui_latest") / "ui_memory.json"


def parse_saved_date(value: str | None, fallback: date) -> date:
    if not value:
        return fallback
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return fallback


def load_ui_memory() -> dict[str, object]:
    defaults: dict[str, object] = {
        "query_mode": "单平台查询",
        "ios_app_id": "6474233312",
        "android_package": "com.moonshot.kimichat",
        "start_date": (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"),
        "end_date": datetime.now().strftime("%Y-%m-%d"),
        "enable_batch_periods": False,
        "batch_mode": "连续周期",
        "batch_window_days": 7,
        "batch_count": 4,
        "include_latest_cycle": True,
        "manual_ranges": "",
        "version_periods_text": "",
        "data_source_mode": "SST API 抓取",
        "endgame_terms_text": CANONICAL_ENDGAME_TERMS_TEXT,
        "attribution_min_samples": 30,
        "version_request_page_limit": 5,
        "limit": 200,
        "fetch_all_pages": False,
        "timeout_seconds": 30,
        "manual_stopwords_text": "",
    }
    try:
        if UI_MEMORY_PATH.exists():
            loaded = json.loads(UI_MEMORY_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                defaults.update(loaded)
        if is_legacy_endgame_terms_text(str(defaults.get("endgame_terms_text", ""))):
            defaults["endgame_terms_text"] = CANONICAL_ENDGAME_TERMS_TEXT
    except (json.JSONDecodeError, OSError):
        pass
    return defaults


def save_ui_memory(memory: dict[str, object]) -> None:
    UI_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    UI_MEMORY_PATH.write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8")


def suggest_noise_terms(counters: list[Counter], top_n: int = 20) -> list[str]:
    """Suggest likely noisy terms that appear in multiple groups with high volume."""
    coverage: dict[str, int] = {}
    total_freq: dict[str, int] = {}

    for counter in counters:
        for token, freq in counter.items():
            total_freq[token] = total_freq.get(token, 0) + int(freq)
        for token in counter.keys():
            coverage[token] = coverage.get(token, 0) + 1

    candidates = [
        token
        for token, cov in coverage.items()
        if cov >= 3 and len(token) >= 2
    ]
    candidates.sort(key=lambda t: (coverage[t], total_freq[t]), reverse=True)
    return candidates[:top_n]


def parse_date_ranges(text: str) -> list[tuple[date, date, str]]:
    ranges: list[tuple[date, date, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        normalized = line.replace("~", ",").replace("至", ",")
        parts = [p.strip() for p in normalized.split(",") if p.strip()]
        if len(parts) != 2:
            continue
        try:
            start = datetime.strptime(parts[0], "%Y-%m-%d").date()
            end = datetime.strptime(parts[1], "%Y-%m-%d").date()
        except ValueError:
            continue
        if start > end:
            start, end = end, start
        ranges.append((start, end, f"{start} ~ {end}"))
    return ranges


def parse_version_periods(text: str) -> list[tuple[str, date, date]]:
    """Parse lines in format: 版本名,YYYY-MM-DD,YYYY-MM-DD."""
    periods: list[tuple[str, date, date]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"[,，]", line) if p.strip()]
        if len(parts) < 3:
            continue
        version_name = parts[0]
        try:
            start = datetime.strptime(parts[1], "%Y-%m-%d").date()
            end = datetime.strptime(parts[2], "%Y-%m-%d").date()
        except ValueError:
            continue
        if start > end:
            start, end = end, start
        periods.append((version_name, start, end))
    return periods


def build_rolling_ranges(
    anchor_end: date,
    window_days: int,
    window_count: int,
    include_anchor_window: bool,
) -> list[tuple[date, date, str]]:
    ranges: list[tuple[date, date, str]] = []
    cursor_end = anchor_end if include_anchor_window else anchor_end - timedelta(days=window_days)
    for _ in range(window_count):
        start = cursor_end - timedelta(days=window_days - 1)
        end = cursor_end
        ranges.append((start, end, f"{start} ~ {end}"))
        cursor_end = start - timedelta(days=1)
    ranges.reverse()
    return ranges


def dedupe_reviews(reviews: list[Review]) -> list[Review]:
    seen: set[tuple[int, str, str, str]] = set()
    unique: list[Review] = []
    for review in reviews:
        key = (
            review.rating,
            review.content.strip(),
            (review.version or "").strip(),
            (review.review_date or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(review)
    return unique


def parse_review_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def contains_any_term(text: str, terms: set[str]) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in terms)


def compute_version_metrics(
    reviews: list[Review],
    endgame_terms: set[str],
    min_samples: int,
    version_periods: list[tuple[str, date, date]] | None = None,
) -> list[dict[str, float | str | int]]:
    grouped: dict[str, list[Review]] = defaultdict(list)
    normalized_periods = version_periods or []

    for review in reviews:
        version = ""
        review_dt = parse_review_datetime(review.review_date)
        if review_dt is not None:
            review_day = review_dt.date()
            for version_name, start, end in normalized_periods:
                if start <= review_day <= end:
                    version = version_name
                    break

        if not version:
            version = (review.version or "未知版本").strip() or "未知版本"

        grouped[version].append(review)

    metrics: list[dict[str, float | str | int]] = []
    for version, items in grouped.items():
        sample_count = len(items)
        five_star_count = sum(1 for r in items if r.rating == 5)
        one_star_count = sum(1 for r in items if r.rating == 1)
        related_items = [r for r in items if contains_any_term(r.content, endgame_terms)]
        related_count = len(related_items)

        if related_count < min_samples:
            continue

        latest_dt: datetime | None = None
        for item in items:
            dt = parse_review_datetime(item.review_date)
            if dt is not None and (latest_dt is None or dt > latest_dt):
                latest_dt = dt

        metrics.append(
            {
                "版本": version,
                "样本量": sample_count,
                "养成相关样本量": related_count,
                "均分": round(sum(r.rating for r in items) / sample_count, 3),
                "5分占比": round(five_star_count / sample_count, 3),
                "1分占比": round(one_star_count / sample_count, 3),
                "最近评论日": latest_dt.strftime("%Y-%m-%d") if latest_dt else "未知",
                "_sort_ts": latest_dt.timestamp() if latest_dt else 0.0,
            }
        )

    metrics.sort(key=lambda row: float(row.get("_sort_ts", 0.0)))
    for row in metrics:
        row.pop("_sort_ts", None)
    return metrics


def compute_attribution_changes(version_metrics: list[dict[str, float | str | int]]) -> list[dict[str, float | str]]:
    rows: list[dict[str, float | str]] = []
    for i in range(1, len(version_metrics)):
        prev = version_metrics[i - 1]
        curr = version_metrics[i]
        delta_rating = float(curr["均分"]) - float(prev["均分"])
        delta_horizontal = float(curr["评论高低分净差（横比）"]) - float(prev["评论高低分净差（横比）"])
        delta_related_samples = int(curr["养成相关样本量"]) - int(prev["养成相关样本量"])

        if delta_horizontal > 0 and delta_related_samples > 0:
            if delta_rating > 0:
                conclusion = "养成相关高低分净差与样本量同步提升，且整体评分同步改善"
            elif delta_rating < 0:
                conclusion = "养成相关高低分净差与样本量同步提升，但整体评分未同步改善"
            else:
                conclusion = "养成相关高低分净差与样本量同步提升，但整体评分持平"
        elif delta_horizontal > 0:
            conclusion = "养成相关高低分净差提升，但样本量未同步增强"
        elif delta_related_samples > 0:
            conclusion = "养成相关样本量提升，但高低分净差未同步增强"
        else:
            conclusion = "养成相关反馈未明显增强，需结合改动日志复核"

        rows.append(
            {
                "版本变化": f"{prev['版本']} -> {curr['版本']}",
                "评分变化": round(delta_rating, 3),
                "养成相关样本量变化": delta_related_samples,
                "评论高低分净差（横比）变化": round(delta_horizontal, 3),
                "判定": conclusion,
            }
        )
    return rows


def compute_endgame_signal_strength(
    reviews: list[Review],
    endgame_terms: set[str],
    min_samples: int,
    version_periods: list[tuple[str, date, date]] | None = None,
) -> list[dict[str, float | str | int]]:
    """养成末端强度：
    - 横比：养成相关样本内 5星数-1星数
    - 纵比：版本前后横比差值
    - 养成影响指数：综合养成相关占比、横比、纵比，归一化到0~100
    """

    grouped: dict[str, list[Review]] = defaultdict(list)
    normalized_periods = version_periods or []
    related_terms = {term.lower() for term in endgame_terms if term and term.strip()}

    for review in reviews:
        version = ""
        review_dt = parse_review_datetime(review.review_date)
        if review_dt is not None:
            review_day = review_dt.date()
            for version_name, start, end in normalized_periods:
                if start <= review_day <= end:
                    version = version_name
                    break

        if not version:
            version = (review.version or "未知版本").strip() or "未知版本"

        grouped[version].append(review)

    base_rows: list[dict[str, float | str | int]] = []
    for version, items in grouped.items():
        sample_count = len(items)
        related = [r for r in items if contains_any_term(r.content, related_terms)]
        related_count = len(related)
        if related_count < min_samples:
            continue

        related_share = (related_count / sample_count) if sample_count else 0.0
        related_five_count = sum(1 for r in related if r.rating == 5)
        related_one_count = sum(1 for r in related if r.rating == 1)
        net_diff_horizontal = related_five_count - related_one_count

        latest_dt: datetime | None = None
        for item in items:
            dt = parse_review_datetime(item.review_date)
            if dt is not None and (latest_dt is None or dt > latest_dt):
                latest_dt = dt

        base_rows.append(
            {
                "版本": version,
                "样本量": sample_count,
                "养成相关评论占比值": related_share,
                "养成相关样本量": related_count,
                "评论高低分净差（横比）": net_diff_horizontal,
                "_sort_ts": latest_dt.timestamp() if latest_dt else 0.0,
            }
        )

    if not base_rows:
        return []

    base_rows.sort(key=lambda row: float(row.get("_sort_ts", 0.0)))

    vertical_values: list[int] = [0]
    for i in range(1, len(base_rows)):
        prev_h = int(base_rows[i - 1]["评论高低分净差（横比）"])
        curr_h = int(base_rows[i]["评论高低分净差（横比）"])
        vertical_values.append(curr_h - prev_h)

    share_values = [float(r["养成相关评论占比值"]) for r in base_rows]
    horizontal_values = [float(r["评论高低分净差（横比）"]) for r in base_rows]
    vertical_float_values = [float(v) for v in vertical_values]

    def min_max_norm(values: list[float], value: float) -> float:
        v_min = min(values)
        v_max = max(values)
        if v_max <= v_min:
            return 0.5
        return (value - v_min) / (v_max - v_min)

    rows: list[dict[str, float | str | int]] = []
    for idx, row in enumerate(base_rows):
        share_n = min_max_norm(share_values, float(row["养成相关评论占比值"]))
        horizontal_n = min_max_norm(horizontal_values, float(row["评论高低分净差（横比）"]))
        vertical_n = min_max_norm(vertical_float_values, float(vertical_values[idx]))

        impact_index = 100.0 * (0.4 * share_n + 0.35 * horizontal_n + 0.25 * vertical_n)

        rows.append(
            {
                "版本": row["版本"],
                "样本量": row["样本量"],
                "养成相关评论占比": f"{float(row['养成相关评论占比值']) * 100:.1f}%",
                "养成相关样本量": row["养成相关样本量"],
                "评论高低分净差（横比）": row["评论高低分净差（横比）"],
                "养成驱动分（纵比）": vertical_values[idx],
                "养成影响指数": round(max(0.0, min(100.0, impact_index)), 1),
            }
        )

    return rows


def compute_endgame_explanatory_power(version_metrics: list[dict[str, float | str | int]]) -> list[dict[str, float | str]]:
    """Layer 2: 养成信号对整体评分变化的解释力（按相邻版本变化衡量）。"""
    rows: list[dict[str, float | str]] = []
    for i in range(1, len(version_metrics)):
        prev = version_metrics[i - 1]
        curr = version_metrics[i]

        delta_rating = float(curr["均分"]) - float(prev["均分"])
        delta_endgame = float(curr["养成净好评"]) - float(prev["养成净好评"])
        delta_focus = float(curr["养成相关评论占比"]) - float(prev["养成相关评论占比"])

        driver_score = 0.7 * delta_endgame + 0.3 * delta_focus
        rating_base = abs(delta_rating)

        if rating_base < 0.03:
            power = 0.0
            conclusion = "整体评分波动很小，解释力不显著"
        else:
            power = max(0.0, min(1.0, abs(driver_score) / rating_base))
            same_direction = (delta_rating > 0 and driver_score > 0) or (delta_rating < 0 and driver_score < 0)
            if power >= 0.7 and same_direction:
                conclusion = "解释力强"
            elif power >= 0.35 and same_direction:
                conclusion = "解释力中等"
            elif same_direction:
                conclusion = "解释力较弱"
            else:
                conclusion = "方向不一致，解释力不足"

        rows.append(
            {
                "版本变化": f"{prev['版本']} -> {curr['版本']}",
                "评分变化": round(delta_rating, 3),
                "养成驱动分": round(driver_score, 3),
                "解释力指数": round(power, 3),
                "解释力判定": conclusion,
            }
        )
    return rows


def compute_endgame_focus_ratio(semantic_rows: list[dict[str, str]]) -> tuple[int, int, float]:
    total = len(semantic_rows)
    if total == 0:
        return 0, 0, 0.0
    # 与展示文案保持一致：按“命中关键词”是否非空来统计命中条数。
    focused = sum(1 for row in semantic_rows if row.get("命中关键词", "").strip())
    ratio = focused / total
    return focused, total, ratio


def write_semantic_csv(rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["平台", "评分", "评论时间", "归属版本", "原始评论", "精简评论", "养成方向的精简评论", "命中关键词"],
        )
        writer.writeheader()
        writer.writerows(rows)


def write_raw_reviews_csv(rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["平台", "评分", "评论时间", "归属版本", "原始评论"],
        )
        writer.writeheader()
        writer.writerows(rows)


def _decode_uploaded_csv_text(uploaded_file) -> str:
    raw_bytes = uploaded_file.getvalue()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8-sig", errors="ignore")


def _normalize_csv_header(value: str) -> str:
    return re.sub(r"\s+", "", (value or "")).lower()


def _pick_csv_value(row: dict[str, str], candidates: list[str]) -> str:
    normalized_map = {_normalize_csv_header(key): key for key in row.keys() if key}
    for candidate in candidates:
        actual_key = normalized_map.get(candidate)
        if actual_key is None:
            continue
        value = str(row.get(actual_key, "")).strip()
        if value:
            return value
    return ""


def _normalize_platform_label(value: str) -> str:
    lowered = (value or "").strip().lower()
    if not lowered:
        return ""
    if any(token in lowered for token in ["android", "google", "gp", "play"]):
        return "Android"
    if any(token in lowered for token in ["ios", "apple", "app store", "iphone", "ipad"]):
        return "iOS"
    return ""


def _parse_review_rating(value: str) -> int:
    text = (value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def load_reviews_from_csv(uploaded_file) -> tuple[list[Review], list[Review], dict[str, object]]:
    text = _decode_uploaded_csv_text(uploaded_file)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV 缺少表头，无法解析。")

    ios_reviews: list[Review] = []
    android_reviews: list[Review] = []
    row_count = 0
    has_rating_column = False
    min_review_dt: datetime | None = None
    max_review_dt: datetime | None = None

    for row in reader:
        if not isinstance(row, dict):
            continue
        row_count += 1

        content = _pick_csv_value(
            row,
            ["原始评论", "content", "text", "review", "comment", "body", "review_text", "message", "评论"],
        )
        if not content:
            continue

        rating_text = _pick_csv_value(
            row,
            ["评分", "rating", "stars", "score", "star", "star_rating", "rating_value"],
        )
        if rating_text:
            has_rating_column = True
        rating = _parse_review_rating(rating_text)

        review_date = _pick_csv_value(
            row,
            ["评论时间", "review_date", "date", "created_at", "updated_at", "createdat", "published_at", "timestamp"],
        )
        version = _pick_csv_value(
            row,
            ["归属版本", "version", "app_version", "appversion", "build_version", "buildversion"],
        )
        platform = _normalize_platform_label(
            _pick_csv_value(row, ["平台", "platform", "store", "渠道", "来源"])
        ) or "iOS"

        review = Review(
            rating=rating,
            content=content,
            version=version or None,
            review_date=review_date or None,
        )

        parsed_dt = parse_review_datetime(review.review_date)
        if parsed_dt is not None:
            if min_review_dt is None or parsed_dt < min_review_dt:
                min_review_dt = parsed_dt
            if max_review_dt is None or parsed_dt > max_review_dt:
                max_review_dt = parsed_dt

        if platform == "Android":
            android_reviews.append(review)
        else:
            ios_reviews.append(review)

    if not ios_reviews and not android_reviews:
        raise ValueError("CSV 中没有解析到有效评论内容，请确认列名是否包含‘原始评论/内容/评论’。")

    meta: dict[str, object] = {
        "row_count": row_count,
        "has_rating_column": has_rating_column,
        "date_range": (min_review_dt, max_review_dt),
        "source_name": getattr(uploaded_file, "name", "uploaded.csv"),
    }
    return ios_reviews, android_reviews, meta


def build_analysis_data(
    *,
    app_name: str,
    android_package: str,
    query_mode: str,
    search_terms: str,
    font_path: str,
    period_labels: list[str],
    batch_mode: bool,
    version_periods: list[tuple[str, date, date]],
    ios_reviews: list[Review],
    android_reviews: list[Review],
    endgame_terms_text: str,
    attribution_min_samples: int,
    version_request_page_limit: int,
    ios_raw_reviews: list[Review] | None = None,
    android_raw_reviews: list[Review] | None = None,
    ios_api_calls: int = 0,
    android_api_calls: int = 0,
    ios_stop_reasons: list[str] | None = None,
    android_stop_reasons: list[str] | None = None,
    source_label: str = "SST API",
) -> dict[str, object]:
    endgame_terms = parse_endgame_keywords(endgame_terms_text)
    english_endgame_terms = sorted([k for k in endgame_terms if re.search(r"[A-Za-z]", k)])

    raw_ios_reviews = list(ios_raw_reviews or ios_reviews)
    raw_android_reviews = list(android_raw_reviews or android_reviews)
    ios_raw_total = len(raw_ios_reviews)
    android_raw_total = len(raw_android_reviews)

    ios_reviews = dedupe_reviews(ios_reviews)
    android_reviews = dedupe_reviews(android_reviews)

    ios_endgame_reviews = [r for r in ios_reviews if contains_any_term(r.content, endgame_terms)]
    android_endgame_reviews = [r for r in android_reviews if contains_any_term(r.content, endgame_terms)]

    ios_high_texts, ios_low_texts = split_reviews_by_rating(ios_reviews)
    android_high_texts, android_low_texts = split_reviews_by_rating(android_reviews)
    ios_endgame_high_texts, ios_endgame_low_texts = split_reviews_by_rating(ios_endgame_reviews)
    android_endgame_high_texts, android_endgame_low_texts = split_reviews_by_rating(android_endgame_reviews)

    semantic_rows = build_semantic_rows(ios_reviews, endgame_keywords=endgame_terms, platform="iOS")
    semantic_rows.extend(build_semantic_rows(android_reviews, endgame_keywords=endgame_terms, platform="Android"))

    semantic_csv_path = Path("outputs_ui_latest") / "reviews_semantic.csv"
    write_semantic_csv(semantic_rows, semantic_csv_path)

    raw_rows: list[dict[str, str]] = []
    for r in raw_ios_reviews:
        raw_rows.append(
            {
                "平台": "iOS",
                "评分": str(r.rating),
                "评论时间": (r.review_date or "").strip() or "未知",
                "归属版本": (r.version or "").strip() or "未知版本",
                "原始评论": (r.content or "").strip(),
            }
        )
    for r in raw_android_reviews:
        raw_rows.append(
            {
                "平台": "Android",
                "评分": str(r.rating),
                "评论时间": (r.review_date or "").strip() or "未知",
                "归属版本": (r.version or "").strip() or "未知版本",
                "原始评论": (r.content or "").strip(),
            }
        )
    raw_csv_path = Path("outputs_ui_latest") / "reviews_raw_before_dedupe.csv"
    write_raw_reviews_csv(raw_rows, raw_csv_path)

    return {
        "app_name": app_name,
        "android_package": android_package,
        "query_mode": query_mode,
        "search_terms": search_terms,
        "font_path": font_path,
        "period_labels": period_labels,
        "batch_mode": batch_mode,
        "version_periods": version_periods,
        "ios_reviews": ios_reviews,
        "android_reviews": android_reviews,
        "endgame_terms": endgame_terms,
        "english_endgame_terms": english_endgame_terms,
        "attribution_min_samples": attribution_min_samples,
        "version_request_page_limit": version_request_page_limit,
        "semantic_rows": semantic_rows,
        "semantic_csv_path": str(semantic_csv_path),
        "raw_csv_path": str(raw_csv_path),
        "ios_total": len(ios_reviews),
        "android_total": len(android_reviews),
        "ios_raw_total": ios_raw_total,
        "android_raw_total": android_raw_total,
        "ios_api_calls": ios_api_calls,
        "android_api_calls": android_api_calls,
        "ios_stop_reasons": ios_stop_reasons or [],
        "android_stop_reasons": android_stop_reasons or [],
        "ios_high_texts": ios_high_texts,
        "ios_low_texts": ios_low_texts,
        "android_high_texts": android_high_texts,
        "android_low_texts": android_low_texts,
        "ios_endgame_high_texts": ios_endgame_high_texts,
        "ios_endgame_low_texts": ios_endgame_low_texts,
        "android_endgame_high_texts": android_endgame_high_texts,
        "android_endgame_low_texts": android_endgame_low_texts,
        "analysis_source": source_label,
        "has_rating_column": True,
    }


if "manual_stopwords" not in st.session_state:
    st.session_state["manual_stopwords"] = set()
if "ui_memory" not in st.session_state:
    st.session_state["ui_memory"] = load_ui_memory()

ui_memory: dict[str, object] = st.session_state["ui_memory"]
if is_legacy_endgame_terms_text(str(ui_memory.get("endgame_terms_text", ""))):
    ui_memory["endgame_terms_text"] = CANONICAL_ENDGAME_TERMS_TEXT
    st.session_state["ui_memory"] = ui_memory
    save_ui_memory(ui_memory)
if not st.session_state["manual_stopwords"] and str(ui_memory.get("manual_stopwords_text", "")).strip():
    st.session_state["manual_stopwords"] = parse_stopwords(str(ui_memory.get("manual_stopwords_text", "")))

# 页面配置
st.set_page_config(
    page_title="SST App Review Analyzer",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.title("🔍 App Review Word Cloud Analyzer")
st.markdown("查询 iOS App Store 和 Google Play 评论，生成高低评论词云")

# ==================== 侧边栏配置 ====================
with st.sidebar:
    st.header("⚙️ 配置信息")

    if "network_diag" not in st.session_state:
        st.session_state["network_diag"] = None
    
    # API Key
    api_key = st.text_input(
        "🔑 SensorTower API Key",
        type="password",
        placeholder="输入你的 auth_token",
        help="从 SensorTower 开发者中心获取",
        key="sst_api_key_input",
    )
    api_base_url = API_BASE_URL

    query_mode = st.radio(
        "查询模式",
        options=["单平台查询", "双平台查询"],
        index=0 if str(ui_memory.get("query_mode", "单平台查询")) != "双平台查询" else 1,
        horizontal=True,
    )

    data_source_mode = st.radio(
        "数据来源",
        options=["SST API 抓取", "导入CSV分析"],
        index=0 if str(ui_memory.get("data_source_mode", "SST API 抓取")) != "导入CSV分析" else 1,
        horizontal=True,
        help="导入CSV后将直接分析本地文件，不再请求SST API。",
    )

    csv_review_file = None
    if data_source_mode == "导入CSV分析":
        csv_review_file = st.file_uploader(
            "上传评论CSV",
            type=["csv"],
            help="建议上传本工具导出的原评论CSV；需至少包含‘原始评论’列，最好同时包含‘评分、评论时间、归属版本、平台’。",
            key="csv_review_file",
        )
        st.caption("导入CSV后不会访问API，只会直接做版本归因和语义分析。")

    if st.button("🩺 一键网络诊断", use_container_width=True):
        host = parse_base_url_host(api_base_url) or "api.sensortower.com"
        dns_ok, dns_error = dns_precheck(host)
        tcp_ok, tcp_error = (False, "")
        https_ok, https_error, https_status = (False, "", None)

        if dns_ok:
            tcp_ok, tcp_error = tcp_precheck(host, port=443, timeout=6.0)
        if dns_ok and tcp_ok:
            https_ok, https_error, https_status = https_precheck(api_base_url, timeout=8.0)

        st.session_state["network_diag"] = {
            "host": host,
            "dns_ok": dns_ok,
            "dns_error": dns_error,
            "tcp_ok": tcp_ok,
            "tcp_error": tcp_error,
            "https_ok": https_ok,
            "https_error": https_error,
            "https_status": https_status,
        }

    diag = st.session_state.get("network_diag")
    if isinstance(diag, dict):
        st.caption("网络诊断结果")
        st.write(f"目标主机: {diag.get('host', '-')}")
        st.write(f"DNS 解析: {'✅' if diag['dns_ok'] else '❌'}")
        st.write(f"TCP 443: {'✅' if diag['tcp_ok'] else '❌'}")
        if diag["https_status"] is not None:
            st.write(f"HTTPS 访问: {'✅' if diag['https_ok'] else '❌'} (status={diag['https_status']})")
        else:
            st.write(f"HTTPS 访问: {'✅' if diag['https_ok'] else '❌'}")

        if not diag["dns_ok"]:
            st.caption(f"DNS 错误: {diag['dns_error']}")
        elif not diag["tcp_ok"]:
            st.caption(f"TCP 错误: {diag['tcp_error']}")
        elif not diag["https_ok"]:
            st.caption(f"HTTPS 错误: {diag['https_error']}")

    st.divider()
    
    st.divider()
    
    # 应用信息
    st.subheader("📱 应用信息")
    
    ios_app_id = st.text_input(
        "iOS App ID (数字)",
        value=str(ui_memory.get("ios_app_id", "6474233312")),
        placeholder="如 6474233312",
        key="ios_app_id_input",
    )
    
    if query_mode == "双平台查询":
        android_package = st.text_input(
            "Android Package ID",
            value=str(ui_memory.get("android_package", "com.moonshot.kimichat")),
            placeholder="如 com.moonshot.kimichat",
            key="android_package_input",
        )
    else:
        android_package = ""
        st.caption("当前为单平台查询，仅使用 iOS App Store 数据。")
    
    st.divider()
    
    # 查询参数
    st.subheader("📅 查询参数")
    
    # 日期范围
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input(
            "开始日期",
            value=parse_saved_date(
                str(ui_memory.get("start_date", "")),
                (datetime.now() - timedelta(days=30)).date(),
            ),
        )
    with col2:
        end_date = st.date_input(
            "结束日期",
            value=parse_saved_date(
                str(ui_memory.get("end_date", "")),
                datetime.now().date(),
            ),
        )

    enable_batch_periods = st.checkbox(
        "启用时间段批量选择",
        value=bool(ui_memory.get("enable_batch_periods", False)),
        help="一次拉取多个连续时间窗口，便于观察版本前后评论变化"
    )

    batch_ranges: list[tuple[date, date, str]] = []
    batch_mode = str(ui_memory.get("batch_mode", "连续周期"))
    batch_window_days = int(ui_memory.get("batch_window_days", 7))
    batch_count = int(ui_memory.get("batch_count", 4))
    include_latest_cycle = bool(ui_memory.get("include_latest_cycle", True))
    manual_ranges = str(ui_memory.get("manual_ranges", ""))
    if enable_batch_periods:
        batch_mode = st.radio(
            "批量模式",
            options=["连续周期", "手动区间列表"],
            index=0 if batch_mode != "手动区间列表" else 1,
            horizontal=True,
        )
        if batch_mode == "连续周期":
            col_b1, col_b2 = st.columns(2)
            with col_b1:
                window_options = [7, 14, 30]
                default_window_idx = window_options.index(batch_window_days) if batch_window_days in window_options else 0
                batch_window_days = st.selectbox("单周期天数", options=window_options, index=default_window_idx)
            with col_b2:
                batch_count = st.slider("周期数量", min_value=2, max_value=8, value=max(2, min(8, batch_count)))
            include_latest_cycle = st.checkbox("包含以结束日期为锚点的最新周期", value=include_latest_cycle)
            batch_ranges = build_rolling_ranges(
                anchor_end=end_date,
                window_days=batch_window_days,
                window_count=batch_count,
                include_anchor_window=include_latest_cycle,
            )
            st.caption("将查询区间：" + " | ".join(label for _, _, label in batch_ranges))
        else:
            manual_ranges = st.text_area(
                "手动输入区间（每行一个，格式 YYYY-MM-DD,YYYY-MM-DD）",
                value=manual_ranges,
                height=120,
                placeholder="2026-01-01,2026-01-14\n2026-01-15,2026-01-28",
            )
            batch_ranges = parse_date_ranges(manual_ranges)
            st.caption(f"已识别有效区间数: {len(batch_ranges)}")

    st.subheader("🧩 版本时间映射")
    version_periods_text = st.text_area(
        "每行: 版本名,开始日期,结束日期",
        value=str(ui_memory.get("version_periods_text", "")),
        height=110,
        placeholder="3.0,2026-03-01,2026-03-20\n3.1,2026-03-21,2026-04-10",
        help="用于修正版本对照时间，优先于评论自带版本号",
    )
    parsed_version_periods = parse_version_periods(version_periods_text)
    st.caption(f"已识别版本区间: {len(parsed_version_periods)}")

    st.subheader("🎯 版本归因关键词")
    endgame_terms_text = st.text_area(
        "养成末端关键词（逗号/空格/换行分隔）",
        value=str(ui_memory.get("endgame_terms_text", "")),
        height=90,
        key="endgame_terms_input",
    )
    attribution_min_samples = st.slider(
        "养成相关版本最小样本量",
        min_value=10,
        max_value=200,
        value=max(10, min(200, int(ui_memory.get("attribution_min_samples", 30)))),
        step=10,
        help="按养成相关评论数判断；低于该值的版本将被跳过"
    )
    version_request_page_limit = st.slider(
        "单版本API请求页数上限",
        min_value=1,
        max_value=20,
        value=max(1, min(20, int(ui_memory.get("version_request_page_limit", 5)))),
        step=1,
        help="每个版本的查询最多翻这么多页，避免过度消耗API"
    )
    
    st.divider()
    
    # 其他选项
    st.subheader("🔧 其他选项")
    limit = 200
    st.caption("单页记录数固定为 200（不可调整）")
    
    fetch_all_pages = st.checkbox(
        "自动获取全部页面",
        value=bool(ui_memory.get("fetch_all_pages", False)),
        help="启用后会自动分页查询所有数据（较慢）"
    )

    timeout_seconds = st.slider(
        "API 超时秒数",
        min_value=10,
        max_value=120,
        value=max(10, min(120, int(ui_memory.get("timeout_seconds", 30)))),
        step=5,
        help="网络较慢或跨境链路不稳定时可适当调大"
    )
    st.caption("当前版本已关闭自动翻译，按评论原文进行语义提炼。")

    st.divider()
    st.subheader("🧹 噪声词过滤")
    manual_stopwords_text = st.text_area(
        "手工停用词（逗号/空格/换行分隔）",
        value=", ".join(sorted(st.session_state["manual_stopwords"])),
        height=90,
        placeholder="如：体验,真的,非常,kimi"
    )
    st.session_state["manual_stopwords"] = parse_stopwords(manual_stopwords_text)

    noise_top_n = st.slider(
        "自动建议噪声词数量",
        min_value=5,
        max_value=40,
        value=20,
        step=5,
    )

# ==================== 主面板 ====================
app_name = f"App_{ios_app_id.strip() or 'Unknown'}"
search_terms = ""
endgame_terms = parse_endgame_keywords(endgame_terms_text)

col1, col2 = st.columns(2)
with col1:
    st.metric("iOS App ID", ios_app_id)
with col2:
    st.metric("查询模式", query_mode)

st.divider()

# 查询按钮
if st.button("🚀 开始查询", use_container_width=True, type="primary"):
    raw_api_key_input = str(st.session_state.get("sst_api_key_input", api_key or ""))
    extracted_api_key = extract_api_key_from_input(raw_api_key_input)
    api_key = extracted_api_key or ""

    latest_ui_memory: dict[str, object] = {
        "query_mode": query_mode,
        "data_source_mode": data_source_mode,
        "ios_app_id": ios_app_id,
        "android_package": android_package,
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": end_date.strftime("%Y-%m-%d"),
        "enable_batch_periods": enable_batch_periods,
        "batch_mode": batch_mode,
        "batch_window_days": batch_window_days,
        "batch_count": batch_count,
        "include_latest_cycle": include_latest_cycle,
        "manual_ranges": manual_ranges,
        "version_periods_text": version_periods_text,
        "endgame_terms_text": endgame_terms_text,
        "attribution_min_samples": attribution_min_samples,
        "version_request_page_limit": version_request_page_limit,
        "limit": 200,
        "fetch_all_pages": fetch_all_pages,
        "timeout_seconds": timeout_seconds,
        "manual_stopwords_text": manual_stopwords_text,
    }
    save_ui_memory(latest_ui_memory)
    st.session_state["ui_memory"] = latest_ui_memory

    if data_source_mode == "导入CSV分析":
        if csv_review_file is None:
            st.error("❌ 已选择导入CSV分析，请先上传CSV文件。")
            st.stop()

        progress_container = st.container()
        with progress_container:
            progress_bar = st.progress(0)
            status_text = st.empty()

            try:
                status_text.text("⏳ 正在解析CSV...")
                progress_bar.progress(10)

                ios_csv_reviews, android_csv_reviews, csv_meta = load_reviews_from_csv(csv_review_file)
                source_name = str(csv_meta.get("source_name", csv_review_file.name))
                date_range = csv_meta.get("date_range")
                period_labels: list[str]
                if isinstance(date_range, tuple) and len(date_range) == 2 and date_range[0] and date_range[1]:
                    start_dt, end_dt = date_range
                    period_labels = [f"{start_dt.date()} ~ {end_dt.date()}"]
                else:
                    period_labels = [Path(source_name).stem or source_name]

                analysis_query_mode = "双平台查询" if ios_csv_reviews and android_csv_reviews else "单平台查询"
                analysis_data = build_analysis_data(
                    app_name=f"CSV_{Path(source_name).stem or 'Import'}",
                    android_package="",
                    query_mode=analysis_query_mode,
                    search_terms="",
                    font_path="C:\\Windows\\Fonts\\msyh.ttc",
                    period_labels=period_labels,
                    batch_mode=False,
                    version_periods=parsed_version_periods,
                    ios_reviews=ios_csv_reviews,
                    android_reviews=android_csv_reviews,
                    endgame_terms_text=endgame_terms_text,
                    attribution_min_samples=attribution_min_samples,
                    version_request_page_limit=version_request_page_limit,
                    ios_raw_reviews=ios_csv_reviews,
                    android_raw_reviews=android_csv_reviews,
                    source_label="CSV导入",
                )
                analysis_data["has_rating_column"] = bool(csv_meta.get("has_rating_column", True))
                analysis_data["csv_source_name"] = source_name
                analysis_data["csv_row_count"] = int(csv_meta.get("row_count", 0) or 0)
                st.session_state["analysis_data"] = analysis_data

                progress_bar.progress(100)
                status_text.text("✅ CSV分析完成！")
                progress_container.empty()
                st.success("✨ CSV导入和分析完成。你现在可以直接调整阈值并重新看入表结果，无需再次请求API。")
                if not analysis_data["has_rating_column"]:
                    st.warning("导入CSV未检测到评分列，均分/5分占比/1分占比可能不准确。")
            except Exception as e:
                status_text.text("")
                progress_container.empty()
                st.error(f"❌ CSV分析失败: {str(e)}")
                import traceback
                st.code(redact_sensitive_text(traceback.format_exc()), language="python")

            st.rerun()
    
    if not api_key:
        st.error("❌ 请输入 API Key")
    elif extracted_api_key is None:
        st.error("❌ API Key 无法识别：请输入真实 SensorTower token（如 ST0_...）。")
    elif not ios_app_id:
        st.error("❌ 请输入 iOS App ID")
    elif query_mode == "双平台查询" and not android_package:
        st.error("❌ 双平台查询时请输入 Android Package")
    elif enable_batch_periods and not batch_ranges:
        st.error("❌ 批量模式已启用，但没有可用时间区间。请检查输入格式。")
    else:
        # 创建进度容器
        progress_container = st.container()
        
        with progress_container:
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            try:
                # 初始化配置
                status_text.text("⏳ 初始化配置...")
                progress_bar.progress(5)
                
                # 创建临时配置对象
                settings = Settings(
                    api_base_url=api_base_url,
                    api_key=api_key,
                    timeout_seconds=timeout_seconds,
                    verify_ssl=True,
                    fetch_all_pages=fetch_all_pages,
                    review_list_path="feedback",
                    review_text_field="content",
                    review_rating_field="rating",
                    chinese_font_path="C:\\Windows\\Fonts\\msyh.ttc"  # Windows 中文字体
                )
                
                client = SSTClient(settings)

                periods = batch_ranges if enable_batch_periods else [
                    (start_date, end_date, f"{start_date} ~ {end_date}")
                ]
                period_count = max(1, len(periods))
                ios_reviews: list[Review] = []
                android_reviews: list[Review] = []
                ios_api_calls = 0
                android_api_calls = 0
                ios_stop_reasons: list[str] = []
                android_stop_reasons: list[str] = []

                ios_regions = DEFAULT_IOS_COUNTRIES
                android_regions = DEFAULT_ANDROID_LANGUAGES
                is_multi_platform = query_mode == "双平台查询"
                qualifying_terms_list = sorted(endgame_terms)
                
                # ========== 批量区间查询 ==========
                for idx, (period_start, period_end, period_label) in enumerate(periods, start=1):
                    status_text.text(f"📲 查询 iOS 评论 ({idx}/{period_count}) {period_label} ...")
                    ios_req = SearchRequest(
                        app_id=ios_app_id,
                        store="apple",
                        countries=ios_regions,
                        start_date=period_start.strftime("%Y-%m-%d"),
                        end_date=period_end.strftime("%Y-%m-%d"),
                        rating_filters="1,2,3,4,5",
                        limit=limit,
                        min_qualifying_reviews=attribution_min_samples,
                        qualifying_terms=qualifying_terms_list,
                        max_pages=version_request_page_limit,
                    )
                    ios_reviews.extend(client.fetch_reviews(ios_req))
                    ios_api_calls += int(getattr(client, "last_fetch_api_calls", 0) or 0)
                    ios_stop_reasons.append(str(getattr(client, "last_fetch_stop_reason", "unknown") or "unknown"))

                    if is_multi_platform:
                        status_text.text(f"🤖 查询 Android 评论 ({idx}/{period_count}) {period_label} ...")
                        android_req = SearchRequest(
                            app_id=android_package,
                            store="google",
                            countries=android_regions,
                            start_date=period_start.strftime("%Y-%m-%d"),
                            end_date=period_end.strftime("%Y-%m-%d"),
                            rating_filters="1,2,3,4,5",
                            limit=limit,
                            min_qualifying_reviews=attribution_min_samples,
                            qualifying_terms=qualifying_terms_list,
                            max_pages=version_request_page_limit,
                        )
                        android_reviews.extend(client.fetch_reviews(android_req))
                        android_api_calls += int(getattr(client, "last_fetch_api_calls", 0) or 0)
                        android_stop_reasons.append(str(getattr(client, "last_fetch_stop_reason", "unknown") or "unknown"))

                    progress = 5 + int(50 * idx / period_count)
                    progress_bar.progress(min(progress, 55))

                ios_raw_reviews = list(ios_reviews)
                android_raw_reviews = list(android_reviews)
                ios_raw_total = len(ios_raw_reviews)
                android_raw_total = len(android_raw_reviews)
                ios_reviews = dedupe_reviews(ios_reviews)
                android_reviews = dedupe_reviews(android_reviews)
                progress_bar.progress(58)
                progress_bar.progress(75)
                
                # ========== 分析 ==========
                status_text.text("📊 正在分析词频...")
                progress_bar.progress(80)

                endgame_terms = parse_endgame_keywords(endgame_terms_text)
                english_endgame_terms = sorted([k for k in endgame_terms if re.search(r"[A-Za-z]", k)])

                ios_endgame_reviews = [r for r in ios_reviews if contains_any_term(r.content, endgame_terms)]
                android_endgame_reviews = [r for r in android_reviews if contains_any_term(r.content, endgame_terms)]

                ios_high_texts, ios_low_texts = split_reviews_by_rating(ios_reviews)
                android_high_texts, android_low_texts = split_reviews_by_rating(android_reviews)
                ios_endgame_high_texts, ios_endgame_low_texts = split_reviews_by_rating(ios_endgame_reviews)
                android_endgame_high_texts, android_endgame_low_texts = split_reviews_by_rating(android_endgame_reviews)

                semantic_rows = build_semantic_rows(
                    ios_reviews,
                    endgame_keywords=endgame_terms,
                    platform="iOS",
                )
                semantic_rows.extend(
                    build_semantic_rows(
                        android_reviews,
                        endgame_keywords=endgame_terms,
                        platform="Android",
                    )
                )

                semantic_csv_path = Path("outputs_ui_latest") / "reviews_semantic.csv"
                write_semantic_csv(semantic_rows, semantic_csv_path)
                raw_rows: list[dict[str, str]] = []
                for r in ios_raw_reviews:
                    raw_rows.append(
                        {
                            "平台": "iOS",
                            "评分": str(r.rating),
                            "评论时间": (r.review_date or "").strip() or "未知",
                            "归属版本": (r.version or "").strip() or "未知版本",
                            "原始评论": (r.content or "").strip(),
                        }
                    )
                for r in android_raw_reviews:
                    raw_rows.append(
                        {
                            "平台": "Android",
                            "评分": str(r.rating),
                            "评论时间": (r.review_date or "").strip() or "未知",
                            "归属版本": (r.version or "").strip() or "未知版本",
                            "原始评论": (r.content or "").strip(),
                        }
                    )
                raw_csv_path = Path("outputs_ui_latest") / "reviews_raw_before_dedupe.csv"
                write_raw_reviews_csv(raw_rows, raw_csv_path)

                # Cache processed data so users can iterate stopwords without re-fetching API data.
                st.session_state["analysis_data"] = {
                    "app_name": app_name,
                    "android_package": android_package,
                    "query_mode": query_mode,
                    "search_terms": search_terms,
                    "font_path": settings.chinese_font_path,
                    "period_labels": [label for _, _, label in periods],
                    "batch_mode": enable_batch_periods,
                    "version_periods": parsed_version_periods,
                    "ios_reviews": ios_reviews,
                    "android_reviews": android_reviews,
                    "endgame_terms": endgame_terms,
                    "english_endgame_terms": english_endgame_terms,
                    "attribution_min_samples": attribution_min_samples,
                    "version_request_page_limit": version_request_page_limit,
                    "semantic_rows": semantic_rows,
                    "semantic_csv_path": str(semantic_csv_path),
                    "raw_csv_path": str(raw_csv_path),
                    "ios_total": len(ios_reviews),
                    "android_total": len(android_reviews),
                    "ios_raw_total": ios_raw_total,
                    "android_raw_total": android_raw_total,
                    "ios_api_calls": ios_api_calls,
                    "android_api_calls": android_api_calls,
                    "ios_stop_reasons": ios_stop_reasons,
                    "android_stop_reasons": android_stop_reasons,
                    "ios_high_texts": ios_high_texts,
                    "ios_low_texts": ios_low_texts,
                    "android_high_texts": android_high_texts,
                    "android_low_texts": android_low_texts,
                    "ios_endgame_high_texts": ios_endgame_high_texts,
                    "ios_endgame_low_texts": ios_endgame_low_texts,
                    "android_endgame_high_texts": android_endgame_high_texts,
                    "android_endgame_low_texts": android_endgame_low_texts,
                }

                progress_bar.progress(100)
                status_text.text("✅ 查询完成！")
                
                # 清空进度显示
                progress_container.empty()
                st.success("✨ 查询和分析完成！你现在可以一键加入建议噪声词并重新生成词云（无需再次请求 API）。")
                
            except Exception as e:
                status_text.text("")
                progress_container.empty()
                st.error(f"❌ 查询失败: {str(e)}")
                import traceback
                st.code(redact_sensitive_text(traceback.format_exc()), language="python")

if "analysis_data" in st.session_state:
    data = st.session_state["analysis_data"]
    is_multi_platform = data.get("query_mode", "单平台查询") == "双平台查询"
    analysis_source = str(data.get("analysis_source", "SST API"))

    st.subheader("🗂 评论语义精简 CSV")
    if analysis_source == "CSV导入":
        st.caption(f"当前数据来源: CSV导入（{data.get('csv_source_name', 'uploaded.csv')}）")
    semantic_rows = data.get("semantic_rows", [])
    semantic_csv_path = Path(data.get("semantic_csv_path", "outputs_ui_latest/reviews_semantic.csv"))
    raw_csv_path = Path(data.get("raw_csv_path", "outputs_ui_latest/reviews_raw_before_dedupe.csv"))
    if semantic_rows:
        focused_count, total_count, focus_ratio = compute_endgame_focus_ratio(semantic_rows)
        st.metric("关注养成末端优化的评论比重", f"{focus_ratio * 100:.1f}%")
        st.caption(f"命中养成末端评论 {focused_count} / {total_count}（按原始评论关键词命中统计）")
        min_required = int(data.get("attribution_min_samples", 0) or 0)
        page_limit = int(data.get("version_request_page_limit", 0) or 0)
        ios_api_calls = int(data.get("ios_api_calls", 0) or 0)
        android_api_calls = int(data.get("android_api_calls", 0) or 0)
        ios_stop_reasons = data.get("ios_stop_reasons", [])
        android_stop_reasons = data.get("android_stop_reasons", [])
        ios_raw_total = int(data.get("ios_raw_total", 0) or 0)
        android_raw_total = int(data.get("android_raw_total", 0) or 0)
        ios_total = int(data.get("ios_total", 0) or 0)
        android_total = int(data.get("android_total", 0) or 0)
        if is_multi_platform:
            st.caption(f"本次实际API请求次数：iOS={ios_api_calls}，Android={android_api_calls}（上限/版本={page_limit}）")
            st.caption(
                f"抓取停止原因：iOS={','.join(ios_stop_reasons) if ios_stop_reasons else 'unknown'}；"
                f"Android={','.join(android_stop_reasons) if android_stop_reasons else 'unknown'}"
            )
            st.caption(
                "评论条数说明："
                f"iOS 去重前={ios_raw_total}，去重后={ios_total}；"
                f"Android 去重前={android_raw_total}，去重后={android_total}。"
                "单页limit=200是上限，不代表每页必返200。"
            )
        else:
            st.caption(f"本次实际API请求次数：iOS={ios_api_calls}（上限/版本={page_limit}）")
            st.caption(f"抓取停止原因：iOS={','.join(ios_stop_reasons) if ios_stop_reasons else 'unknown'}")
            st.caption(
                "评论条数说明："
                f"iOS 去重前={ios_raw_total}，去重后={ios_total}。"
                "单页limit=200是上限，不代表每页必返200。"
            )
        if min_required > 0 and focused_count < min_required:
            st.warning(
                f"当前关键词与查询范围下仅命中 {focused_count} 条，未达到目标 {min_required} 条。"
                f"本次单版本API请求页数上限={page_limit}。"
            )
        english_terms = data.get("english_endgame_terms", [])
        st.caption(
            "关键词匹配口径：完全基于“养成末端关键词”集合（含中英文）。"
            f" 当前英文关键词数={len(english_terms)}"
        )
        st.caption("分组标题（如【养成目标】）仅用于展示，不参与关键词匹配。")
        st.caption("导出字段：平台、评分、评论时间、归属版本、原始评论、精简评论、养成方向的精简评论、命中关键词。")
        st.dataframe(semantic_rows[:200], use_container_width=True)
        with semantic_csv_path.open("rb") as f:
            st.download_button(
                "⬇️ 下载语义精简 CSV",
                data=f,
                file_name=f"reviews_semantic_{data['app_name']}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with raw_csv_path.open("rb") as f:
            st.download_button(
                "⬇️ 下载原评论 CSV（去重前）",
                data=f,
                file_name=f"reviews_raw_before_dedupe_{data['app_name']}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        st.caption(f"CSV 文件位置: {semantic_csv_path}")
    else:
        st.info("暂无可导出的评论语义数据，请先执行查询。")

    st.divider()

    app_domain_stopwords = build_domain_stopwords(
        data["app_name"],
        data["android_package"],
        data.get("search_terms", data["app_name"]),
    )
    active_stopwords = app_domain_stopwords | st.session_state["manual_stopwords"]

    ios_high_freq = word_freq(data.get("ios_endgame_high_texts", []), extra_stopwords=active_stopwords)
    ios_low_freq = word_freq(data.get("ios_endgame_low_texts", []), extra_stopwords=active_stopwords)
    android_high_freq = word_freq(data.get("android_endgame_high_texts", []), extra_stopwords=active_stopwords)
    android_low_freq = word_freq(data.get("android_endgame_low_texts", []), extra_stopwords=active_stopwords)

    noise_candidates = suggest_noise_terms(
        [ios_high_freq, ios_low_freq, android_high_freq, android_low_freq],
        top_n=noise_top_n,
    )

    st.subheader("📈 查询统计")
    if is_multi_platform:
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("iOS 评论总数", data["ios_total"])
        with col2:
            st.metric("iOS 高评 (4-5)", len(data["ios_high_texts"]))
        with col3:
            st.metric("Android 评论总数", data["android_total"])
        with col4:
            st.metric("Android 高评 (4-5)", len(data["android_high_texts"]))
        st.caption(
            "词云样本口径：仅使用命中养成末端关键词的评论。"
            f" iOS={len(data.get('ios_endgame_high_texts', [])) + len(data.get('ios_endgame_low_texts', []))}"
            f" / Android={len(data.get('android_endgame_high_texts', [])) + len(data.get('android_endgame_low_texts', []))}"
        )
    else:
        col1, col2 = st.columns(2)
        with col1:
            st.metric("iOS 评论总数", data["ios_total"])
        with col2:
            st.metric("iOS 高评 (4-5)", len(data["ios_high_texts"]))
        st.caption(
            "词云样本口径：仅使用命中养成末端关键词的 iOS 评论。"
            f" iOS={len(data.get('ios_endgame_high_texts', [])) + len(data.get('ios_endgame_low_texts', []))}"
        )

    st.caption("当前按评论原文进行语义分析（已关闭自动翻译）。")

    st.divider()
    st.subheader("🧭 版本变化与养成末端归因")
    period_text = " | ".join(data.get("period_labels", []))
    if period_text:
        st.caption(f"分析时间段: {period_text}")

    endgame_terms = data.get("endgame_terms", set())
    min_samples = int(data.get("attribution_min_samples", 30))
    version_periods = data.get("version_periods", [])

    st.caption(
        "当前聚焦养成末端优化：版本是否入表取决于养成相关样本量是否达到阈值；"
        "强度表按养成相关样本量、评论高低分净差（横比）、版本前后高低分净差（纵比）共同计算。"
    )
    if version_periods:
        mapping_text = " | ".join([f"{name}: {start}~{end}" for name, start, end in version_periods])
        st.caption(f"已应用版本时间映射: {mapping_text}")
    else:
        st.caption("未配置版本时间映射，当前按评论自带版本号分组。")

    if is_multi_platform:
        version_view = st.radio(
            "版本归因视图",
            options=["合并（默认）", "仅 iOS", "仅 Android"],
            index=0,
            horizontal=True,
        )

        if version_view == "仅 iOS":
            selected_reviews = data.get("ios_reviews", [])
            st.caption("当前查看: iOS 单平台")
        elif version_view == "仅 Android":
            selected_reviews = data.get("android_reviews", [])
            st.caption("当前查看: Android 单平台")
        else:
            selected_reviews = data.get("ios_reviews", []) + data.get("android_reviews", [])
            st.caption("当前查看: iOS + Android 合并样本")
    else:
        ios_single = data.get("ios_reviews", [])
        android_single = data.get("android_reviews", [])
        if android_single and not ios_single:
            selected_reviews = android_single
            st.caption("当前查看: Android 单平台")
        else:
            selected_reviews = ios_single
            st.caption("当前查看: iOS 单平台（单平台查询模式）")

    combined_version_metrics = compute_version_metrics(
        selected_reviews,
        endgame_terms=endgame_terms,
        min_samples=0,
        version_periods=version_periods,
    )
    if combined_version_metrics:
        st.subheader("📊 基础版本指标")
        st.dataframe(combined_version_metrics, use_container_width=True)
        st.caption("本区不按养成相关样本量阈值过滤，版本均会入表显示。")

        st.subheader("🧱 养成末端正反馈强度")
        signal_strength_rows = compute_endgame_signal_strength(
            selected_reviews,
            endgame_terms=endgame_terms,
            min_samples=0,
            version_periods=version_periods,
        )
        st.dataframe(signal_strength_rows, use_container_width=True)
    else:
        st.info("当前没有可用于版本归因的评论数据。请先导入CSV或重新查询。")

    st.divider()
    st.subheader("🧪 噪声词建议")
    st.caption("这些词在 4 张词云中的至少 3 组都高频出现，常见于语气词/泛化词。")

    if noise_candidates:
        st.write("建议词:", "、".join(noise_candidates))
        col_auto, col_pick = st.columns(2)
        with col_auto:
            if st.button("⚡ 一键加入建议噪声词", use_container_width=True):
                st.session_state["manual_stopwords"] = st.session_state["manual_stopwords"] | set(noise_candidates)
                st.rerun()
        with col_pick:
            selected_noise = st.multiselect(
                "选择后加入停用词",
                options=noise_candidates,
                default=[],
            )
            if st.button("➕ 加入所选噪声词", use_container_width=True):
                st.session_state["manual_stopwords"] = st.session_state["manual_stopwords"] | set(selected_noise)
                st.rerun()
    else:
        st.info("当前没有明显噪声词建议。")

    st.caption(f"当前停用词数量（动态+手工）: {len(active_stopwords)}")

    st.divider()
    st.subheader("🎨 词云结果（仅养成末端相关评论）")

    output_dir = Path("outputs_ui_latest")
    output_dir.mkdir(exist_ok=True)
    ios_high_output = output_dir / "ios_high.png"
    ios_low_output = output_dir / "ios_low.png"
    android_high_output = output_dir / "android_high.png"
    android_low_output = output_dir / "android_low.png"

    generate_wordcloud(ios_high_freq, ios_high_output, font_path=data["font_path"])
    generate_wordcloud(ios_low_freq, ios_low_output, font_path=data["font_path"])
    if is_multi_platform:
        generate_wordcloud(android_high_freq, android_high_output, font_path=data["font_path"])
        generate_wordcloud(android_low_freq, android_low_output, font_path=data["font_path"])

    if is_multi_platform:
        tab1, tab2, tab3, tab4 = st.tabs([
            "📲 iOS - 高评论(4-5)",
            "📲 iOS - 低评论(1-2)",
            "🤖 Android - 高评论(4-5)",
            "🤖 Android - 低评论(1-2)",
        ])
    else:
        tab1, tab2 = st.tabs([
            "📲 iOS - 高评论(4-5)",
            "📲 iOS - 低评论(1-2)",
        ])

    from PIL import Image

    with tab1:
        img = Image.open(ios_high_output)
        st.image(img, caption=f"iOS 养成相关 4-5星评论 ({len(data.get('ios_endgame_high_texts', []))} 条)", use_column_width=True)
        with open(ios_high_output, "rb") as f:
            st.download_button("⬇️ 下载", f, f"ios_high_{data['app_name']}.png")

    with tab2:
        img = Image.open(ios_low_output)
        st.image(img, caption=f"iOS 养成相关 1-2星评论 ({len(data.get('ios_endgame_low_texts', []))} 条)", use_column_width=True)
        with open(ios_low_output, "rb") as f:
            st.download_button("⬇️ 下载", f, f"ios_low_{data['app_name']}.png")

    if is_multi_platform:
        with tab3:
            img = Image.open(android_high_output)
            st.image(img, caption=f"Android 养成相关 4-5星评论 ({len(data.get('android_endgame_high_texts', []))} 条)", use_column_width=True)
            with open(android_high_output, "rb") as f:
                st.download_button("⬇️ 下载", f, f"android_high_{data['app_name']}.png")

        with tab4:
            img = Image.open(android_low_output)
            st.image(img, caption=f"Android 养成相关 1-2星评论 ({len(data.get('android_endgame_low_texts', []))} 条)", use_column_width=True)
            with open(android_low_output, "rb") as f:
                st.download_button("⬇️ 下载", f, f"android_low_{data['app_name']}.png")

st.divider()

# 底部说明
with st.expander("📖 使用说明"):
    st.markdown("""
    ### 功能说明
    
    1. **输入配置**：在左侧栏选择单平台/双平台查询模式，并填写对应应用信息和查询参数
    2. **开始查询**：点击"🚀 开始查询"按钮
    3. **查看结果**：单平台模式生成 2 个词云图，双平台模式生成 4 个词云图
    4. **下载图片**：点击"⬇️ 下载"按钮保存词云
    
    ### 参数说明
    
    - **API Key**：SensorTower 的 auth_token
    - **iOS App ID**：纯数字，从 App Store URL 或 iTunes API 查询
    - **Android Package**：仅双平台查询时需要，包名形如 com.company.app
    - **国家/语言代码**：逗号分隔（如 US,CN 或 en,zh）
    - **日期范围**：评论查询时间段
    - **单页记录数**：固定 200，不可调整
    - **自动分页**：启用后会查询所有页面（较慢）
    - **自动翻译**：非中文评论翻译为中文后生成词云
    
    ### 词云说明
    
    - **高评论 (4-5星)**：用户满意的关键词
    - **低评论 (1-2星)**：用户不满意的关键词
    - **字体大小**：词频越高字体越大
    """)

st.divider()
st.caption("💡 Powered by Streamlit | Built with SensorTower API")
