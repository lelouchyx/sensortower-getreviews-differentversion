from dataclasses import dataclass
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    api_base_url: str
    api_key: str
    timeout_seconds: int = 30
    verify_ssl: bool = True
    fetch_all_pages: bool = True
    translate_enabled: bool = True
    translate_target_lang: str = "zh-CN"
    translate_max_reviews: int = 400
    translate_time_budget_seconds: int = 180
    chinese_font_path: str = ""
    review_list_path: str = "feedback"
    review_text_field: str = "content"
    review_rating_field: str = "rating"



def _to_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}



def load_settings() -> Settings:
    load_dotenv()

    base_url = os.getenv("SST_API_BASE_URL", "").strip()
    api_key = os.getenv("SST_API_KEY", "").strip()

    if not base_url:
        raise ValueError("Missing environment variable: SST_API_BASE_URL")
    if not api_key:
        raise ValueError("Missing environment variable: SST_API_KEY")

    timeout_raw = os.getenv("SST_TIMEOUT_SECONDS", "30").strip()
    verify_raw = os.getenv("SST_VERIFY_SSL", "true")
    fetch_all_pages_raw = os.getenv("SST_FETCH_ALL_PAGES", "true")
    translate_enabled_raw = os.getenv("TRANSLATE_ENABLED", "true")
    translate_max_reviews_raw = os.getenv("TRANSLATE_MAX_REVIEWS", "400").strip()
    translate_time_budget_raw = os.getenv("TRANSLATE_TIME_BUDGET_SECONDS", "180").strip()

    return Settings(
        api_base_url=base_url,
        api_key=api_key,
        timeout_seconds=int(timeout_raw),
        verify_ssl=_to_bool(verify_raw),
        fetch_all_pages=_to_bool(fetch_all_pages_raw),
        translate_enabled=_to_bool(translate_enabled_raw),
        translate_target_lang=os.getenv("TRANSLATE_TARGET_LANG", "zh-CN").strip() or "zh-CN",
        translate_max_reviews=max(0, int(translate_max_reviews_raw)),
        translate_time_budget_seconds=max(0, int(translate_time_budget_raw)),
        chinese_font_path=os.getenv("CHINESE_FONT_PATH", "").strip(),
        review_list_path=os.getenv("SST_REVIEW_LIST_PATH", "feedback").strip() or "feedback",
        review_text_field=os.getenv("SST_REVIEW_TEXT_FIELD", "content").strip() or "content",
        review_rating_field=os.getenv("SST_REVIEW_RATING_FIELD", "rating").strip() or "rating",
    )
