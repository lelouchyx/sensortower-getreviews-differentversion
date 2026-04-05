from __future__ import annotations

import argparse
from pathlib import Path

from .analyzer import split_reviews_by_rating, word_freq
from .config import load_settings
from .models import SearchRequest
from .sst_client import SSTClient
from .wordcloud_gen import generate_wordcloud



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SST review wordcloud generator")
    parser.add_argument("--app-id", required=True, help="App ID (same for iOS and Android)")
    parser.add_argument("--countries", default="US,CN", help="iOS App Store countries, e.g. US,CN")
    parser.add_argument("--languages", default="en,zh", help="Google Play languages, e.g. en,zh")
    parser.add_argument("--start-date", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--rating-filters", default="1,2,4,5", help="Ratings to request from API")
    parser.add_argument("--limit", type=int, default=200, help="Page size for API requests")
    parser.add_argument("--output-dir", default="outputs", help="Directory for generated images")
    return parser.parse_args()



def main() -> None:
    args = parse_args()

    settings = load_settings()
    client = SSTClient(settings)
    
    countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]
    languages = [l.strip().lower() for l in args.languages.split(",") if l.strip()]
    
    # Query iOS App Store
    ios_req = SearchRequest(
        app_id=args.app_id,
        store="apple",
        countries=countries,
        start_date=args.start_date,
        end_date=args.end_date,
        rating_filters=args.rating_filters,
        limit=args.limit,
    )
    
    # Query Google Play Android
    android_req = SearchRequest(
        app_id=args.app_id,
        store="google",
        countries=languages,  # reuse countries field for languages when store is google
        start_date=args.start_date,
        end_date=args.end_date,
        rating_filters=args.rating_filters,
        limit=args.limit,
    )

    print("=== Fetching iOS App Store reviews ===")
    ios_reviews = client.fetch_reviews(ios_req)
    print(f"iOS reviews fetched: {len(ios_reviews)}")
    
    print("\n=== Fetching Google Play reviews ===")
    android_reviews = client.fetch_reviews(android_req)
    print(f"Android reviews fetched: {len(android_reviews)}")
    
    ios_high_texts, ios_low_texts = split_reviews_by_rating(ios_reviews)
    android_high_texts, android_low_texts = split_reviews_by_rating(android_reviews)

    ios_high_freq = word_freq(ios_high_texts)
    ios_low_freq = word_freq(ios_low_texts)
    android_high_freq = word_freq(android_high_texts)
    android_low_freq = word_freq(android_low_texts)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    ios_high_output = output_dir / "ios_high_rating_wordcloud.png"
    ios_low_output = output_dir / "ios_low_rating_wordcloud.png"
    android_high_output = output_dir / "android_high_rating_wordcloud.png"
    android_low_output = output_dir / "android_low_rating_wordcloud.png"

    generate_wordcloud(ios_high_freq, ios_high_output, font_path=settings.chinese_font_path)
    generate_wordcloud(ios_low_freq, ios_low_output, font_path=settings.chinese_font_path)
    generate_wordcloud(android_high_freq, android_high_output, font_path=settings.chinese_font_path)
    generate_wordcloud(android_low_freq, android_low_output, font_path=settings.chinese_font_path)

    print(f"\n=== Summary ===")
    print(f"iOS - High-rating (4-5): {len(ios_high_texts)} -> {ios_high_output}")
    print(f"iOS - Low-rating (1-2): {len(ios_low_texts)} -> {ios_low_output}")
    print(f"Android - High-rating (4-5): {len(android_high_texts)} -> {android_high_output}")
    print(f"Android - Low-rating (1-2): {len(android_low_texts)} -> {android_low_output}")


if __name__ == "__main__":
    main()
