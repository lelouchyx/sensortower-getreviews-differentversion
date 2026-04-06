from __future__ import annotations

import html
from typing import Any
from urllib.parse import urlparse

import requests
from requests.exceptions import ConnectTimeout, ConnectionError as RequestsConnectionError, HTTPError, ReadTimeout

from .config import Settings
from .models import Review, SearchRequest
from .review_semantic import find_matched_keywords


class SSTClient:
    _MAX_LIMIT_PER_PAGE = 200

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.last_fetch_api_calls = 0
        self.last_fetch_stop_reason = "not_started"
        self.last_fetch_qualifying_count = 0

    @staticmethod
    def _build_base_url_candidates(configured_base_url: str) -> list[str]:
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

    def fetch_reviews(self, req: SearchRequest) -> list[Review]:
        self.last_fetch_api_calls = 0
        self.last_fetch_stop_reason = "not_started"
        self.last_fetch_qualifying_count = 0
        # Select endpoint and param name based on store
        if req.store == "apple":
            endpoint = "/v1/ios/review/get_reviews"
            region_param_name = "countries"
        elif req.store == "google":
            endpoint = "/v1/android/review/get_reviews"
            region_param_name = "languages"
        else:
            raise ValueError(f"Unsupported store: {req.store}. Use 'apple' or 'google'.")

        base_url_candidates = self._build_base_url_candidates(self._settings.api_base_url)
        active_base_url = base_url_candidates[0]
        url = f"{active_base_url}{endpoint}"

        all_reviews: list[Review] = []
        seen_ids: set[int] = set()
        page = 1
        api_calls = 0
        qualifying_count = 0
        stop_reason = "unknown"
        # 使用原始关键词形式以匹配find_matched_keywords的期望
        qualifying_terms = {t.strip() for t in (req.qualifying_terms or []) if t and t.strip()}
        min_qualifying_reviews = max(0, int(req.min_qualifying_reviews or 0)) if req.min_qualifying_reviews else 0
        max_pages = max(1, int(req.max_pages)) if req.max_pages is not None else None

        while True:
            if max_pages is not None and api_calls >= max_pages:
                stop_reason = "max_api_calls_reached"
                break

            safe_limit = max(1, min(int(req.limit), self._MAX_LIMIT_PER_PAGE))
            base_params = {
                "app_id": req.app_id,
                "start_date": req.start_date,
                "end_date": req.end_date,
                region_param_name: ",".join(req.countries),  # Use countries field for both stores
                "rating_filters": req.rating_filters,
                # SensorTower review endpoint对单页limit有上限，超出会返回422。
                "limit": safe_limit,
                "page": page,
                "auth_token": self._settings.api_key.strip().strip("\"'"),
            }

            response = None
            last_timeout_error: ConnectTimeout | None = None
            last_read_timeout_error: ReadTimeout | None = None
            last_connection_error: RequestsConnectionError | None = None
            unauthorized_hosts: list[str] = []
            unauthorized_details: list[str] = []
            last_http_error: HTTPError | None = None
            data: dict[str, Any] | None = None

            for candidate_base_url in base_url_candidates:
                url = f"{candidate_base_url}{endpoint}"
                api_host = urlparse(candidate_base_url).hostname or candidate_base_url
                try:
                    candidate_response = requests.get(
                        url,
                        params=base_params,
                        headers={"accept": "*/*"},
                        timeout=self._settings.timeout_seconds,
                        verify=self._settings.verify_ssl,
                    )
                    candidate_response.raise_for_status()

                    candidate_data = candidate_response.json()
                    payload_error = self._extract_error_payload(candidate_data)
                    if payload_error is not None:
                        code, message = payload_error
                        if self._is_auth_error_message(message):
                            unauthorized_hosts.append(str(api_host))
                            unauthorized_details.append(f"{api_host}: {message[:220]}")
                            continue
                        raise RuntimeError(f"SST API 返回错误（code={code}）：{message}")

                    response = candidate_response
                    data = candidate_data
                    active_base_url = candidate_base_url
                    api_calls += 1
                    self.last_fetch_api_calls = api_calls
                    break
                except ConnectTimeout as exc:
                    last_timeout_error = exc
                    continue
                except ReadTimeout as exc:
                    # 读取超时：尝试切换备用域名继续请求，避免单一域名链路抖动导致整体失败。
                    last_read_timeout_error = exc
                    continue
                except RequestsConnectionError as exc:
                    last_connection_error = exc
                    continue
                except HTTPError as exc:
                    last_http_error = exc
                    status_code = exc.response.status_code if exc.response is not None else None
                    if status_code == 401:
                        unauthorized_hosts.append(str(api_host))
                        raw_text = ""
                        if exc.response is not None and exc.response.text:
                            raw_text = " ".join(exc.response.text.strip().split())[:220]
                        if raw_text:
                            unauthorized_details.append(f"{api_host}: {raw_text}")
                        continue
                    if status_code == 422:
                        detail = ""
                        if exc.response is not None and exc.response.text:
                            detail = " ".join(exc.response.text.strip().split())[:260]
                        raise RuntimeError(
                            "请求参数无效（422）。"
                            f"已自动使用 limit<={self._MAX_LIMIT_PER_PAGE}，"
                            "请检查 app_id、日期范围、国家/语言代码与 rating_filters。"
                            + (f" 服务端返回: {detail}" if detail else "")
                        ) from exc
                    raise

            if response is None:
                if unauthorized_hosts:
                    hosts = " / ".join(unauthorized_hosts)
                    details = " | ".join(unauthorized_details[:2])
                    raise RuntimeError(
                        f"{hosts} 返回 401 Unauthorized："
                        "该 token 可能无此接口权限，或账号被限制访问 review 端点。"
                        + (f" 服务端返回: {details}" if details else "")
                    ) from last_http_error
                if last_timeout_error is not None:
                    api_host = urlparse(active_base_url).hostname or active_base_url
                    raise RuntimeError(
                        f"连接 {api_host} 超时。请检查当前网络/代理设置，"
                        "或增大 SST_TIMEOUT_SECONDS 后重试。"
                    ) from last_timeout_error
                if last_read_timeout_error is not None:
                    api_host = urlparse(active_base_url).hostname or active_base_url
                    raise RuntimeError(
                        f"读取 {api_host} 响应超时。请检查当前网络质量，"
                        "可尝试增大 API 超时秒数或切换网络后重试。"
                    ) from last_read_timeout_error
                if last_connection_error is not None:
                    raise RuntimeError(
                        "无法连接 SensorTower API。请检查网络连通性、DNS、代理或防火墙策略。"
                    ) from last_connection_error
                raise RuntimeError("请求失败：未收到有效响应。")
            if data is None:
                data = response.json()

            page_reviews = self._parse_reviews(data)
            new_count = 0
            raw_feedback = data.get("feedback", [])
            for idx, review in enumerate(page_reviews):
                row = raw_feedback[idx] if idx < len(raw_feedback) else {}
                row_id = row.get("id") if isinstance(row, dict) else None
                if isinstance(row_id, int) and row_id in seen_ids:
                    continue
                if isinstance(row_id, int):
                    seen_ids.add(row_id)
                all_reviews.append(review)
                # 使用与CSV相同的关键词匹配逻辑确保一致性
                if min_qualifying_reviews and qualifying_terms:
                    matched = find_matched_keywords(review.content, qualifying_terms, max_hits=5)
                    if matched:
                        qualifying_count += 1
                new_count += 1

            if min_qualifying_reviews and qualifying_count >= min_qualifying_reviews:
                stop_reason = "target_reached"
                break
            if not self._settings.fetch_all_pages and min_qualifying_reviews == 0:
                stop_reason = "single_page_mode"
                break

            page_count = data.get("page_count")
            chasing_target = min_qualifying_reviews > 0 and qualifying_count < min_qualifying_reviews
            # 若本页无新增评论，继续请求只会重复耗费API。
            if new_count == 0:
                stop_reason = "no_new_data"
                break
            # 已到服务端最后一页时，继续请求也不会有新数据。
            if isinstance(page_count, int) and page >= page_count:
                stop_reason = "end_of_pages"
                break
            # 服务端未返回page_count且不追目标时，按稳妥策略停止。
            if not isinstance(page_count, int) and not chasing_target:
                stop_reason = "page_count_unavailable"
                break
            page += 1

        self.last_fetch_api_calls = api_calls
        self.last_fetch_stop_reason = stop_reason
        self.last_fetch_qualifying_count = qualifying_count
        return all_reviews

    def _extract_error_payload(self, data: Any) -> tuple[str, str] | None:
        if not isinstance(data, dict):
            return None
        if "error" not in data:
            return None

        err = data.get("error")
        if isinstance(err, dict):
            code = err.get("code") or err.get("status") or err.get("type") or "unknown"
            message = (
                err.get("message")
                or err.get("detail")
                or err.get("error")
                or str(err)
            )
        else:
            code = "unknown"
            message = str(err)

        return str(code), str(message)

    @staticmethod
    def _is_auth_error_message(message: str) -> bool:
        lowered = (message or "").lower()
        return (
            "invalid authentication token" in lowered
            or ("auth" in lowered and "token" in lowered)
            or "unauthorized" in lowered
            or "invalid token" in lowered
        )

    @staticmethod
    def _contains_any_term(text: str, terms: set[str]) -> bool:
        lowered = (text or "").lower()
        return any(term in lowered for term in terms)

    def _parse_reviews(self, data: dict[str, Any]) -> list[Review]:
        """Parse response into Review list using configurable mapping and fallbacks."""
        raw_reviews = self._extract_review_list(data)
        if not isinstance(raw_reviews, list):
            keys_preview = ", ".join(sorted([str(k) for k in data.keys()][:20])) if isinstance(data, dict) else type(data).__name__
            raise ValueError(
                "Cannot locate review list from SST response. "
                "Please set SST_REVIEW_LIST_PATH, e.g. data.reviews. "
                f"Top-level keys/type: {keys_preview}"
            )

        parsed: list[Review] = []

        for item in raw_reviews:
            if not isinstance(item, dict):
                continue

            item_for_read = self._unwrap_review_item(item)

            rating = self._pick_value(item_for_read, self._settings.review_rating_field, ["rating", "score", "star", "stars", "star_rating", "rating_value"])
            content = self._pick_value(item_for_read, self._settings.review_text_field, ["content", "text", "review", "comment", "body", "review_text", "message"])
            version = self._pick_value(item_for_read, "app_version", ["version", "appVersion", "build_version", "buildVersion", "app_version"])
            review_date = self._pick_value(item_for_read, "date", ["created_at", "review_date", "updated_at", "createdAt", "published_at", "timestamp"])
            if rating is None:
                continue

            text = html.unescape(str(content)).strip()
            if not text:
                continue

            try:
                parsed.append(
                    Review(
                        rating=int(float(rating)),
                        content=text,
                        version=str(version).strip() if version is not None and str(version).strip() else None,
                        review_date=str(review_date).strip() if review_date is not None and str(review_date).strip() else None,
                    )
                )
            except (TypeError, ValueError):
                continue

        return parsed

    def _extract_review_list(self, data: dict[str, Any]) -> Any:
        # 1) Honor explicit dot-path mapping first.
        value = self._get_by_path(data, self._settings.review_list_path)
        if isinstance(value, list):
            return value

        # 2) Fallback to common list keys for quick bootstrap.
        for key in [
            "feedback",
            "data.feedback",
            "feedback.items",
            "feedback.reviews",
            "reviews",
            "data.reviews",
            "data.list",
            "list",
            "records",
            "items",
            "data.items",
            "results",
            "data.results",
            "response.feedback",
            "response.reviews",
            "result.feedback",
            "result.reviews",
            "data",
        ]:
            v = self._get_by_path(data, key)
            if isinstance(v, list):
                return v

        # 3) Heuristic fallback: recursively find first list that looks like review rows.
        guessed = self._find_review_like_list(data)
        if isinstance(guessed, list):
            return guessed

        return None

    def _find_review_like_list(self, payload: Any) -> list[dict[str, Any]] | None:
        def looks_like_review_row(item: Any) -> bool:
            if not isinstance(item, dict):
                return False
            keys = {str(k).lower() for k in item.keys()}
            has_text = bool({"content", "text", "review", "comment", "body", "review_text", "message"} & keys)
            has_rating = bool({"rating", "score", "star", "stars", "star_rating", "rating_value"} & keys)

            nested = None
            for nested_key in ("review", "attributes", "data", "item"):
                maybe = item.get(nested_key)
                if isinstance(maybe, dict):
                    nested = maybe
                    break
            if isinstance(nested, dict):
                nkeys = {str(k).lower() for k in nested.keys()}
                has_text = has_text or bool({"content", "text", "review", "comment", "body", "review_text", "message"} & nkeys)
                has_rating = has_rating or bool({"rating", "score", "star", "stars", "star_rating", "rating_value"} & nkeys)

            # 放宽条件：只要命中评分或正文其一即可，后续_parse_reviews还会二次过滤。
            return has_text or has_rating

        def walk(node: Any, depth: int) -> list[dict[str, Any]] | None:
            if depth > 4:
                return None

            if isinstance(node, list):
                if node and all(isinstance(x, dict) for x in node):
                    sample = node[: min(5, len(node))]
                    if any(looks_like_review_row(x) for x in sample):
                        return node
                for child in node[:10]:
                    found = walk(child, depth + 1)
                    if found is not None:
                        return found
                return None

            if isinstance(node, dict):
                for value in node.values():
                    found = walk(value, depth + 1)
                    if found is not None:
                        return found

            return None

        return walk(payload, 0)

    @staticmethod
    def _unwrap_review_item(item: dict[str, Any]) -> dict[str, Any]:
        for nested_key in ("review", "attributes", "data", "item"):
            nested = item.get(nested_key)
            if isinstance(nested, dict):
                merged = dict(item)
                merged.update(nested)
                return merged
        return item

    @staticmethod
    def _get_by_path(payload: Any, path: str) -> Any:
        if not path:
            return None

        current = payload
        for part in path.split("."):
            part = part.strip()
            if not part:
                return None
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return current

    @staticmethod
    def _pick_value(item: dict[str, Any], primary_key: str, fallback_keys: list[str]) -> Any:
        if primary_key in item and item.get(primary_key) is not None:
            return item.get(primary_key)
        for key in fallback_keys:
            if key in item and item.get(key) is not None:
                return item.get(key)
        return None
