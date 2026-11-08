"""JSON-file state shared by the How and Why pipelines: lists and the seen-URL ledger.

A ledger is a JSON array of {"url": ..., "processed_at": ISO timestamp} entries,
pruned to a retention window on every write.
"""
import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_DROPPED_QUERY_PARAMS = {
    "access_token", "token", "key", "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source",
}


def load_json_list(path: str) -> List[Dict[str, Any]]:
    """Load a JSON array file. Missing file → []. Malformed file raises."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{path} is malformed JSON: {e}") from e
    if not isinstance(data, list):
        raise RuntimeError(f"{path} must contain a JSON array, got {type(data).__name__}")
    return data


def save_json_list(path: str, data: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def prune_seen(
    seen: List[Dict[str, Any]], now: datetime, retention_days: int
) -> List[Dict[str, Any]]:
    """Drop ledger entries older than retention_days (and any unparsable ones)."""
    cutoff = now - timedelta(days=retention_days)
    kept: List[Dict[str, Any]] = []
    for entry in seen:
        try:
            if entry.get("url") and datetime.fromisoformat(entry["processed_at"]) >= cutoff:
                kept.append(entry)
        except (KeyError, TypeError, ValueError):
            continue
    return kept


def latest_processed_at(seen: List[Dict[str, Any]]) -> Optional[datetime]:
    """Newest parsable processed_at in the ledger, or None if the ledger is empty."""
    latest: Optional[datetime] = None
    for entry in seen:
        try:
            stamp = datetime.fromisoformat(entry["processed_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if latest is None or stamp > latest:
            latest = stamp
    return latest


def mark_seen(
    seen: List[Dict[str, Any]], urls: List[str], now: datetime
) -> List[Dict[str, Any]]:
    """Return the ledger with urls appended (existing URLs are not duplicated)."""
    known = {e.get("url") for e in seen}
    stamp = now.isoformat()
    return seen + [
        {"url": u, "processed_at": stamp} for u in dict.fromkeys(urls) if u and u not in known
    ]


def canonical_url(url: str) -> str:
    """Lowercased scheme/host, path without trailing slash, and only meaningful query params.

    Credential and tracking params are dropped: Stratechery links carry a per-fetch
    ?access_token=…, so the raw URL is not a stable key and must never be written to
    a committed file. Other params are kept (sorted) because some identify the page,
    e.g. WordPress ?p=20955.
    """
    parts = urlsplit(url.strip())
    if not parts.netloc:
        return url.strip()
    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _DROPPED_QUERY_PARAMS and not k.lower().startswith("utm_")
    ))
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, "")
    )
