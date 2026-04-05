import streamlit as st
from datetime import datetime, timedelta
from pathlib import Path
import os
import sys
import re
from collections import Counter

# 添加 src 到路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from sst_search.config import Settings
from sst_search.models import Review, SearchRequest
from sst_search.sst_client import SSTClient
from sst_search.translator import ReviewTranslator, contains_chinese
from sst_search.analyzer import split_reviews_by_rating, word_freq
from sst_search.wordcloud_gen import generate_wordcloud


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


if "manual_stopwords" not in st.session_state:
    st.session_state["manual_stopwords"] = set()

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
    
    # API Key
    api_key = st.text_input(
        "🔑 SensorTower API Key",
        type="password",
        placeholder="输入你的 auth_token",
        help="从 SensorTower 开发者中心获取"
    )
    
    st.divider()
    
    # 应用信息
    st.subheader("📱 应用信息")
    app_name = st.text_input(
        "应用名称",
        value="Kimi",
        placeholder="如 Kimi, ChatGPT"
    )

    search_terms = st.text_input(
        "搜索关键词（用于品牌词过滤）",
        value="Kimi",
        placeholder="如 kimi 或 zhipu，可用逗号分隔多个"
    )
    
    ios_app_id = st.text_input(
        "iOS App ID (数字)",
        value="6474233312",
        placeholder="如 6474233312"
    )
    
    android_package = st.text_input(
        "Android Package ID",
        value="com.moonshot.kimichat",
        placeholder="如 com.moonshot.kimichat"
    )
    
    st.divider()
    
    # 查询参数
    st.subheader("📅 查询参数")
    
    # 日期范围
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input(
            "开始日期",
            value=datetime.now() - timedelta(days=30)
        )
    with col2:
        end_date = st.date_input(
            "结束日期",
            value=datetime.now()
        )
    
    st.divider()
    
    # iOS 国家
    st.subheader("🍎 iOS - App Store")
    ios_countries = st.text_input(
        "国家代码 (逗号分隔)",
        value="US,CN",
        placeholder="如 US,CN,JP"
    )
    
    st.divider()
    
    # Android 语言
    st.subheader("🤖 Android - Google Play")
    android_languages = st.text_input(
        "语言代码 (逗号分隔)",
        value="en,zh",
        placeholder="如 en,zh,ja"
    )
    
    st.divider()
    
    # 其他选项
    st.subheader("🔧 其他选项")
    limit = st.slider(
        "单页记录数",
        min_value=10,
        max_value=500,
        value=200,
        step=50
    )
    
    fetch_all_pages = st.checkbox(
        "自动获取全部页面",
        value=False,
        help="启用后会自动分页查询所有数据（较慢）"
    )
    
    translate_enabled = st.checkbox(
        "启用自动翻译",
        value=True,
        help="非中文评论自动翻译为中文"
    )

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
col1, col2, col3 = st.columns(3)
with col1:
    st.metric("应用名称", app_name)
with col2:
    st.metric("iOS App ID", ios_app_id)
with col3:
    st.metric("Android Package", android_package[-20:] if len(android_package) > 20 else android_package)

st.divider()

# 查询按钮
if st.button("🚀 开始查询", use_container_width=True, type="primary"):
    
    if not api_key:
        st.error("❌ 请输入 API Key")
    elif not ios_app_id or not android_package:
        st.error("❌ 请输入 iOS App ID 和 Android Package")
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
                    api_base_url="https://api.sensortower.com",
                    api_key=api_key,
                    timeout_seconds=30,
                    verify_ssl=True,
                    fetch_all_pages=fetch_all_pages,
                    review_list_path="feedback",
                    review_text_field="content",
                    review_rating_field="rating",
                    translate_enabled=translate_enabled,
                    translate_target_lang="zh-CN",
                    chinese_font_path="C:\\Windows\\Fonts\\msyh.ttc"  # Windows 中文字体
                )
                
                client = SSTClient(settings)
                translator = ReviewTranslator(settings)
                
                # ========== iOS 查询 ==========
                status_text.text("📲 正在查询 iOS App Store 评论...")
                progress_bar.progress(15)
                
                ios_req = SearchRequest(
                    app_id=ios_app_id,
                    store="apple",
                    countries=[c.strip().upper() for c in ios_countries.split(",") if c.strip()],
                    start_date=start_date.strftime("%Y-%m-%d"),
                    end_date=end_date.strftime("%Y-%m-%d"),
                    rating_filters="1,2,4,5",
                    limit=limit
                )
                
                ios_reviews = client.fetch_reviews(ios_req)
                progress_bar.progress(35)
                
                # ========== Android 查询 ==========
                status_text.text("🤖 正在查询 Google Play 评论...")
                progress_bar.progress(45)
                
                android_req = SearchRequest(
                    app_id=android_package,
                    store="google",
                    countries=[l.strip().lower() for l in android_languages.split(",") if l.strip()],
                    start_date=start_date.strftime("%Y-%m-%d"),
                    end_date=end_date.strftime("%Y-%m-%d"),
                    rating_filters="1,2,4,5",
                    limit=limit
                )
                
                android_reviews = client.fetch_reviews(android_req)
                progress_bar.progress(55)
                
                # ========== 翻译 ==========
                status_text.text("🌐 正在翻译评论...")
                progress_bar.progress(65)
                
                ios_reviews = translator.translate_reviews(ios_reviews)
                android_reviews = translator.translate_reviews(android_reviews)

                # Diagnostics: if many records remain non-Chinese, translation likely degraded.
                ios_non_zh = sum(1 for r in ios_reviews if r.content and not contains_chinese(r.content))
                android_non_zh = sum(1 for r in android_reviews if r.content and not contains_chinese(r.content))
                
                progress_bar.progress(75)
                
                # ========== 分析 ==========
                status_text.text("📊 正在分析词频...")
                progress_bar.progress(80)
                
                ios_high_texts, ios_low_texts = split_reviews_by_rating(ios_reviews)
                android_high_texts, android_low_texts = split_reviews_by_rating(android_reviews)

                # Cache processed data so users can iterate stopwords without re-fetching API data.
                st.session_state["analysis_data"] = {
                    "app_name": app_name,
                    "android_package": android_package,
                    "search_terms": search_terms,
                    "font_path": settings.chinese_font_path,
                    "translate_enabled": translate_enabled,
                    "ios_total": len(ios_reviews),
                    "android_total": len(android_reviews),
                    "ios_non_zh": ios_non_zh,
                    "android_non_zh": android_non_zh,
                    "ios_high_texts": ios_high_texts,
                    "ios_low_texts": ios_low_texts,
                    "android_high_texts": android_high_texts,
                    "android_low_texts": android_low_texts,
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
                st.code(traceback.format_exc(), language="python")

if "analysis_data" in st.session_state:
    data = st.session_state["analysis_data"]

    app_domain_stopwords = build_domain_stopwords(
        data["app_name"],
        data["android_package"],
        data.get("search_terms", data["app_name"]),
    )
    active_stopwords = app_domain_stopwords | st.session_state["manual_stopwords"]

    ios_high_freq = word_freq(data["ios_high_texts"], extra_stopwords=active_stopwords)
    ios_low_freq = word_freq(data["ios_low_texts"], extra_stopwords=active_stopwords)
    android_high_freq = word_freq(data["android_high_texts"], extra_stopwords=active_stopwords)
    android_low_freq = word_freq(data["android_low_texts"], extra_stopwords=active_stopwords)

    noise_candidates = suggest_noise_terms(
        [ios_high_freq, ios_low_freq, android_high_freq, android_low_freq],
        top_n=noise_top_n,
    )

    st.subheader("📈 查询统计")
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
        f"翻译后仍为非中文: iOS {data['ios_non_zh']}/{data['ios_total']}，Android {data['android_non_zh']}/{data['android_total']}"
    )
    if data["translate_enabled"] and (
        (data["ios_total"] > 0 and data["ios_non_zh"] / data["ios_total"] > 0.5)
        or (data["android_total"] > 0 and data["android_non_zh"] / data["android_total"] > 0.5)
    ):
        st.warning("检测到较高比例英文原文，可能是翻译服务限流导致回退原文。可缩小时间范围或降低查询条数后重试。")

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
    st.subheader("🎨 词云结果")

    output_dir = Path("outputs_ui_latest")
    output_dir.mkdir(exist_ok=True)
    ios_high_output = output_dir / "ios_high.png"
    ios_low_output = output_dir / "ios_low.png"
    android_high_output = output_dir / "android_high.png"
    android_low_output = output_dir / "android_low.png"

    generate_wordcloud(ios_high_freq, ios_high_output, font_path=data["font_path"])
    generate_wordcloud(ios_low_freq, ios_low_output, font_path=data["font_path"])
    generate_wordcloud(android_high_freq, android_high_output, font_path=data["font_path"])
    generate_wordcloud(android_low_freq, android_low_output, font_path=data["font_path"])

    tab1, tab2, tab3, tab4 = st.tabs([
        "📲 iOS - 高评论(4-5)",
        "📲 iOS - 低评论(1-2)",
        "🤖 Android - 高评论(4-5)",
        "🤖 Android - 低评论(1-2)",
    ])

    from PIL import Image

    with tab1:
        img = Image.open(ios_high_output)
        st.image(img, caption=f"iOS 4-5星评论 ({len(data['ios_high_texts'])} 条)", use_column_width=True)
        with open(ios_high_output, "rb") as f:
            st.download_button("⬇️ 下载", f, f"ios_high_{data['app_name']}.png")

    with tab2:
        img = Image.open(ios_low_output)
        st.image(img, caption=f"iOS 1-2星评论 ({len(data['ios_low_texts'])} 条)", use_column_width=True)
        with open(ios_low_output, "rb") as f:
            st.download_button("⬇️ 下载", f, f"ios_low_{data['app_name']}.png")

    with tab3:
        img = Image.open(android_high_output)
        st.image(img, caption=f"Android 4-5星评论 ({len(data['android_high_texts'])} 条)", use_column_width=True)
        with open(android_high_output, "rb") as f:
            st.download_button("⬇️ 下载", f, f"android_high_{data['app_name']}.png")

    with tab4:
        img = Image.open(android_low_output)
        st.image(img, caption=f"Android 1-2星评论 ({len(data['android_low_texts'])} 条)", use_column_width=True)
        with open(android_low_output, "rb") as f:
            st.download_button("⬇️ 下载", f, f"android_low_{data['app_name']}.png")

st.divider()

# 底部说明
with st.expander("📖 使用说明"):
    st.markdown("""
    ### 功能说明
    
    1. **输入配置**：在左侧栏输入 API Key、应用信息和查询参数
    2. **开始查询**：点击"🚀 开始查询"按钮
    3. **查看结果**：自动生成 4 个词云图（iOS/Android × 高评/低评）
    4. **下载图片**：点击"⬇️ 下载"按钮保存词云
    
    ### 参数说明
    
    - **API Key**：SensorTower 的 auth_token
    - **iOS App ID**：纯数字，从 App Store URL 或 iTunes API 查询
    - **Android Package**：包名，形如 com.company.app
    - **国家/语言代码**：逗号分隔（如 US,CN 或 en,zh）
    - **日期范围**：评论查询时间段
    - **单页记录数**：单次 API 请求的条数（200 推荐）
    - **自动分页**：启用后会查询所有页面（较慢）
    - **自动翻译**：非中文评论翻译为中文后生成词云
    
    ### 词云说明
    
    - **高评论 (4-5星)**：用户满意的关键词
    - **低评论 (1-2星)**：用户不满意的关键词
    - **字体大小**：词频越高字体越大
    """)

st.divider()
st.caption("💡 Powered by Streamlit | Built with SensorTower API")
