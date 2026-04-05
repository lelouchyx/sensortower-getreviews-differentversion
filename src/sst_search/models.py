from dataclasses import dataclass


@dataclass
class Review:
    rating: int
    content: str
    version: str | None = None
    review_date: str | None = None


@dataclass(frozen=True)
class SearchRequest:
    app_id: str
    store: str
    countries: list[str]
    start_date: str
    end_date: str
    rating_filters: str
    limit: int
    min_qualifying_reviews: int | None = None
    qualifying_terms: list[str] | None = None
    max_pages: int | None = None
