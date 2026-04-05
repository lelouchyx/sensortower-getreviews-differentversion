from __future__ import annotations

import re
from collections import OrderedDict

from .models import Review

DEFAULT_ENDGAME_KEYWORDS = {
    "毕业", "拉满", "高投入", "满练",
    "圣遗物", "遗器", "驱动盘", "武器", "天赋",
    "定轨", "定向", "圣言自明机", "祝圣之霜", "尘脂", "自塑尘脂", "遂愿尘脂", "变量骰子", "母盘", "调律校音器", "谐振核心仪",
    "词条", "主词条", "副词条", "双暴", "暴击率", "暴击伤害", "充能", "精通", "击破特攻", "异常精通", "穿透", "有效词条", "无效词条",
    "天赋材料", "武器突破", "角色升级", "角色突破", "素材", "材料", "体力", "原萃树脂", "开拓力", "电量", "浓缩树脂", "燃料", "电池", "周本", "秘境", "副本", "刷本",
    "强化", "替换", "锁定", "分解", "拆解", "合成", "洗词条", "凹词条", "roll词条", "胚子", "升阶", "突破", "升级", "养成",
    "重复刷", "高耗时", "随机性", "不确定性", "歪词条", "毕业难", "材料缺口", "体力不足", "养成周期长", "零提升", "双倍活动",
}

OTHER_FACTOR_KEYWORDS = {
    "剧情", "文案", "角色", "主线", "配音", "音乐", "美术", "画面", "战斗", "打击感", "特效", "操作", "优化", "卡顿", "闪退", "氪金", "活动",
}

SENTIMENT_CUES = {
    "喜欢", "不喜欢", "满意", "不满意", "推荐", "失望", "舒服", "难受", "好玩", "不好玩", "无聊", "上头", "劝退", "烂", "优秀", "垃圾", "棒", "差",
}

ISSUE_CUES = {
    "膨胀", "数值", "跟不上", "退环境", "打不过", "歪", "保底", "抽卡", "逼氪", "肝", "耗时", "重复", "坐牢", "压力", "焦虑", "卡关",
    "power creep", "hard pity", "lose 50/50", "rng", "grind",
}

WEAK_PREFIX = re.compile(r"^(我觉得|我感觉|感觉|觉得|其实|真的|确实|总体来说|总的来说|就是说|就是)[:：,，\s]*")
MARKER_ONLY_RE = re.compile(r"^(但是|但|不过|然而|可是|却|but|however|though|yet)$", re.IGNORECASE)


def normalize_text(text: str) -> str:
    normalized = text.replace("\r", " ").replace("\n", " ")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def split_clauses(text: str) -> list[str]:
    chunks: list[str] = []
    for sentence in re.split(r"[。！？!?;；]", text):
        sentence = sentence.strip()
        if not sentence:
            continue

        # 中文评论里常用空格分隔短句，这里在中文占主导时按空格继续切。
        zh_count = len(re.findall(r"[\u4e00-\u9fff]", sentence))
        if zh_count >= 6 and " " in sentence:
            sentence = sentence.replace("  ", " ").replace(" ", "，")

        parts = re.split(r"[，,](?=.{2,})", sentence)
        for part in parts:
            piece = part.strip()
            if piece:
                chunks.append(piece)
    return chunks


def _contains_any(text: str, keywords: set[str]) -> bool:
    lowered = text.lower()
    return any(k in lowered for k in keywords)


def find_matched_keywords(text: str, keywords: set[str], max_hits: int = 5) -> list[str]:
    lowered = (text or "").lower()
    matched: list[str] = []
    for kw in sorted((k.strip() for k in keywords if k and k.strip()), key=len, reverse=True):
        if kw.lower() in lowered and kw not in matched:
            matched.append(kw)
        if len(matched) >= max_hits:
            break
    return matched


def _score_clause(clause: str) -> int:
    score = 0
    if _contains_any(clause, SENTIMENT_CUES):
        score += 3
    if _contains_any(clause, DEFAULT_ENDGAME_KEYWORDS):
        score += 4
    if _contains_any(clause, ISSUE_CUES):
        score += 3
    if _contains_any(clause, OTHER_FACTOR_KEYWORDS):
        score += 2
    if re.search(r"不\w{0,2}(喜欢|满意|推荐|好|行)|没\w{0,2}(意思|体验|手感)", clause):
        score += 2
    if 4 <= len(clause) <= 60:
        score += 1
    return score


def _clean_clause(clause: str) -> str:
    cleaned = WEAK_PREFIX.sub("", clause).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip("，,。；;:")
    if MARKER_ONLY_RE.fullmatch(cleaned):
        return ""
    return cleaned


def _compress_endgame_clause(clause: str, keywords: set[str]) -> str:
    cleaned = _clean_clause(clause)
    if not cleaned:
        return ""

    # 优先保留包含养成关键词的最短子句，避免把无关后半段一并带入。
    candidates = re.split(r"[，,、:：]", cleaned)
    hit_candidates = [c.strip() for c in candidates if c.strip() and _contains_any(c, keywords)]
    if hit_candidates:
        hit_candidates.sort(key=len)
        return hit_candidates[0][:80]

    # 若未切出子句，截取关键词附近窗口。
    lowered = cleaned.lower()
    for kw in sorted(keywords, key=len, reverse=True):
        pos = lowered.find(kw)
        if pos >= 0:
            left = max(0, pos - 18)
            right = min(len(cleaned), pos + len(kw) + 18)
            return cleaned[left:right].strip(" ，,。；;:")
    return ""


def simplify_review(text: str, max_clauses: int = 3) -> str:
    normalized = normalize_text(text)
    if not normalized:
        return ""

    clauses = split_clauses(normalized)

    if not clauses:
        return normalized[:80]

    cleaned_all = [_clean_clause(c) for c in clauses]
    cleaned_all = [c for c in cleaned_all if c]
    if not cleaned_all:
        return ""
    if len("；".join(cleaned_all)) <= 140:
        return "；".join(cleaned_all)

    max_clauses = max(2, max_clauses)

    # 先按全句评分选核心，再补齐首尾语境，避免只抓到局部片段。
    scored = [(idx, clause, _score_clause(clause)) for idx, clause in enumerate(cleaned_all)]
    scored.sort(key=lambda x: (x[2], -len(x[1])), reverse=True)

    selected_idx: set[int] = set(idx for idx, _, _ in scored[:max_clauses])
    selected_idx.add(0)
    selected_idx.add(len(cleaned_all) - 1)

    # 若包含强问题信号，最多补充2句，避免摘要过长。
    issue_candidates = [
        (idx, _score_clause(clause))
        for idx, clause in enumerate(cleaned_all)
        if _contains_any(clause, ISSUE_CUES)
    ]
    issue_candidates.sort(key=lambda x: x[1], reverse=True)
    for idx, _ in issue_candidates[:2]:
        selected_idx.add(idx)

    ordered_idx = sorted(selected_idx)
    selected = [cleaned_all[i] for i in ordered_idx if 0 <= i < len(cleaned_all)]

    # 控制最终长度，但尽量保留多句信息。
    result_parts: list[str] = []
    for clause in selected:
        tentative = "；".join(result_parts + [clause])
        if len(tentative) <= 160:
            result_parts.append(clause)
        else:
            break

    if not result_parts:
        return selected[0][:180]
    return "；".join(result_parts)


def simplify_endgame_direction(text: str, endgame_keywords: set[str] | None = None) -> str:
    keywords = {k.lower() for k in (endgame_keywords or DEFAULT_ENDGAME_KEYWORDS) if k and k.strip()}
    normalized = normalize_text(text)
    if not normalized:
        return ""

    clauses = split_clauses(normalized)

    endgame_clauses = [c for c in clauses if _contains_any(c, keywords)]
    if not endgame_clauses:
        return ""

    dedup = OrderedDict()
    for clause in endgame_clauses:
        concise = _compress_endgame_clause(clause, keywords)
        if concise and concise not in dedup:
            dedup[concise] = True
    return "；".join(list(dedup.keys())[:2])[:120]


def build_semantic_rows(
    reviews: list[Review],
    endgame_keywords: set[str] | None = None,
    platform: str | None = None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    # 使用原始关键词形式而不是小写，确保与API端的逻辑保持一致
    keywords_to_use = endgame_keywords or DEFAULT_ENDGAME_KEYWORDS
    for review in reviews:
        raw = normalize_text(review.content)
        if not raw:
            continue

        simplified = simplify_review(raw)
        matched_keywords = find_matched_keywords(raw, keywords_to_use)
        # 只要原始评论命中养成关键词，就进入养成方向精简；精简文本仅负责压缩表达。
        endgame_simplified = simplify_endgame_direction(raw, endgame_keywords=keywords_to_use) if matched_keywords else ""

        rows.append(
            {
                "平台": platform or "",
                "评分": str(review.rating),
                "评论时间": (review.review_date or "").strip() or "未知",
                "归属版本": (review.version or "").strip() or "未知版本",
                "原始评论": raw,
                "精简评论": simplified,
                "养成方向的精简评论": endgame_simplified,
                "命中关键词": " | ".join(matched_keywords),
            }
        )
    return rows
