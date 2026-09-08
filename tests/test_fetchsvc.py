"""Tests for the fetch service's contract shaping and its config diagnostics.

Everything here runs against the pure functions, so the suite needs neither
web-fetch nor a database. The two things worth protecting are:

1. the HTTP contract, because every project's CLAUDE.md depends on it, and
2. the "not configured" classification, because getting it wrong is what
   made an unset API key look like a hostile website for four months.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from orch.fetchsvc import diagnostics
from orch.fetchsvc.batch import BatchStore
from orch.fetchsvc.server import (
    HttpError,
    normalize_url,
    parse_options,
    render_result,
    to_jsonable,
    unknown_fields,
)


# The tier inventory web-fetch reports when ZYTE_API_KEY is unset or empty —
# the exact state that produced `blocked / escalation exhausted / attempts=[]`.
NO_ZYTE = {
    "configured": [{"tier": "direct", "number": 0, "is_browser": False}],
    "unconfigured": [
        {"tier": "zyte-http", "number": 2, "is_browser": False,
         "missing_credentials": ["zyte_api_key"], "missing_modules": []},
        {"tier": "zyte-browser", "number": 7, "is_browser": True,
         "missing_credentials": ["zyte_api_key"], "missing_modules": []},
        {"tier": "local-browser", "number": 6, "is_browser": True,
         "missing_credentials": [], "missing_modules": ["patchright"]},
    ],
}

FULLY_CONFIGURED = {
    "configured": [
        {"tier": "direct", "number": 0, "is_browser": False},
        {"tier": "zyte-browser", "number": 7, "is_browser": True},
    ],
    "unconfigured": [],
}


# ── contract: the documented response keys ──────────────────────────────────

# Exactly what the previous implementation returned, captured from the live
# service before the cutover. Losing any of these breaks callers.
CONTRACT_KEYS = {
    "request_id", "status", "url", "final_url", "cache_key", "tier_used",
    "cached", "fetched_at", "expires_at", "cost_usd", "status_code",
    "markdown", "chunks", "metadata", "chunk_count", "html", "attempts",
    "error_class", "error_message",
}


@dataclass
class _FakeResult:
    """Stand-in with web-fetch's FetchResult field set."""

    request_id: str = "r1"
    status: str = "done"
    url: str = "https://example.com/"
    final_url: str | None = "https://example.com/"
    cache_key: str = "abc"
    tier_used: str | None = "direct"
    cached: bool = False
    fetched_at: datetime = field(
        default_factory=lambda: datetime(2026, 9, 7, tzinfo=timezone.utc)
    )
    expires_at: datetime | None = None
    cost_usd: float = 0.0
    status_code: int | None = 200
    markdown: str = "# hi"
    chunks: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    chunk_count: int = 0
    html: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    error_class: str | None = None
    error_message: str | None = None
    binary: bool = False
    content_type: str | None = None
    body: bytes | None = None


def test_render_result_covers_the_documented_keys():
    payload = render_result(_FakeResult())
    assert CONTRACT_KEYS <= set(payload)


def test_render_result_drops_raw_bytes():
    # A PDF fetch carries megabytes on `body` for in-process consumers. It
    # must never reach the socket.
    payload = render_result(_FakeResult(binary=True, body=b"%PDF-1.7 ..."))
    assert "body" not in payload
    assert payload["binary"] is True


def test_render_result_serializes_datetimes():
    payload = render_result(_FakeResult())
    assert payload["fetched_at"] == "2026-09-07T00:00:00+00:00"


def test_to_jsonable_handles_nesting():
    assert to_jsonable({"a": [datetime(2026, 1, 1)], "b": (1, 2)}) == {
        "a": ["2026-01-01T00:00:00"], "b": [1, 2],
    }


# ── contract: request parsing ───────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("https://example.com", "https://example.com/"),
    ("HTTPS://Example.COM/Path", "https://example.com/Path"),
    ("https://example.com/a?b=c#d", "https://example.com/a?b=c#d"),
])
def test_normalize_url_matches_previous_behaviour(raw, expected):
    # The old service ran URLs through pydantic's HttpUrl, which lowercased
    # scheme/host and added the empty path. Callers see `url` echoed back, so
    # the normalization has to match or the response changes shape.
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "ftp://example.com", "not a url", None, 7])
def test_normalize_url_rejects_junk(raw):
    with pytest.raises(HttpError) as exc:
        normalize_url(raw)
    assert exc.value.status == 422


def test_parse_options_defaults_match_the_old_service():
    options = parse_options({"url": "https://example.com"})
    assert options["mode"] == "main"
    assert options["render"] is False
    assert options["cache_ttl_seconds"] == 86_400
    assert options["max_chunk_chars"] == 30_000
    assert options["timeout"] == 60.0


def test_unknown_fields_are_reported_but_not_fatal():
    # The old service was a pydantic model with extra="ignore", so a caller
    # passing an unknown key got a 200. Turning that into a 422 would break
    # them — but a silently dropped option is the same class of bug as a
    # silently skipped tier, so it is surfaced rather than swallowed.
    body = {"url": "https://example.com", "rendeer": True}
    assert unknown_fields(body) == ["rendeer"]
    assert parse_options(body)["render"] is False


def test_batch_only_fields_are_not_unknown_on_a_batch():
    body = {"urls": [], "name": "job", "queue": "fetch_bulk"}
    assert unknown_fields(body, extra_allowed=("name", "queue")) == []
    # ...but they are not options on a single fetch.
    assert unknown_fields(body) == ["name", "queue"]


@pytest.mark.parametrize("body", [
    {"render": "yes"},
    {"mode": "sideways"},
    {"timeout": 0},
    {"max_chunk_chars": "lots"},
])
def test_parse_options_rejects_bad_values(body):
    with pytest.raises(HttpError):
        parse_options({"url": "https://example.com", **body})


# ── the defect this service exists to prevent ───────────────────────────────

def test_render_request_without_a_browser_tier_is_refused_up_front():
    assert diagnostics.eligible(NO_ZYTE, render=True) == []
    reason = diagnostics.preflight(NO_ZYTE, render=True)
    assert reason is not None
    assert "not a block by the target site" in reason
    assert "zyte_api_key" in reason


def test_plain_request_still_runs_on_the_direct_tier():
    # A missing Zyte key must not take the whole service down — direct still
    # serves anything that isn't behind a bot wall.
    assert diagnostics.preflight(NO_ZYTE, render=False) is None


def test_fully_configured_deployment_has_no_preflight_refusal():
    assert diagnostics.preflight(FULLY_CONFIGURED, render=True) is None


def test_blocked_with_no_attempts_is_relabelled_not_configured():
    # This is the exact payload the old service returned for a Qorvo fetch:
    # status=blocked, "escalation exhausted", nothing tried. It reads as a
    # hostile site and it is an unset environment variable.
    payload = {
        "status": "blocked",
        "attempts": [],
        "tier_used": None,
        "error_class": "blocked",
        "error_message": "escalation exhausted",
        "metadata": {},
    }
    out = diagnostics.annotate(payload, NO_ZYTE, render=True)
    assert out["status"] == "failed"
    assert out["error_class"] == diagnostics.NOT_CONFIGURED
    assert "zyte_api_key" in out["error_message"]
    assert out["metadata"]["tiers_live"] == []


def test_a_real_block_stays_blocked():
    # Tiers ran and the host refused them. That is a block, and calling it a
    # config problem would be just as misleading in the other direction.
    payload = {
        "status": "blocked",
        "attempts": [{"tier": "zyte-browser", "verdict": "http_block"}],
        "tier_used": "zyte-browser",
        "error_class": "blocked",
        "error_message": "escalation exhausted",
        "metadata": {},
    }
    out = diagnostics.annotate(payload, FULLY_CONFIGURED, render=True)
    assert out["status"] == "blocked"
    assert out["error_class"] == "blocked"


def test_block_with_live_tiers_stays_blocked_even_if_others_are_unconfigured():
    # Most deployments have *some* unconfigured tier (nobody has BrightData).
    # That alone must not turn every block into "not configured".
    payload = {
        "status": "blocked",
        "attempts": [],
        "tier_used": None,
        "error_class": "blocked",
        "error_message": "breaker tripped for example.com",
        "metadata": {},
    }
    out = diagnostics.annotate(payload, NO_ZYTE, render=False)
    assert out["status"] == "blocked"


def test_successful_responses_are_left_alone():
    payload = {"status": "done", "attempts": [], "metadata": {"title": "x"}}
    assert diagnostics.annotate(dict(payload), NO_ZYTE, render=False) == payload


def test_status_stays_inside_the_published_set():
    published = {"done", "cached", "blocked", "failed"}
    refusal = diagnostics.not_configured_result(
        request_id="r", url="https://x.test/", cache_key="", reason="nope",
        config_info=NO_ZYTE, render=True, fetched_at="2026-09-07T00:00:00Z",
    )
    assert refusal["status"] in published
    assert refusal["error_class"] == diagnostics.NOT_CONFIGURED
    assert CONTRACT_KEYS <= set(refusal)


def test_skipped_separates_missing_credentials_from_render_filtering():
    reasons = {s["tier"]: s["reason"] for s in diagnostics.skipped(NO_ZYTE, render=True)}
    assert reasons["zyte-browser"] == "unconfigured"
    assert reasons["direct"] == "not_browser"


def test_summary_line_names_what_each_dead_tier_wants():
    line = diagnostics.summary_line(NO_ZYTE)
    assert "tiers live: direct" in line
    assert "zyte-browser(zyte_api_key)" in line
    assert "local-browser(module:patchright)" in line


# ── batch queue ─────────────────────────────────────────────────────────────

def test_batch_roundtrip(tmp_path):
    store = BatchStore(tmp_path / "b.db")
    batch_id, ids = store.create(
        ["https://a.test/", "https://b.test/"],
        name="job", queue="fetch_bulk", options={"render": False},
    )
    assert len(ids) == 2

    claimed = store.claim()
    assert claimed["url"] == "https://a.test/"
    assert claimed["options"] == {"render": False}

    store.finish(claimed["request_id"], {
        "status": "done", "tier_used": "direct", "markdown": "hi",
        "error_class": None, "error_message": None,
    })

    batch = store.batch(batch_id)
    assert batch["total"] == 2
    assert batch["counts"] == {"done": 1, "pending": 1}
    assert [i["url"] for i in batch["items"]] == ["https://a.test/", "https://b.test/"]
    assert store.result(claimed["request_id"])["markdown"] == "hi"


def test_batch_claim_is_exclusive(tmp_path):
    store = BatchStore(tmp_path / "b.db")
    store.create(["https://a.test/"], name=None, queue="fetch_default", options={})
    assert store.claim() is not None
    assert store.claim() is None, "a claimed item must not be handed out twice"


def test_restart_requeues_in_flight_work(tmp_path):
    # A 10k-URL batch must survive a service restart rather than stranding
    # every item that happened to be running.
    store = BatchStore(tmp_path / "b.db")
    store.create(["https://a.test/"], name=None, queue="fetch_default", options={})
    store.claim()
    assert store.recover() == 1
    assert store.claim() is not None


def test_unknown_batch_and_request_read_as_missing(tmp_path):
    store = BatchStore(tmp_path / "b.db")
    assert store.batch("nope") is None
    assert store.result("nope") is None
