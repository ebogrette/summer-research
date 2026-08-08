# Keyword Scrub — Implementation Plan

A keyword-driven scraping pipeline over 4chan, Reddit, and X/Twitter, exposed as a
Flask JSON API. Each platform is an isolated adapter module; all adapters emit the
same normalized `Post` record.

---

## 1. Reality check on the three sources

The single most important design constraint is that these platforms do not offer
comparable access. Pretending they do is how this kind of project rots.

| | Access path | Auth | Native keyword search? | Practical limit |
|---|---|---|---|---|
| **4chan** | Official read-only JSON API (`a.4cdn.org`) | none | **No** | ~1 req/sec, `If-Modified-Since` required by their rules |
| **Reddit** | Official API (OAuth2, script app) | client id/secret | **Yes** (`/search`, `/r/{sub}/search`) | ~100 req/min per client, ~1000-result pagination ceiling |
| **X/Twitter** | API v2 recent search | paid tier bearer token | **Yes** | tier-gated volume; free tier does **not** include search |

Consequences that drive the architecture:

- **4chan needs crawl-then-filter.** There is no server-side search. You pull the
  board catalog, pull threads, and match keywords locally. This is slow and I/O
  bound, which is why the API needs an async job mode (§6.3), not just synchronous
  request/response.
- **4chan content is ephemeral.** Threads are pruned within hours to days. If you
  care about history, you must either poll continuously and persist, or read from a
  third-party archive (4plebs, desuarchive) which *does* offer a search endpoint
  (FoolFuuka API) but only covers a subset of boards. Plan for a
  `FourChanArchiveAdapter` as a sibling module, not a rewrite.
- **X/Twitter is the risk item.** There is no free, ToS-compliant, reliable path to
  keyword search. Verify current API pricing before committing — it has changed
  repeatedly. Design the module as a thin interface over a swappable backend
  (§5.3), ship it disabled by default, and let the API report it as unavailable
  rather than failing the whole request. Everything else works without it.
- **Respect robots/ToS and rate limits.** Send a real User-Agent, honor
  `Retry-After` and 429s, keep per-source concurrency low. This is both correct and
  the only way the pipeline stays working.

---

## 2. Repository layout

```
keyword-scrub/
├── pyproject.toml
├── .env.example
├── README.md
├── PLAN.md
├── src/keyword_scrub/
│   ├── __init__.py
│   ├── config.py              # env-backed settings, per-source credentials
│   ├── models.py              # Post, SearchQuery, SearchResult, SourceInfo
│   ├── matching.py            # keyword matcher (client-side filtering)
│   ├── errors.py              # SourceError, RateLimited, AuthError, NotConfigured
│   ├── http.py                # shared client: retries, backoff, UA, caching
│   ├── ratelimit.py           # per-source token bucket
│   ├── registry.py            # adapter discovery + capability reporting
│   ├── pipeline.py            # fan-out, merge, dedupe, sort, paginate
│   ├── sources/
│   │   ├── base.py            # SourceAdapter protocol + SearchCapability enum
│   │   ├── fourchan.py
│   │   ├── reddit.py
│   │   └── twitter.py
│   ├── store/
│   │   ├── __init__.py
│   │   ├── schema.sql
│   │   └── sqlite.py          # optional persistence + job state
│   └── api/
│       ├── __init__.py        # create_app() factory
│       ├── routes.py
│       ├── schemas.py         # request validation, response serialization
│       └── jobs.py            # async job endpoints
└── tests/
    ├── fixtures/              # captured API payloads per source
    ├── test_models.py
    ├── test_matching.py
    ├── test_sources_*.py      # one per adapter, fixture-driven, no network
    ├── test_pipeline.py
    └── test_api.py
```

**Do not build on the existing `.venv`** — it is Python 3.9, which rules out `X | Y`
union syntax and several stdlib niceties. Create a fresh 3.11+ environment.

---

## 3. The normalized record

This is the contract every adapter satisfies and the only shape the API emits.
Get this right before writing any adapter.

```python
# models.py
@dataclass(frozen=True, slots=True)
class Post:
    # identity
    source: str                     # "4chan" | "reddit" | "twitter"
    id: str                         # platform-native id, always str
    url: str                        # canonical permalink

    # threading
    thread_id: str | None           # root container id (OP no. / submission id / conversation id)
    parent_id: str | None           # direct parent, None if root
    container: str | None           # board ("pol"), subreddit ("r/news"), None for tweets

    # content
    title: str | None               # 4chan subject, reddit title, None for tweets/comments
    body: str                       # plaintext, HTML/markdown stripped, entities decoded
    author: str | None              # None or "Anonymous" on 4chan
    lang: str | None

    # timestamps — always tz-aware UTC
    created_at: datetime
    fetched_at: datetime

    # engagement (None where the platform doesn't expose it)
    score: int | None               # upvotes / likes
    reply_count: int | None

    # media
    media: list[MediaRef]           # url, kind, thumbnail_url, width/height

    # search provenance
    matched_keywords: list[str]
    match_field: str                # "title" | "body" | "both" | "native"

    # escape hatch — omitted from JSON unless ?include_raw=true
    raw: dict = field(repr=False, default_factory=dict)
```

Normalization rules, applied inside each adapter, never in the API layer:

- **Time**: everything to tz-aware UTC `datetime`. 4chan gives unix `time`, Reddit
  gives float `created_utc`, X gives ISO-8601. Serialize as ISO-8601 with `Z`.
- **Body**: 4chan comments are HTML (`<br>`, `<span class="quote">`, `<a>` backlinks)
  — strip to text, preserve quote lines as `>text`. Reddit — use `selftext` /
  `body`, not `_html`. X — expand `t.co` links from `entities` before storing.
- **IDs**: always strings, never ints, even for 4chan post numbers.
- **Globally unique key**: `f"{source}:{id}"` — used for dedupe.
- **Missing vs zero**: use `None` for "platform doesn't expose this", `0` for
  "genuinely zero". Do not paper over the difference.

`SearchQuery` is the inbound shape:

```python
@dataclass(frozen=True)
class SearchQuery:
    keywords: list[str]
    mode: Literal["any", "all", "phrase"] = "any"
    sources: list[str] | None = None          # None = all enabled
    containers: list[str] | None = None       # boards / subreddits / None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 100                          # per-source cap
    include_replies: bool = True
    case_sensitive: bool = False
```

---

## 4. The adapter contract

```python
# sources/base.py
class SearchCapability(StrEnum):
    NATIVE = "native"          # platform searches server-side (reddit, twitter)
    CRAWL_FILTER = "crawl"     # we fetch broadly and filter locally (4chan)

class SourceAdapter(Protocol):
    name: str
    capability: SearchCapability
    requires_auth: bool

    def is_configured(self) -> bool: ...
    def describe(self) -> SourceInfo: ...          # for GET /sources
    def search(self, q: SearchQuery) -> Iterator[Post]: ...
```

`search()` **yields** rather than returning a list, so the pipeline can apply a
global cap and stop early — this matters enormously for 4chan, where a naive
implementation walks every thread on a board.

Every adapter raises only the typed errors in `errors.py`. Raw `httpx` /
`requests` exceptions never escape a module — that's what keeps one flaky source
from taking down the whole query.

---

## 5. Per-source modules

### 5.1 `sources/fourchan.py` — capability: `CRAWL_FILTER`

Endpoints (all under `https://a.4cdn.org`):

- `/boards.json` — board list + metadata; cache for hours
- `/{board}/catalog.json` — every thread on the board with a preview of replies
- `/{board}/thread/{no}.json` — full thread with all posts

Algorithm:

1. Resolve target boards: `q.containers` if given, else a configured default set.
   Refuse to crawl all ~70 boards implicitly — that is minutes of work per request.
2. Fetch each board catalog. Match keywords against OP `sub` + `com` and the
   preview replies already present in the catalog.
3. For threads with a catalog-level hit, or (if `deep_scan` is on) for all threads,
   fetch the full thread and match every post.
4. Yield matching posts, capped at `q.limit`.

Implementation notes:

- **Sequence the requests.** ~1 req/sec, single connection. Use the shared token
  bucket; do not parallelize 4chan.
- **Send `If-Modified-Since`** and treat 304 as "reuse cached copy". Their API rules
  ask for this explicitly and it cuts traffic dramatically on repeat polls.
- Media URLs are built, not given: `https://i.4cdn.org/{board}/{tim}{ext}`, thumb is
  `{tim}s.jpg`. Post permalink: `https://boards.4chan.org/{board}/thread/{no}#p{id}`.
- Threading: OP has no `resto`; replies have `resto` = thread number. Map
  `resto == 0` → `parent_id = None`, `thread_id = no`.
- `sub`/`com` are optional keys — a post with an image and no text has neither.
  Default to `""`, never `KeyError`.
- Catalog-only mode should be the default for API-facing requests; deep scan belongs
  in the job queue.

### 5.2 `sources/reddit.py` — capability: `NATIVE`

Auth: OAuth2 client-credentials ("script" app) against
`https://www.reddit.com/api/v1/access_token`, then all calls to `oauth.reddit.com`
with a descriptive `User-Agent` (Reddit actively throttles generic ones). Cache the
token and refresh on expiry.

Use raw HTTP via the shared client rather than PRAW — PRAW brings its own session,
rate limiter, and lazy-loading model that fight the pipeline's. The endpoints are
simple enough that the dependency isn't worth it.

- `/search?q=&sort=&t=&limit=&after=` — sitewide
- `/r/{sub}/search?restrict_sr=1&...` — scoped
- `/comments/{id}` — comment tree, only when `include_replies` is set

Notes:

- Translate `SearchQuery.mode` into Reddit's syntax: `any` → `a OR b`,
  `all` → `a AND b`, `phrase` → `"a b"`.
- Reddit's search does not honor arbitrary date ranges well; pass the coarse `t`
  param (`hour`/`day`/`week`/…) and then filter precisely against `since`/`until`
  locally.
- Paginate with the `after` fullname cursor. Stop at `q.limit` or when `after` is
  null. Be aware of the ~1000-item listing ceiling.
- Comments have no `title`; submissions have no `parent_id`. Set `match_field` to
  `"native"` since the platform did the matching, but still populate
  `matched_keywords` by re-running the local matcher over the returned text — the
  API needs to tell the caller *why* a result matched.

### 5.3 `sources/twitter.py` — capability: `NATIVE`, disabled by default

Structure this as an interface with one real implementation and a clear seam:

```python
class TwitterBackend(Protocol):
    def recent_search(self, query: str, since, until, limit) -> Iterator[dict]: ...

class ApiV2Backend:   # official, requires paid bearer token
class NullBackend:    # default; is_configured() -> False
```

- `GET /2/tweets/search/recent` with `tweet.fields=created_at,public_metrics,lang,
  conversation_id,referenced_tweets,entities` and
  `expansions=author_id,attachments.media_keys`.
- The response splits users and media into `includes` — the adapter must join them
  back onto each tweet before building `Post`. This join is the bulk of the module.
- `conversation_id` → `thread_id`; `referenced_tweets[type=replied_to].id` →
  `parent_id`; `public_metrics.like_count` → `score`; `reply_count` → `reply_count`.
- Honor `x-rate-limit-remaining` / `x-rate-limit-reset` headers, not just 429s.
- When unconfigured, `is_configured()` returns False and the pipeline reports the
  source as `unavailable` in the response envelope. It never raises.

**Before building this**, confirm the current API tier that includes recent search
and what it costs. If it isn't viable, the fallback is to keep `NullBackend` in
place and document the seam — the other two sources are fully functional without it.

---

## 6. Pipeline and API

### 6.1 `pipeline.py`

```python
def run_search(q: SearchQuery, registry: Registry) -> SearchResult
```

1. Resolve requested sources against those enabled and configured.
2. Fan out with a `ThreadPoolExecutor`, one worker per source (these are I/O-bound
   HTTP calls; threads are the right tool and keep Flask simple).
3. Per-source timeout. A source that times out or errors contributes an entry to
   `result.errors` — it does not fail the request.
4. Merge, dedupe on `source:id`, sort by `created_at` desc (or `score`), apply the
   global limit.
5. Return `SearchResult(posts, per_source_counts, errors, query_echo, elapsed_ms)`.

### 6.2 Endpoints

```
GET  /health                → {"status":"ok","version":...}
GET  /sources               → capability + configured status per adapter
GET  /search?q=...          → synchronous search (see params below)
POST /search                → same, JSON body, for complex/boolean queries
POST /jobs                  → enqueue a long crawl, returns job id
GET  /jobs/<id>             → status + results when done
GET  /jobs/<id>/results     → paginated results
```

`GET /search` params: `q` (required, comma-separated or repeated), `mode`,
`sources`, `containers`, `since`, `until`, `limit`, `sort`, `include_raw`,
`include_replies`.

Response envelope:

```json
{
  "query": { "keywords": ["..."], "mode": "any", "...": "..." },
  "count": 42,
  "posts": [ { "source": "reddit", "id": "...", "...": "..." } ],
  "sources": {
    "reddit":  { "status": "ok", "count": 40, "elapsed_ms": 812 },
    "4chan":   { "status": "ok", "count": 2,  "elapsed_ms": 3140 },
    "twitter": { "status": "unavailable", "reason": "not_configured" }
  },
  "elapsed_ms": 3160
}
```

Always returning a per-source status block is what makes partial failure legible
instead of mysterious.

Errors: consistent JSON shape `{"error": {"code","message","details"}}` with 400 for
validation, 429 when the caller trips the app's own limiter, 502 when every source
failed, 200 with populated `errors` when only some did.

### 6.3 Async jobs

Deep 4chan crawls take minutes and will blow past any sane HTTP timeout. Phase 6
adds a job mode. Start with a `ThreadPoolExecutor` + SQLite job table — that is
sufficient for single-process use and avoids a Redis/Celery dependency until there's
a reason for one. Keep the job interface narrow enough that swapping in RQ or Celery
later is a contained change.

---

## 7. Cross-cutting concerns

- **Config** (`config.py`): pydantic-settings or plain dataclass over `os.environ`.
  Keys: `REDDIT_CLIENT_ID/SECRET/USER_AGENT`, `TWITTER_BEARER_TOKEN`,
  `FOURCHAN_DEFAULT_BOARDS`, `HTTP_TIMEOUT`, `CACHE_TTL`, `DATABASE_URL`.
  Ship `.env.example`; never commit real credentials. Add `.env` to `.gitignore`.
- **HTTP** (`http.py`): one `httpx.Client` with connection pooling, per-source
  headers, exponential backoff with jitter on 5xx/429, `Retry-After` honored,
  hard timeout. Every outbound request goes through here.
- **Rate limiting** (`ratelimit.py`): per-source token bucket — 4chan 1 rps,
  Reddit ~90 rpm (under the 100 ceiling), Twitter per-tier.
- **Caching**: response cache keyed on URL + relevant headers, short TTL. Biggest
  win is 4chan catalogs during repeated polling.
- **Matching** (`matching.py`): precompile keywords into a single regex with word
  boundaries; support `any`/`all`/`phrase`, case folding, and Unicode
  normalization. Return which keywords hit, not just a boolean — that populates
  `matched_keywords`.
- **Logging**: structured (JSON) with a request id threaded through to each
  adapter call, so a slow source is identifiable in the logs.
- **Testing**: capture one real payload per endpoint into `tests/fixtures/` and
  drive all adapter tests off those with `respx`/`responses`. Zero network in CI.
  Add one opt-in live smoke test behind an env flag.

---

## 8. Build order

| Phase | Deliverable | Done when |
|---|---|---|
| **0** | Fresh 3.11 venv, `pyproject.toml`, package skeleton, `.env.example`, pytest wired | `pytest` runs green on an empty suite |
| **1** | `models.py`, `matching.py`, `errors.py`, `http.py`, `ratelimit.py`, `config.py` | `Post` round-trips to JSON; matcher tests pass |
| **2** | `sources/fourchan.py` + fixture tests | Catalog search over a board returns normalized `Post`s offline |
| **3** | `sources/reddit.py` + OAuth + fixture tests | Keyword search returns normalized `Post`s; token refresh covered |
| **4** | `registry.py`, `pipeline.py`, Flask app, `/health` `/sources` `/search` | `curl "localhost:5000/search?q=foo&sources=reddit,4chan"` returns the envelope |
| **5** | `sources/twitter.py` with `NullBackend` default | Unconfigured source reports `unavailable`; API backend swappable |
| **6** | SQLite store + `/jobs` async crawls | Deep 4chan scan runs as a job and results persist |
| **7** | Hardening: caller rate limits, pagination cursors, structured logs, Docker, README | End-to-end docs let someone else run it |

Phases 2–3 are independent and can be built in either order. Phase 4 is the first
point where the thing is genuinely usable — target that as the first milestone and
treat 5–7 as follow-on.

---

## 9. Decisions worth making before coding

1. **Persistence from day one, or later?** The plan defers it to Phase 6. If the
   real goal is longitudinal monitoring of 4chan (where content vanishes), pull
   SQLite forward to Phase 2 instead — retrofitting is more painful than starting
   with it.
2. **Default 4chan boards.** Crawling is expensive and unbounded; pick an explicit
   default set in config rather than "all".
3. **X/Twitter budget.** Confirm before Phase 5 whether a paid tier is on the table.
   If not, `NullBackend` is the permanent answer and that's worth knowing early.
4. **Auth on the Flask API itself.** Fine to skip for localhost; required before
   this is exposed anywhere. Simplest sufficient answer is a static API key header.
