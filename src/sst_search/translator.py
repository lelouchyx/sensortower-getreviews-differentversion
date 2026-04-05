from __future__ import annotations

import re
import time

from deep_translator import GoogleTranslator

from .config import Settings
from .models import Review


def contains_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


class ReviewTranslator:
    """Translate non-Chinese reviews into Chinese with a built-in translator."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._translator = GoogleTranslator(source="auto", target=settings.translate_target_lang)
        self._translation_fails = 0
        self.last_stats: dict[str, int | float] = {}

    def translate_reviews(self, reviews: list[Review]) -> list[Review]:
        if not self._settings.translate_enabled:
            return reviews

        translated: list[Review] = []
        translated_count = 0
        failed_count = 0
        skipped_by_limit = 0
        translated_requests = 0
        cache: dict[str, str] = {}
        time_budget = max(0, int(self._settings.translate_time_budget_seconds))
        max_reviews = max(0, int(self._settings.translate_max_reviews))
        start_ts = time.time()

        for idx, review in enumerate(reviews):
            text = review.content.strip()
            if not text:
                translated.append(review)
                continue

            if contains_chinese(text):
                translated.append(review)
                continue

            # 跳过过长的文本直接降级
            if len(text) > 10000:
                print(f"[{idx + 1}/{len(reviews)}] 文本过长（{len(text)}）字符，跳过翻译")
                translated.append(review)
                continue

            elapsed = time.time() - start_ts
            budget_exceeded = time_budget > 0 and elapsed >= time_budget
            limit_exceeded = max_reviews > 0 and translated_requests >= max_reviews
            if budget_exceeded or limit_exceeded:
                skipped_by_limit += 1
                translated.append(review)
                continue

            if text in cache:
                zh_text = cache[text]
            else:
                translated_requests += 1
                zh_text = self._translate_text(text, idx + 1, len(reviews))
                cache[text] = zh_text

            translated.append(
                Review(
                    rating=review.rating,
                    content=zh_text,
                    version=review.version,
                    review_date=review.review_date,
                )
            )
            if zh_text != text:
                translated_count += 1
            else:
                failed_count += 1

        elapsed_total = round(time.time() - start_ts, 2)
        self.last_stats = {
            "total_reviews": len(reviews),
            "translated_requests": translated_requests,
            "translated_count": translated_count,
            "failed_count": failed_count,
            "skipped_by_limit": skipped_by_limit,
            "elapsed_seconds": elapsed_total,
        }
        print(
            "翻译统计: "
            f"请求 {translated_requests} 条, 成功 {translated_count} 条, 失败回退 {failed_count} 条, "
            f"预算跳过 {skipped_by_limit} 条, 用时 {elapsed_total}s"
        )

        return translated

    def _translate_text(self, text: str, idx: int = 0, total: int = 0) -> str:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                translated_text = self._safe_translate(text)
                self._translation_fails = 0  # 成功后重置计数
                return translated_text or text
            except Exception as e:
                last_error = e
                self._translation_fails += 1
                prefix = f"[{idx}/{total}] " if idx > 0 else ""
                print(f"{prefix}翻译失败 (尝试 {attempt}/3, 连续失败: {self._translation_fails}): {type(e).__name__}")
                time.sleep(min(2.0, 0.5 * attempt))

        if last_error is not None:
            prefix = f"[{idx}/{total}] " if idx > 0 else ""
            print(f"{prefix}翻译最终回退原文: {type(last_error).__name__}")
        return text

    def _safe_translate(self, text: str) -> str:
        # Some providers fail on very long text; split by sentence and merge.
        if len(text) <= 3500:
            result = self._translator.translate(text)
            return str(result).strip()

        segments = self._split_long_text(text)
        translated_segments: list[str] = []
        for seg in segments:
            result = self._translator.translate(seg)
            translated_segments.append(str(result).strip())
            time.sleep(0.2)  # 短段落之间加延迟，防止被限流
        return " ".join(s for s in translated_segments if s)

    @staticmethod
    def _split_long_text(text: str) -> list[str]:
        # Prefer punctuation boundaries for better translation quality.
        raw_segments = re.split(r"(?<=[.!?。！？])\s+", text)
        merged: list[str] = []
        buf = ""

        for piece in raw_segments:
            piece = piece.strip()
            if not piece:
                continue

            if len(buf) + len(piece) + 1 <= 3000:
                buf = f"{buf} {piece}".strip()
            else:
                if buf:
                    merged.append(buf)
                buf = piece

        if buf:
            merged.append(buf)

        return merged
