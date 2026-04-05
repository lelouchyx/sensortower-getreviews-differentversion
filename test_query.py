import sys
from pathlib import Path
from datetime import datetime, timedelta

# 添加 src 到路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from sst_search.config import Settings
from sst_search.models import SearchRequest
from sst_search.sst_client import SSTClient
from sst_search.translator import ReviewTranslator
from sst_search.analyzer import split_reviews_by_rating, word_freq
from sst_search.wordcloud_gen import generate_wordcloud

# API Key (从环境变量读取，不要硬编码！)
api_key = os.getenv("SST_API_KEY", "")
if not api_key:
    print("❌ 错误：SST_API_KEY 环境变量未设置")
    print("   请设置环境变量: export SST_API_KEY=your_api_key")
    sys.exit(1)

# 应用信息
ios_app_id = "6474233312"
android_package = "com.moonshot.kimichat"

# 日期范围
start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
end_date = datetime.now().strftime("%Y-%m-%d")

print("=" * 60)
print("🚀 SST App Review Analyzer - Test Run")
print("=" * 60)
print(f"App: Kimi (iOS: {ios_app_id}, Android: {android_package})")
print(f"Date Range: {start_date} to {end_date}")
print("=" * 60)

try:
    # 初始化配置
    print("\n⏳ 初始化配置...")
    settings = Settings(
        api_base_url="https://api.sensortower.com",
        api_key=api_key,
        timeout_seconds=30,
        verify_ssl=True,
        fetch_all_pages=False,
        review_list_path="feedback",
        review_text_field="content",
        review_rating_field="rating",
        translate_enabled=True,  # 默认开启翻译，避免英文词云误导验证
        translate_target_lang="zh-CN",
        chinese_font_path="C:\\Windows\\Fonts\\msyh.ttc"
    )
    
    client = SSTClient(settings)
    translator = ReviewTranslator(settings)
    
    # ========== iOS 查询 ==========
    print("\n📲 正在查询 iOS App Store 评论...")
    ios_req = SearchRequest(
        app_id=ios_app_id,
        store="apple",
        countries=["US"],  # 单个国家加快查询
        start_date=start_date,
        end_date=end_date,
        rating_filters="1,2,4,5",
        limit=50  # 减少记录数加快处理
    )
    
    ios_reviews = client.fetch_reviews(ios_req)
    print(f"✓ iOS 获取 {len(ios_reviews)} 条评论")
    
    # ========== Android 查询 ==========
    print("\n🤖 正在查询 Google Play 评论...")
    android_req = SearchRequest(
        app_id=android_package,
        store="google",
        countries=["en"],  # 单个语言加快查询
        start_date=start_date,
        end_date=end_date,
        rating_filters="1,2,4,5",
        limit=50  # 减少记录数加快处理
    )
    
    android_reviews = client.fetch_reviews(android_req)
    print(f"✓ Android 获取 {len(android_reviews)} 条评论")
    
    # ========== 翻译 ==========
    print("\n🌐 正在翻译评论...")
    ios_reviews = translator.translate_reviews(ios_reviews)
    android_reviews = translator.translate_reviews(android_reviews)
    print("✓ 翻译完成")
    
    # ========== 分析 ==========
    print("\n📊 正在分析词频...")
    ios_high_texts, ios_low_texts = split_reviews_by_rating(ios_reviews)
    android_high_texts, android_low_texts = split_reviews_by_rating(android_reviews)
    
    ios_high_freq = word_freq(ios_high_texts)
    ios_low_freq = word_freq(ios_low_texts)
    android_high_freq = word_freq(android_high_texts)
    android_low_freq = word_freq(android_low_texts)
    
    print(f"✓ iOS: 高评 {len(ios_high_texts)}, 低评 {len(ios_low_texts)}")
    print(f"✓ Android: 高评 {len(android_high_texts)}, 低评 {len(android_low_texts)}")
    
    # ========== 生成词云 ==========
    print("\n🎨 正在生成词云...")
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)
    
    ios_high_output = output_dir / "ios_high_rating_wordcloud.png"
    ios_low_output = output_dir / "ios_low_rating_wordcloud.png"
    android_high_output = output_dir / "android_high_rating_wordcloud.png"
    android_low_output = output_dir / "android_low_rating_wordcloud.png"
    
    generate_wordcloud(ios_high_freq, ios_high_output, font_path=settings.chinese_font_path)
    generate_wordcloud(ios_low_freq, ios_low_output, font_path=settings.chinese_font_path)
    generate_wordcloud(android_high_freq, android_high_output, font_path=settings.chinese_font_path)
    generate_wordcloud(android_low_freq, android_low_output, font_path=settings.chinese_font_path)
    
    print("✓ 词云生成完成")
    
    # ========== 总结 ==========
    print("\n" + "=" * 60)
    print("✨ 查询完成！")
    print("=" * 60)
    print(f"📊 iOS App Store:")
    print(f"   - 总评论数: {len(ios_reviews)}")
    print(f"   - 高评 (4-5): {len(ios_high_texts)}")
    print(f"   - 低评 (1-2): {len(ios_low_texts)}")
    print(f"\n📊 Google Play:")
    print(f"   - 总评论数: {len(android_reviews)}")
    print(f"   - 高评 (4-5): {len(android_high_texts)}")
    print(f"   - 低评 (1-2): {len(android_low_texts)}")
    print(f"\n📁 输出文件:")
    print(f"   - {ios_high_output}")
    print(f"   - {ios_low_output}")
    print(f"   - {android_high_output}")
    print(f"   - {android_low_output}")
    print("=" * 60)
    
except Exception as e:
    print(f"\n❌ 错误: {str(e)}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
