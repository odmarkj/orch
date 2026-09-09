"""Telling "we are not configured for this" apart from "the site blocked us".

This is the defect that cost the most time in practice. web-fetch's tier
ladder skips any tier whose credentials are missing. With ``ZYTE_API_KEY``
empty and ``render: true`` set, *every* tier is skipped — the browser filter
removes the direct tier, the credential filter removes both Zyte tiers — so
the escalation loop body never executes and raises ``EscalationExhausted([])``.
The caller sees ``status=blocked``, ``error="escalation exhausted"``,
``tier_used=None``, ``attempts=[]``, which reads exactly like a hostile site.
It is not: it is an unset environment variable.

So the serving layer refuses to let a configuration gap masquerade as a
block:

* **Pre-flight** — if no tier is eligible for the request at all, fail before
  spending a network call, with ``error_class="not_configured"`` naming the
  missing credentials.
* **Post-hoc** — a ``blocked`` result that tried literally nothing, in a
  deployment with unconfigured tiers, is re-labelled the same way.

``status`` stays inside the published set (``done|cached|blocked|failed``);
the new information rides on ``error_class`` and ``metadata``, so no consumer
has to change. ``failed`` rather than ``blocked`` is deliberate — a missing
key is our failure, not the host's verdict.

Every function that takes ``config_info`` takes the dict returned by
``web_fetch.tiers.tier_configuration()``, so the classification is pure and
testable without the library installed.
"""

from __future__ import annotations

from typing import Any


NOT_CONFIGURED = "not_configured"


def tier_configuration() -> dict[str, list[dict]]:
    """Live tier inventory from web-fetch. Imports the library lazily."""
    from web_fetch.tiers import tier_configuration as _tc

    return _tc()


def eligible(config_info: dict[str, Any], *, render: bool) -> list[dict]:
    """Configured tiers that could actually serve a request with this
    ``render`` flag. ``render=True`` restricts the ladder to browser tiers,
    which is what makes a missing browser credential fatal rather than a
    downgrade."""
    tiers = list(config_info.get("configured") or [])
    if render:
        tiers = [t for t in tiers if t.get("is_browser")]
    return tiers


def skipped(config_info: dict[str, Any], *, render: bool) -> list[dict]:
    """Every tier that will not run for this request, with the reason.

    Two distinct reasons, kept distinct because they need different fixes:
    ``unconfigured`` wants a credential or a package, ``not_browser`` is just
    this request asking for rendering.
    """
    out: list[dict] = []
    for tier in config_info.get("unconfigured") or []:
        out.append({
            "tier": tier.get("tier"),
            "reason": "unconfigured",
            "missing_credentials": tier.get("missing_credentials") or [],
            "missing_modules": tier.get("missing_modules") or [],
        })
    if render:
        for tier in config_info.get("configured") or []:
            if not tier.get("is_browser"):
                out.append({
                    "tier": tier.get("tier"),
                    "reason": "not_browser",
                    "missing_credentials": [],
                    "missing_modules": [],
                })
    return out


def missing_settings(config_info: dict[str, Any]) -> list[str]:
    """Flat, de-duplicated list of the credentials/modules that would light
    up more tiers. Ordered by first appearance so the message is stable."""
    seen: list[str] = []
    for tier in config_info.get("unconfigured") or []:
        for name in (tier.get("missing_credentials") or []):
            if name not in seen:
                seen.append(name)
        for name in (tier.get("missing_modules") or []):
            label = f"module:{name}"
            if label not in seen:
                seen.append(label)
    return seen


def preflight(config_info: dict[str, Any], *, render: bool) -> str | None:
    """Reason this request cannot run at all, or None if some tier is live."""
    if eligible(config_info, render=render):
        return None
    missing = missing_settings(config_info)
    what = "browser-capable tier" if render else "fetch tier"
    detail = f" (unset: {', '.join(missing)})" if missing else ""
    return (
        f"no {what} is configured{detail}. This is a configuration gap in the "
        f"fetch service, not a block by the target site."
    )


def summary_line(config_info: dict[str, Any]) -> str:
    """One-line startup/doctor summary: which tiers are live, which are not,
    and what each unconfigured one is waiting for."""
    live = ", ".join(t["tier"] for t in config_info.get("configured") or [])
    parts = []
    for tier in config_info.get("unconfigured") or []:
        want = list(tier.get("missing_credentials") or [])
        want += [f"module:{m}" for m in (tier.get("missing_modules") or [])]
        parts.append(f"{tier['tier']}({'+'.join(want) or 'disabled'})")
    return (
        f"tiers live: {live or '(none)'} | "
        f"tiers skipped: {', '.join(parts) or '(none)'}"
    )


def annotate(
    payload: dict[str, Any],
    config_info: dict[str, Any],
    *,
    render: bool,
) -> dict[str, Any]:
    """Attach the tier inventory to a non-success response and re-label a
    configuration gap that came back wearing ``blocked``.

    Only touches failing responses: a successful fetch does not need to carry
    a config dump, and every extra key is one more thing a consumer might
    come to depend on.
    """
    status = payload.get("status")
    if status not in ("blocked", "failed"):
        return payload

    metadata = dict(payload.get("metadata") or {})
    metadata["tiers_live"] = [t["tier"] for t in eligible(config_info, render=render)]
    metadata["tiers_skipped"] = skipped(config_info, render=render)
    payload["metadata"] = metadata

    # A block with zero attempts is not a block. Either the ladder was empty
    # (nothing configured) or the request never reached a provider; when the
    # deployment has unconfigured tiers, that is overwhelmingly the reason,
    # and mislabelling it costs hours of chasing an imaginary bot wall.
    if (
        status == "blocked"
        and not payload.get("attempts")
        and (config_info.get("unconfigured") or [])
        and not metadata["tiers_live"]
    ):
        missing = missing_settings(config_info)
        payload["status"] = "failed"
        payload["error_class"] = NOT_CONFIGURED
        payload["error_message"] = (
            "no tier could run this request — the fetch service is missing "
            f"configuration ({', '.join(missing) or 'see tiers_skipped'}), "
            "so nothing was ever sent to the target host"
        )
    return payload


def not_configured_result(
    *,
    request_id: str,
    url: str,
    cache_key: str,
    reason: str,
    config_info: dict[str, Any],
    render: bool,
    fetched_at: str,
) -> dict[str, Any]:
    """A full contract-shaped response for the pre-flight refusal.

    Same keys as any other response so callers never branch on shape — only
    ``status``/``error_class`` differ.
    """
    return {
        "request_id": request_id,
        "status": "failed",
        "url": url,
        "final_url": None,
        "cache_key": cache_key,
        "tier_used": None,
        "cached": False,
        "fetched_at": fetched_at,
        "expires_at": None,
        "cost_usd": 0.0,
        "status_code": None,
        "markdown": "",
        "chunks": [],
        "metadata": {
            "tiers_live": [t["tier"] for t in eligible(config_info, render=render)],
            "tiers_skipped": skipped(config_info, render=render),
        },
        "chunk_count": 0,
        "html": None,
        "attempts": [],
        "error_class": NOT_CONFIGURED,
        "error_message": reason,
    }
