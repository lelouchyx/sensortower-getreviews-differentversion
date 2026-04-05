import re
from collections import Counter

from .models import Review

try:
    import jieba
except ImportError:  # pragma: no cover
    jieba = None

# Basic English stopwords. Extend or replace for your language/domain.
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "am", "to", "of", "in", "on",
    "for", "with", "it", "this", "that", "was", "were", "be", "as", "at", "by", "from",
    "i", "you", "we", "they", "he", "she", "my", "our", "your", "their", "app",
    "very", "really", "just", "also", "too", "still", "even", "quite", "much", "more", "most",
    "can", "could", "would", "should", "will", "do", "did", "does", "done", "have", "has", "had",
    "get", "got", "make", "made", "use", "used", "using", "like", "one", "two",
    "good", "bad", "great", "nice", "well", "better", "best", "worse", "worst",
    "ios", "android", "google", "play", "store", "review", "reviews",
}

ZH_STOPWORDS = {
    "的", "了", "是", "我", "你", "他", "她", "它", "我们", "你们", "他们", "这", "那",
    "一个", "没有", "而且", "因为", "所以", "非常", "真的", "还是", "就是", "这个", "那个",
    "应用", "软件", "东西", "感觉", "如果", "但是",
    "比较", "特别", "有点", "很多", "一些", "这种", "那个", "这里", "那里", "然后", "已经",
    "可以", "能够", "不是", "没有", "还有", "以及", "希望", "觉得", "问题", "功能",
    "很好", "不错", "一般", "体验", "使用", "真的", "非常", "太", "更", "最",
    "苹果", "安卓", "谷歌", "商店", "评论",
}



def split_reviews_by_rating(reviews: list[Review]) -> tuple[list[str], list[str]]:
    high = [r.content for r in reviews if 4 <= r.rating <= 5]
    low = [r.content for r in reviews if 1 <= r.rating <= 2]
    return high, low



def tokenize(texts: list[str], extra_stopwords: set[str] | None = None) -> list[str]:
    joined = " ".join(texts).lower()
    custom = {w.strip().lower() for w in (extra_stopwords or set()) if w and w.strip()}
    english_words = re.findall(r"[a-zA-Z]{2,}", joined)
    english_tokens = [w for w in english_words if w not in STOPWORDS and w not in custom]

    chinese_text = " ".join(re.findall(r"[\u4e00-\u9fff]+", joined))
    if jieba is not None and chinese_text:
        zh_tokens_raw = [w.strip() for w in jieba.lcut(chinese_text)]
    else:
        # Fallback: use character-level tokens when jieba is unavailable.
        zh_tokens_raw = list(chinese_text.replace(" ", ""))

    zh_tokens = [
        token
        for token in zh_tokens_raw
        if token and token not in ZH_STOPWORDS and token not in custom and len(token) >= 2
    ]

    return english_tokens + zh_tokens



def word_freq(texts: list[str], extra_stopwords: set[str] | None = None) -> Counter:
    return Counter(tokenize(texts, extra_stopwords=extra_stopwords))
