"""video.youtube -- YouTube search or lookup by id, statistics on every item.

Pinned: the search.list and videos.list shapes (YouTube Data API v3,
captured 2026-09-27) parsed into one item record; ISO 8601 durations; a
hidden likeCount is null, never zero; a search hit that videos.list no
longer returns is still listed with null statistics; exactly one of
query / video_ids is accepted, and bad ids are refused before the gate;
Google's quota and key errors are ProviderUnavailable (named), a 400 is the
caller's, 429/5xx are transient, a 404 on ids is an empty delivery; the
provider is off without WORKER_YOUTUBE_API_KEY; the skill runs search then
ONE videos call; the catalog row and output schema this test carries are the
ones the orchestrator integrates, checked here against the skill's output.

The provider and skill are loaded by file path with the package's name
prefix because providers/__init__.py and skills/__init__.py do not import
them yet; once they do, the cached package modules are used instead.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
WORKER = "video.youtube"
SEED = {"query": "x402 payments", "max_results": 3}
IDS = ["IJhS_fv5Ktk", "aB3dE5fG7hI", "zZ9yY8xX7wW"]
LONG_DESCRIPTION = ("x402 is an open standard for internet-native payments over HTTP 402. " * 12).strip()

# search.list?part=snippet&q=x402+payments&type=video&maxResults=3 -- captured shape 2026-09-27.
# The fourth item is a channel hit, which type=video never returns but the parser must skip.
SEARCH_RESPONSE = {
    "kind": "youtube#searchListResponse", "etag": "k7Qb2xYz", "nextPageToken": "CAMQAA", "regionCode": "US",
    "pageInfo": {"totalResults": 19113, "resultsPerPage": 3},
    "items": [
        {"kind": "youtube#searchResult", "etag": "e1", "id": {"kind": "youtube#video", "videoId": "IJhS_fv5Ktk"},
         "snippet": {"publishedAt": "2026-05-14T16:00:12Z", "channelId": "UCoBPd2jgYzQ5m8k3f1nR9wA",
                     "title": "What is x402? Internet-native payments for agents",
                     "description": "A short intro to the x402 payment protocol.",
                     "thumbnails": {"default": {"url": "https://i.ytimg.com/vi/IJhS_fv5Ktk/default.jpg", "width": 120, "height": 90},
                                    "medium": {"url": "https://i.ytimg.com/vi/IJhS_fv5Ktk/mqdefault.jpg", "width": 320, "height": 180},
                                    "high": {"url": "https://i.ytimg.com/vi/IJhS_fv5Ktk/hqdefault.jpg", "width": 480, "height": 360}},
                     "channelTitle": "Coinbase Developer Platform", "liveBroadcastContent": "none",
                     "publishTime": "2026-05-14T16:00:12Z"}},
        {"kind": "youtube#searchResult", "etag": "e2", "id": {"kind": "youtube#video", "videoId": "aB3dE5fG7hI"},
         "snippet": {"publishedAt": "2026-09-27T09:00:00Z", "channelId": "UCliveXYZ12345678901234",
                     "title": "LIVE: x402 builders call", "description": "",
                     "thumbnails": {"default": {"url": "https://i.ytimg.com/vi/aB3dE5fG7hI/default.jpg", "width": 120, "height": 90}},
                     "channelTitle": "x402 Community", "liveBroadcastContent": "live",
                     "publishTime": "2026-09-27T09:00:00Z"}},
        {"kind": "youtube#searchResult", "etag": "e3", "id": {"kind": "youtube#video", "videoId": "zZ9yY8xX7wW"},
         "snippet": {"publishedAt": "2026-03-02T11:30:45Z", "channelId": "UCgoneABCDEFGHIJKLMNOPQ",
                     "title": "x402 payments walkthrough", "description": "Step by step.",
                     "thumbnails": {"medium": {"url": "https://i.ytimg.com/vi/zZ9yY8xX7wW/mqdefault.jpg", "width": 320, "height": 180}},
                     "channelTitle": "Agent Payments", "liveBroadcastContent": "none",
                     "publishTime": "2026-03-02T11:30:45Z"}},
        {"kind": "youtube#searchResult", "etag": "e4", "id": {"kind": "youtube#channel", "channelId": "UCsomechannel0123456789"},
         "snippet": {"title": "A channel, not a video", "liveBroadcastContent": "none"}},
    ]}

# videos.list?part=snippet,statistics,contentDetails&id=IJhS_fv5Ktk,aB3dE5fG7hI,zZ9yY8xX7wW -- captured shape.
# The third id is not returned (made private); the live one hides likes and comments.
VIDEOS_RESPONSE = {
    "kind": "youtube#videoListResponse", "etag": "v9",
    "items": [
        {"kind": "youtube#video", "etag": "v1", "id": "IJhS_fv5Ktk",
         "snippet": {"publishedAt": "2026-05-14T16:00:12Z", "channelId": "UCoBPd2jgYzQ5m8k3f1nR9wA",
                     "title": "What is x402? Internet-native payments for agents", "description": LONG_DESCRIPTION,
                     "thumbnails": {"default": {"url": "https://i.ytimg.com/vi/IJhS_fv5Ktk/default.jpg", "width": 120, "height": 90},
                                    "medium": {"url": "https://i.ytimg.com/vi/IJhS_fv5Ktk/mqdefault.jpg", "width": 320, "height": 180},
                                    "high": {"url": "https://i.ytimg.com/vi/IJhS_fv5Ktk/hqdefault.jpg", "width": 480, "height": 360},
                                    "standard": {"url": "https://i.ytimg.com/vi/IJhS_fv5Ktk/sddefault.jpg", "width": 640, "height": 480},
                                    "maxres": {"url": "https://i.ytimg.com/vi/IJhS_fv5Ktk/maxresdefault.jpg", "width": 1280, "height": 720}},
                     "channelTitle": "Coinbase Developer Platform", "tags": ["x402", "payments"], "categoryId": "28",
                     "liveBroadcastContent": "none", "defaultLanguage": "en",
                     "localized": {"title": "What is x402? Internet-native payments for agents", "description": LONG_DESCRIPTION},
                     "defaultAudioLanguage": "en"},
         "contentDetails": {"duration": "PT12M34S", "dimension": "2d", "definition": "hd", "caption": "false",
                            "licensedContent": True, "contentRating": {}, "projection": "rectangular"},
         "statistics": {"viewCount": "52004", "likeCount": "2225", "favoriteCount": "0", "commentCount": "112"}},
        {"kind": "youtube#video", "etag": "v2", "id": "aB3dE5fG7hI",
         "snippet": {"publishedAt": "2026-09-27T09:00:00Z", "channelId": "UCliveXYZ12345678901234",
                     "title": "LIVE: x402 builders call", "description": "",
                     "thumbnails": {"default": {"url": "https://i.ytimg.com/vi/aB3dE5fG7hI/default.jpg", "width": 120, "height": 90}},
                     "channelTitle": "x402 Community", "categoryId": "28", "liveBroadcastContent": "live"},
         "contentDetails": {"duration": "P0D", "dimension": "2d", "definition": "hd", "caption": "false",
                            "licensedContent": False, "contentRating": {}, "projection": "rectangular"},
         "statistics": {"viewCount": "1503", "favoriteCount": "0"}},
    ],
    "pageInfo": {"totalResults": 2, "resultsPerPage": 2}}

QUOTA_403 = {"error": {"code": 403, "message": "The request cannot be completed because you have exceeded your quota.",
                       "errors": [{"message": "The request cannot be completed because you have exceeded your quota.",
                                   "domain": "youtube.quota", "reason": "quotaExceeded"}]}}
KEY_INVALID_403 = {"error": {"code": 403, "message": "Bad Request",
                             "errors": [{"message": "Bad Request", "domain": "usageLimits", "reason": "keyInvalid"}]}}
KEY_INVALID_400 = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                             "errors": [{"message": "API key not valid. Please pass a valid API key.", "domain": "global",
                                         "reason": "badRequest"}],
                             "status": "INVALID_ARGUMENT",
                             "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID",
                                          "domain": "googleapis.com", "metadata": {"service": "youtube.googleapis.com"}}]}}
BAD_PARAMETER_400 = {"error": {"code": 400, "message": "Invalid value 'views'. Values must be one of: date, rating, relevance, title, videoCount, viewCount",
                               "errors": [{"message": "Invalid value 'views'.", "domain": "global", "reason": "invalidParameter",
                                           "location": "order", "locationType": "parameter"}]}}


def _load_workers():
    cached = sys.modules.get("wcag_audit_engine_workers")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "wcag_audit_engine_workers", PKG / "__init__.py",
        submodule_search_locations=[str(PKG)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["wcag_audit_engine_workers"] = module
    spec.loader.exec_module(module)
    return module


def _load_by_path(name: str, path: Path):
    """A submodule the package's __init__ does not import yet, registered
    under the package's own name prefix so its relative imports resolve."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


W = _load_workers()
YT = _load_by_path(W.__name__ + ".providers.youtube", PKG / "providers" / "youtube.py")
if getattr(W.providers, "youtube", None) is None:
    W.providers.youtube = YT  # what the package import will do once __init__ lists it; lets Worker.available() see it
V = _load_by_path(W.__name__ + ".skills.video", PKG / "skills" / "video.py")


# --- the row and the contract the orchestrator integrates -------------------------

def _output_schema() -> dict:
    C = W.catalog.contract
    return C._obj({
        "query": C._s("The query as given; null when video_ids were looked up.", "x402 payments", nullable=True),
        "video_ids": {"type": ["array", "null"], "items": {"type": "string"},
                      "description": "The ids asked for, in order; null when a query was searched.",
                      "examples": [["IJhS_fv5Ktk"]]},
        "total_results": C._i("YouTube's estimated total matches for the query (pageInfo.totalResults); null when not sent.",
                              19113, nullable=True),
        "items": C._arr(C._obj({
            "video_id": C._s("YouTube video id.", "IJhS_fv5Ktk"),
            "url": C._s("Watch URL.", "https://www.youtube.com/watch?v=IJhS_fv5Ktk"),
            "title": C._s("Title.", "What is x402? Internet-native payments for agents", nullable=True),
            "description": C._s("Description, cut at 500 characters; null when empty.",
                                "A short intro to the x402 payment protocol.", nullable=True),
            "channel_id": C._s("Uploading channel id.", "UCoBPd2jgYzQ5m8k3f1nR9wA", nullable=True),
            "channel_title": C._s("Uploading channel name.", "Coinbase Developer Platform", nullable=True),
            "published_at": C._s("Upload time as YouTube states it (ISO 8601).", "2026-05-14T16:00:12Z", nullable=True),
            "duration_seconds": C._i("Length in whole seconds from contentDetails.duration; 0 for a live or upcoming "
                                     "stream; null when YouTube did not return the video's details.", 754, nullable=True),
            "view_count": C._i("Views; null when YouTube does not send it.", 52004, nullable=True),
            "like_count": C._i("Likes; null when the uploader hides them.", 2225, nullable=True),
            "comment_count": C._i("Comments; null when hidden or comments are off.", 112, nullable=True),
            "live": {"type": ["boolean", "null"],
                     "description": "Live or upcoming broadcast (liveBroadcastContent is not none); null when not stated.",
                     "examples": [False]},
            "thumbnail_url": C._s("Thumbnail URL, the high size when YouTube offers it.",
                                  "https://i.ytimg.com/vi/IJhS_fv5Ktk/hqdefault.jpg", nullable=True),
        }, ["video_id", "url", "title", "description", "channel_id", "channel_title", "published_at",
            "duration_seconds", "view_count", "like_count", "comment_count", "live", "thumbnail_url"],
            "One video, with its live statistics."),
            "Videos, in YouTube's search order (or the order of the ids asked); ids YouTube does not know are absent."),
        "item_count": C._i("Videos returned.", 3),
        "source": C._const("youtube-data-api", "Data source."),
        "as_of": C._s("The same instant as checked_at: YouTube does not stamp its statistics.", "2026-09-27T12:00:00Z"),
        "checked_at": C._CHECKED_AT,
    }, ["query", "video_ids", "total_results", "items", "item_count", "source", "as_of", "checked_at"])


def _row():
    K = W.catalog
    return K.Worker(
        name="video.youtube", price_usd=0.05, tier="utility",
        title="YouTube search and video statistics, live",
        description=(
            'Search YouTube or look up videos by id, each with live statistics read at call '
            'time from the YouTube Data API v3: title, channel, upload time, duration, '
            "views, likes, comments, live flag and thumbnail. A query runs YouTube's own "
            'video search (sort by relevance, date, views, rating or title; filter by '
            'upload date, country, language) plus one details call; video_ids skips it. '
            'Input: query or video_ids; optional max_results, order, published_after, '
            'region_code, language.'),
        tags=["youtube", "video", "search", "social", "live", "statistics"],
        input_schema=dict(K._obj({
            "query": {"type": "string", "minLength": 1, "maxLength": 300,
                      "description": "Words to search YouTube for, in any language. Use this OR `video_ids`."},
            "video_ids": {"type": "array", "items": {"type": "string", "pattern": "^[A-Za-z0-9_-]{11}$"},
                          "minItems": 1, "maxItems": 25,
                          "description": "YouTube video ids (11 characters) to look up directly; skips the search."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 25,
                            "description": "With query: videos to return, default 10."},
            "order": {"type": "string", "enum": ["relevance", "date", "viewCount", "rating", "title"],
                      "description": "With query: YouTube's sort, default relevance."},
            "published_after": {"type": "string", "format": "date-time",
                                "description": "With query: only videos uploaded at or after this RFC 3339 time, e.g. 2026-01-01T00:00:00Z."},
            "region_code": {"type": "string", "minLength": 2, "maxLength": 2,
                            "description": "With query: ISO 3166-1 alpha-2 country whose results to prefer, e.g. US, JP."},
            "language": {"type": "string", "pattern": "^[A-Za-z]{2,3}$",
                         "description": "With query: ISO 639 language code of the results to prefer, e.g. en, ja."},
        }, []), oneOf=[{"required": ["query"]}, {"required": ["video_ids"]}]),
        returns=("query, video_ids, total_results, items[{video_id, url, title, description, channel_id, "
                 "channel_title, published_at, duration_seconds, view_count, like_count, comment_count, live, "
                 "thumbnail_url}], item_count, source, as_of, checked_at."),
        skill="video.youtube", max_seconds=60,
        pricing_basis=("Provisional. No monetary provider cost: the YouTube Data API is quota-metered "
                       "(100 units per search, 1 per details call, of a 10,000-unit daily default); two "
                       "requests at most."),
        requires=("youtube",), output_schema=_output_schema())


# --- fakes ------------------------------------------------------------------------

def _stub_get(monkeypatch, responses: dict, calls: list = None):
    """responses: {path: (status, json)}; every call is appended to `calls`."""
    async def fake(path, params):
        if calls is not None:
            calls.append((path, dict(params)))
        if path not in responses:
            raise AssertionError(f"unexpected YouTube call {path}")
        return responses[path]
    monkeypatch.setattr(YT, "_get_json", fake)


def _live_provider(monkeypatch, responses: dict, calls: list = None):
    monkeypatch.setenv("WORKER_YOUTUBE_API_KEY", "test-key")
    _stub_get(monkeypatch, responses, calls)
    return YT.PROVIDERS[0]


class _Ctx:
    """Serves the `search` and `videos` steps from fixture values; records what ran."""

    def __init__(self, search=None, videos=None):
        self.search_value, self.videos_value = search, videos
        self.steps, self.providers_used, self.attempts, self.calls = [], [], [], []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        assert providers is YT.PROVIDERS, "the skill must run its steps over the youtube provider list"
        self.steps.append(step)
        outer = self

        class _Provider:
            id = "fixture"

            def available(self):
                return True

            async def search(self, query, **options):
                outer.calls.append(("search", query, options))
                if isinstance(outer.search_value, Exception):
                    raise outer.search_value
                return W.runtime.ProviderResult(value=outer.search_value, cost_micros=0, cost_measured=True)

            async def videos(self, ids):
                outer.calls.append(("videos", list(ids)))
                if isinstance(outer.videos_value, Exception):
                    raise outer.videos_value
                return W.runtime.ProviderResult(value=outer.videos_value, cost_micros=0, cost_measured=True)

        result = await call(_Provider())
        self.providers_used.append("fixture")
        return result.value


def _parsed(monkeypatch):
    """The provider's own parse of both captured responses."""
    provider = _live_provider(monkeypatch, {"search": (200, SEARCH_RESPONSE), "videos": (200, VIDEOS_RESPONSE)})
    found = asyncio.run(provider.search("x402 payments", max_results=3)).value
    details = asyncio.run(provider.videos(found["video_ids"])).value
    return found, details


# --- the provider, parsed from captured responses ---------------------------------

def test_iso_8601_durations():
    parse = YT.parse_duration
    assert parse("PT1H2M3S") == 3723 and parse("PT45S") == 45 and parse("PT12M34S") == 754
    assert parse("P0D") == 0  # a live or upcoming stream
    assert parse("P1DT2H") == 93600 and parse("PT2H") == 7200 and parse("P1W") == 604800
    assert parse(None) is None and parse("") is None and parse("PT") is None and parse("12:34") is None


def test_statistics_are_strings_and_hidden_counts_are_null():
    count = YT._count
    assert count("52004") == 52004 and count(7) == 7
    assert count(None) is None and count("") is None and count("N/A") is None and count(True) is None
    record = YT.video_record("aB3dE5fG7hI", VIDEOS_RESPONSE["items"][1]["snippet"],
                             VIDEOS_RESPONSE["items"][1]["statistics"], VIDEOS_RESPONSE["items"][1]["contentDetails"])
    assert record["view_count"] == 1503 and record["like_count"] is None and record["comment_count"] is None
    assert record["live"] is True and record["duration_seconds"] == 0 and record["description"] is None
    assert record["thumbnail_url"] == "https://i.ytimg.com/vi/aB3dE5fG7hI/default.jpg"  # no high size offered
    # a search-only record: statistics and duration unknown, never invented
    bare = YT.video_record("zZ9yY8xX7wW", SEARCH_RESPONSE["items"][2]["snippet"])
    assert bare["title"] == "x402 payments walkthrough" and bare["url"] == "https://www.youtube.com/watch?v=zZ9yY8xX7wW"
    assert bare["view_count"] is None and bare["like_count"] is None and bare["duration_seconds"] is None
    assert bare["live"] is False and bare["thumbnail_url"].endswith("/zZ9yY8xX7wW/mqdefault.jpg")
    assert YT.video_record("zZ9yY8xX7wW", {})["live"] is None and YT.video_record("zZ9yY8xX7wW", {})["title"] is None


def test_search_is_parsed_from_the_captured_shape_and_sends_every_filter(monkeypatch):
    calls = []
    provider = _live_provider(monkeypatch, {"search": (200, SEARCH_RESPONSE)}, calls)
    result = asyncio.run(provider.search("x402 payments", max_results=3, order="date",
                                         published_after="2026-01-01T00:00:00Z", region_code="JP",
                                         relevance_language="ja"))
    assert result.cost_micros == 0 and result.cost_measured is True and result.usage == "quota_units=100 results=3"
    value = result.value
    assert value["query"] == "x402 payments" and value["total_results"] == 19113 and value["region_code"] == "US"
    assert value["video_ids"] == IDS  # the channel hit is skipped, order kept
    assert value["snippets"]["IJhS_fv5Ktk"]["channelTitle"] == "Coinbase Developer Platform"
    path, params = calls[0]
    assert path == "search"
    assert params == {"part": "snippet", "q": "x402 payments", "type": "video", "maxResults": "3", "order": "date",
                      "key": "test-key", "publishedAfter": "2026-01-01T00:00:00Z", "regionCode": "JP",
                      "relevanceLanguage": "ja"}


def test_videos_are_parsed_in_one_call_with_statistics_and_duration(monkeypatch):
    calls = []
    provider = _live_provider(monkeypatch, {"videos": (200, VIDEOS_RESPONSE)}, calls)
    result = asyncio.run(provider.videos(IDS))
    assert result.usage == "quota_units=1 results=2" and result.cost_micros == 0 and result.cost_measured is True
    assert len(calls) == 1 and calls[0][0] == "videos"
    assert calls[0][1] == {"part": "snippet,statistics,contentDetails", "id": ",".join(IDS), "key": "test-key"}
    value = result.value
    assert value["total_results"] == 2 and [i["video_id"] for i in value["items"]] == ["IJhS_fv5Ktk", "aB3dE5fG7hI"]
    first = value["items"][0]
    assert first["url"] == "https://www.youtube.com/watch?v=IJhS_fv5Ktk"
    assert first["title"] == "What is x402? Internet-native payments for agents"
    assert len(first["description"]) == 500 and first["description"] == LONG_DESCRIPTION[:500]
    assert first["channel_id"] == "UCoBPd2jgYzQ5m8k3f1nR9wA" and first["channel_title"] == "Coinbase Developer Platform"
    assert first["published_at"] == "2026-05-14T16:00:12Z" and first["duration_seconds"] == 754
    assert first["view_count"] == 52004 and first["like_count"] == 2225 and first["comment_count"] == 112
    assert first["live"] is False and first["thumbnail_url"] == "https://i.ytimg.com/vi/IJhS_fv5Ktk/hqdefault.jpg"


def test_unknown_ids_are_an_empty_delivery_not_a_failure(monkeypatch):
    provider = _live_provider(monkeypatch, {"videos": (404, {"error": {"code": 404, "message": "Not Found"}})})
    assert asyncio.run(provider.videos(["zZ9yY8xX7wW"])).value == {"total_results": 0, "items": []}
    provider = _live_provider(monkeypatch, {"videos": (200, {"kind": "youtube#videoListResponse", "items": [],
                                                             "pageInfo": {"totalResults": 0, "resultsPerPage": 0}})})
    assert asyncio.run(provider.videos(["zZ9yY8xX7wW"])).value == {"total_results": 0, "items": []}


def test_quota_and_key_errors_are_this_deployments_and_a_400_is_the_callers(monkeypatch):
    provider = _live_provider(monkeypatch, {"search": (403, QUOTA_403)})
    with pytest.raises(W.runtime.ProviderUnavailable) as exc:
        asyncio.run(provider.search("x"))
    assert "quotaExceeded" in exc.value.detail
    provider = _live_provider(monkeypatch, {"search": (403, KEY_INVALID_403)})
    with pytest.raises(W.runtime.ProviderUnavailable) as exc:
        asyncio.run(provider.search("x"))
    assert "keyInvalid" in exc.value.detail
    provider = _live_provider(monkeypatch, {"videos": (400, KEY_INVALID_400)})
    with pytest.raises(W.runtime.ProviderUnavailable) as exc:
        asyncio.run(provider.videos(IDS))
    assert "API_KEY_INVALID" in exc.value.detail
    provider = _live_provider(monkeypatch, {"search": (400, BAD_PARAMETER_400)})
    with pytest.raises(W.runtime.InvalidRequest) as exc:
        asyncio.run(provider.search("x"))
    assert "Invalid value 'views'" in exc.value.detail
    provider = _live_provider(monkeypatch, {"search": (200, {"kind": "youtube#searchListResponse"})})
    with pytest.raises(W.runtime.InvalidProviderResponse):
        asyncio.run(provider.search("x"))


def test_429_and_5xx_are_transient_and_non_json_is_invalid(monkeypatch):
    monkeypatch.setenv("WORKER_YOUTUBE_API_KEY", "test-key")

    class _Response:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            if self._body is None:
                raise ValueError("not json")
            return self._body

    def client_answering(status, body):
        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, params=None, headers=None):
                assert url == "https://www.googleapis.com/youtube/v3/search" and params["key"] == "test-key"
                return _Response(status, body)
        return _Client

    for status in (429, 500, 502, 503, 504):
        monkeypatch.setattr(YT.httpx, "AsyncClient", client_answering(status, {}))
        with pytest.raises(W.runtime.TransientProviderError):
            asyncio.run(YT._get_json("search", {"key": "test-key"}))
    monkeypatch.setattr(YT.httpx, "AsyncClient", client_answering(200, None))
    with pytest.raises(W.runtime.InvalidProviderResponse):
        asyncio.run(YT._get_json("search", {"key": "test-key"}))
    monkeypatch.setattr(YT.httpx, "AsyncClient", client_answering(200, SEARCH_RESPONSE))
    assert asyncio.run(YT._get_json("search", {"key": "test-key"})) == (200, SEARCH_RESPONSE)


def test_the_provider_is_off_without_the_key_and_never_calls_out(monkeypatch):
    monkeypatch.delenv("WORKER_YOUTUBE_API_KEY", raising=False)
    _stub_get(monkeypatch, {})  # any call would fail loudly
    provider = YT.PROVIDERS[0]
    assert provider.id == "youtube-data-api" and provider.available() is False
    assert "WORKER_YOUTUBE_API_KEY" in provider.unavailable_reason()
    with pytest.raises(W.runtime.ProviderUnavailable):
        asyncio.run(provider.search("x402"))
    with pytest.raises(W.runtime.ProviderUnavailable):
        asyncio.run(provider.videos(IDS))
    assert _row().available() is False and "WORKER_YOUTUBE_API_KEY" in _row().unavailable_reason()
    monkeypatch.setenv("WORKER_YOUTUBE_API_KEY", " AIza-test ")
    assert provider.available() is True and provider.unavailable_reason() == "" and _row().available() is True


# --- the skill ----------------------------------------------------------------------

def test_exactly_one_of_query_or_video_ids_and_precise_ranges():
    P = V.parse
    req = P({"query": " x402 payments ", "max_results": 3, "order": "date", "published_after": "2026-01-01T00:00:00Z",
             "region_code": "jp", "language": "JA"})
    assert req == {"query": "x402 payments", "video_ids": None, "max_results": 3, "order": "date",
                   "published_after": "2026-01-01T00:00:00Z", "region_code": "JP", "language": "ja"}
    assert P({"query": "x"})["max_results"] == 10 and P({"query": "x"})["order"] == "relevance"
    req = P({"video_ids": ["IJhS_fv5Ktk", "aB3dE5fG7hI", "IJhS_fv5Ktk"]})
    assert req["video_ids"] == ["IJhS_fv5Ktk", "aB3dE5fG7hI"] and req["query"] is None
    for bad in ({}, {"query": "x", "video_ids": ["IJhS_fv5Ktk"]}, {"query": " "}, {"query": 5}, {"query": "x" * 301},
                {"query": "x", "max_results": 0}, {"query": "x", "max_results": 26}, {"query": "x", "max_results": True},
                {"query": "x", "max_results": "3"}, {"query": "x", "order": "views"}, {"query": "x", "order": "VIEWCOUNT"},
                {"query": "x", "published_after": "yesterday"}, {"query": "x", "published_after": "2026-01-01"},
                {"query": "x", "published_after": "2026-13-01T00:00:00Z"}, {"query": "x", "region_code": "USA"},
                {"query": "x", "region_code": "J1"}, {"query": "x", "language": "english"}, {"query": "x", "language": "pt-BR"},
                {"video_ids": []}, {"video_ids": "IJhS_fv5Ktk"}, {"video_ids": ["short"]}, {"video_ids": ["IJhS_fv5Ktk!"]},
                {"video_ids": ["IJhS_fv5Ktk", 7]}, {"video_ids": ["A" * 11] * 26}, {"video_ids": ["IJhS_fv5Ktk"], "order": "date"},
                {"video_ids": ["IJhS_fv5Ktk"], "max_results": 5}, "not a body"):
        with pytest.raises(W.runtime.InvalidRequest):
            V.precheck(bad)
    assert V.PRECHECKS == {"video.youtube": V.precheck} and V.SKILLS == {"video.youtube": V.search}


def test_a_query_runs_search_then_one_videos_call_in_search_order(monkeypatch):
    found, details = _parsed(monkeypatch)
    ctx = _Ctx(search=found, videos=details)
    r = asyncio.run(V.search(ctx, SEED))
    assert ctx.steps == ["search", "videos"] and ctx.providers_used == ["fixture", "fixture"]
    assert ctx.calls[0] == ("search", "x402 payments", {"max_results": 3, "order": "relevance", "published_after": None,
                                                        "region_code": None, "relevance_language": None})
    assert ctx.calls[1] == ("videos", IDS)
    assert r["query"] == "x402 payments" and r["video_ids"] is None and r["total_results"] == 19113
    assert [i["video_id"] for i in r["items"]] == IDS and r["item_count"] == 3
    first, live, gone = r["items"]
    assert first["view_count"] == 52004 and first["like_count"] == 2225 and first["duration_seconds"] == 754
    assert first["description"] == LONG_DESCRIPTION[:500]  # the full description from videos.list, cut at 500
    assert live["live"] is True and live["like_count"] is None and live["comment_count"] is None and live["duration_seconds"] == 0
    # the hit videos.list no longer returned is still listed, from its search snippet, with nulls
    assert gone["title"] == "x402 payments walkthrough" and gone["view_count"] is None and gone["duration_seconds"] is None
    assert r["source"] == "youtube-data-api" and r["checked_at"].endswith("Z") and r["as_of"] == r["checked_at"]
    assert W.catalog.contract.check(_output_schema(), r) is None


def test_no_search_hits_means_no_videos_call_and_an_empty_result():
    ctx = _Ctx(search={"query": "zzqx", "total_results": 0, "video_ids": [], "snippets": {}, "region_code": "US"})
    r = asyncio.run(V.search(ctx, {"query": "zzqx"}))
    assert ctx.steps == ["search"] and r["items"] == [] and r["item_count"] == 0 and r["total_results"] == 0
    assert W.catalog.contract.check(_output_schema(), r) is None


def test_video_ids_skip_the_search_and_unknown_ids_are_absent(monkeypatch):
    _, details = _parsed(monkeypatch)
    ctx = _Ctx(videos=details)
    r = asyncio.run(V.search(ctx, {"video_ids": ["aB3dE5fG7hI", "zZ9yY8xX7wW", "IJhS_fv5Ktk"]}))
    assert ctx.steps == ["videos"] and ctx.calls == [("videos", ["aB3dE5fG7hI", "zZ9yY8xX7wW", "IJhS_fv5Ktk"])]
    assert r["query"] is None and r["video_ids"] == ["aB3dE5fG7hI", "zZ9yY8xX7wW", "IJhS_fv5Ktk"]
    assert [i["video_id"] for i in r["items"]] == ["aB3dE5fG7hI", "IJhS_fv5Ktk"]  # the order asked; the private one absent
    assert r["item_count"] == 2 and r["total_results"] == 2
    assert W.catalog.contract.check(_output_schema(), r) is None
    empty = asyncio.run(V.search(_Ctx(videos={"total_results": 0, "items": []}), {"video_ids": ["zZ9yY8xX7wW"]}))
    assert empty["items"] == [] and empty["item_count"] == 0 and empty["total_results"] == 0


def test_a_provider_failure_is_not_swallowed():
    ctx = _Ctx(search=W.runtime.ProviderUnavailable("YouTube Data API quota exhausted on this deployment (quotaExceeded)"))
    with pytest.raises(W.runtime.ProviderUnavailable):
        asyncio.run(V.search(ctx, SEED))


# --- the row and the contract, as they will be integrated -----------------------------

def test_the_row_is_priced_utility_keyed_and_always_current():
    row = _row()
    assert row.path == "/work/video/youtube" and row.price_usd == 0.05 and row.tier == "utility"
    assert row.skill == "video.youtube" and row.requires == ["youtube"] and row.max_seconds == 60
    assert len(row.description) <= 480, len(row.description)
    assert row.tool_name == "work_video_youtube"
    schema = row.output_schema
    assert "checked_at" in schema["required"] and schema["properties"]["checked_at"]["format"] == "date-time"
    assert "as_of" in schema["required"] and "items" in schema["required"]
    item = schema["properties"]["items"]["items"]
    assert set(item["required"]) == set(item["properties"]) == {
        "video_id", "url", "title", "description", "channel_id", "channel_title", "published_at",
        "duration_seconds", "view_count", "like_count", "comment_count", "live", "thumbnail_url"}
    # the example generated from the schema satisfies the schema (what the docs and the Bazaar record publish)
    example = W.catalog.contract.example_from_schema(schema)
    assert W.catalog.contract.check(schema, example) is None and example["source"] == "youtube-data-api"


def test_the_input_schema_sells_exactly_one_of_query_or_video_ids():
    schema = _row().input_schema
    jsonschema.validate(SEED, schema)
    jsonschema.validate({"video_ids": ["IJhS_fv5Ktk", "aB3dE5fG7hI"]}, schema)
    jsonschema.validate({"query": "x402", "order": "viewCount", "region_code": "JP", "language": "ja",
                         "published_after": "2026-01-01T00:00:00Z", "max_results": 25}, schema)
    for bad in ({}, {"query": "x", "video_ids": ["IJhS_fv5Ktk"]}, {"query": ""}, {"video_ids": ["nope"]},
                {"query": "x", "order": "views"}, {"query": "x", "max_results": 26}, {"query": "x", "extra": 1}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)
    # every body the schema accepts, the precheck accepts; every body it refuses, the precheck refuses
    for body in (SEED, {"video_ids": ["IJhS_fv5Ktk"]}):
        V.precheck(body)
