from collections import Counter
from pathlib import Path

from wordcloud import WordCloud



def generate_wordcloud(freq: Counter, output_path: Path, font_path: str | None = None) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not freq:
        # Ensure we still output a file so pipeline is deterministic.
        freq = Counter({"no_data": 1})

    cloud = WordCloud(
        width=1400,
        height=900,
        background_color="white",
        colormap="viridis",
        max_words=200,
        font_path=font_path or None,
    ).generate_from_frequencies(freq)

    cloud.to_file(str(output_path))
