import os
import time
import requests
from typing import Any, Dict, List, Optional, Tuple, Union
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify

app = Flask(__name__)

# Set this in Render → Environment → HIKER_API_KEY
ACCESS_KEY = os.environ.get("HIKER_API_KEY")


def _get(url: str, params: Optional[Dict[str, Any]] = None, tries: int = 0) -> Any:
    """
    GET helper that:
      - Attaches HikerAPI access_key as a query param
      - Retries on 429/5xx with exponential backoff
    Returns parsed JSON (list or dict).
    """
    if not ACCESS_KEY:
        raise RuntimeError("HIKER_API_KEY env var is missing")

    qp = dict(params or {})
    qp["access_key"] = ACCESS_KEY

    try:
        r = requests.get(url, params=qp, timeout=40)
    except requests.exceptions.RequestException as e:
        if tries < 3:
            time.sleep(0.5 * (2 ** tries))
            return _get(url, params, tries + 1)
        raise e

    if r.status_code in (429, 500, 502, 503, 504) and tries < 3:
        time.sleep(0.5 * (2 ** tries))
        return _get(url, params, tries + 1)

    r.raise_for_status()
    # Could be dict OR list (v1/chunk sometimes returns [users[], next_max_id])
    try:
        return r.json()
    except ValueError:
        # Not JSON
        raise RuntimeError(f"Non-JSON from {url}: {r.text[:200]}")


def _normalize_followers_v1(page: Union[Dict[str, Any], List[Any]]
                            ) -> Tuple[List[Dict[str, Any]], Optional[str], List[str]]:
    """
    Support BOTH shapes from /v1/user/followers/chunk:
      A) list: [ users[], next_max_id_or_null ]
      B) dict: { users: [], next_max_id: "..." }
    Returns: (items, next_cursor, debug_keys)
    """
    # A) list/tuple shape
    if isinstance(page, (list, tuple)):
        items = page[0] if len(page) > 0 and isinstance(page[0], list) else []
        next_cursor = page[1] if len(page) > 1 else None
        return items, (str(next_cursor) if next_cursor not in (None, "") else None), ["list_payload"]

    # B) dict shape
    items = page.get("users") or page.get("items") or page.get("results") or []
    next_cursor = (
        page.get("next_max_id")
        or page.get("next_cursor")
        or page.get("page_id")
        or page.get("end_cursor")
    )
    next_cursor = str(next_cursor) if next_cursor not in (None, "") else None
    return items, next_cursor, list(page.keys())


def _enrich_by_pk(pk: Union[str, int], fallback_username: Optional[str] = None) -> Dict[str, Any]:
    """
    Query /v1/user/by/id?id={pk} and return normalized row with follower count.
    """
    info = _get("https://api.hikerapi.com/v1/user/by/id", {"id": pk})
    followers_cnt = (
        info.get("followers_count")
        or info.get("follower_count")
        or (info.get("edge_followed_by") or {}).get("count", 0)
        or 0
    )
    return {
        "username": info.get("username", fallback_username),
        "followers_count": int(followers_cnt),
        "full_name": info.get("full_name", ""),
        "pk": info.get("pk") or info.get("id") or pk,
        "is_private": bool(info.get("is_private")),
    }


@app.get("/health")
def health():
    return jsonify(ok=True)


@app.get("/followers_enriched")
def followers_enriched():
    """
    GET /followers_enriched?username=therealbrianmark&page_size=200&min_followers=10000&workers=5&cursor=...
      or use user_id=50786729042 to skip resolving from username

    Query:
      - username OR user_id
      - page_size (default 200)
      - min_followers (default 10000)
      - workers (1..8, default 5)
      - cursor (pass previous next_cursor)
      - debug=1 to include debug info
    """
    try:
        handle = request.args.get("username")
        user_id = request.args.get("user_id")
        page_size = int(request.args.get("page_size", 200))
        min_followers = int(request.args.get("min_followers", 10000))
        cursor = request.args.get("cursor")
        workers = max(1, min(int(request.args.get("workers", 5)), 8))
        include_debug = request.args.get("debug") == "1"

        # 1) Resolve user id if only username given
        account_label = handle or user_id
        if not user_id:
            if not handle:
                return jsonify(error="Provide 'username' or 'user_id'"), 400
            u = _get("https://api.hikerapi.com/v1/user/by/username", {"username": handle})
            user_id = u.get("pk") or u.get("id")
            if not user_id:
                return jsonify(error="Could not resolve user id from username", username=handle, raw=u), 400

        # 2) Fetch a followers page via v1 chunk (IMPORTANT: use 'count', not 'page_size')
        params = {"user_id": user_id, "count": page_size}
        if cursor:
            params["max_id"] = cursor
        page = _get("https://api.hikerapi.com/v1/user/followers/chunk", params)

        items, next_cursor, follower_page_keys = _normalize_followers_v1(page)

        # 3) Enrich in parallel and filter ≥ min_followers
        users: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = []
            for it in items:
                pk = it.get("pk") or it.get("id")
                if not pk:
                    continue
                futs.append(ex.submit(_enrich_by_pk, pk, it.get("username")))
            for f in as_completed(futs):
                try:
                    row = f.result()
                    if row["followers_count"] >= min_followers:
                        users.append(row)
                except Exception:
                    # Skip one-off failures
                    pass

        resp = {
            "account_scraped": account_label,
            "returned": len(users),
            "next_cursor": next_cursor,
            "users": users,
        }
        if include_debug:
            resp["debug"] = {
                "followers_page_keys": follower_page_keys,
                "received_type": type(page).__name__,
                "page_size_requested": page_size,
                "workers": workers,
            }
        return jsonify(resp)

    except requests.HTTPError as e:
        # Surface upstream error to your browser and Render logs
        return jsonify(error="HikerAPI HTTPError", detail=str(e)), 502
    except Exception as e:
        # Show Python exception text so you can see what's wrong
        return jsonify(error="Server exception", detail=str(e)), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3000"))
    app.run(host="0.0.0.0", port=port)
