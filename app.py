import os, time, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify

app = Flask(__name__)

# Read your HikerAPI access key from an environment variable
ACCESS_KEY = os.environ.get("HIKER_API_KEY")

def _get(url: str, params: dict, tries: int = 0):
    """GET with retries on 429/5xx, attaches access_key automatically."""
    if ACCESS_KEY is None:
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
    return r.json()

@app.get("/health")
def health():
    return jsonify(ok=True)

@app.get("/followers_enriched")
def followers_enriched():
    """
    Example:
    GET /followers_enriched?username=therealbrianmark&page_size=200&min_followers=10000&workers=5&cursor=...
    Returns: users [ {username, followers_count, full_name, pk, is_private}, ... ], next_cursor
    """
    username = request.args.get("username")
    if not username:
        return jsonify(error="username required"), 400

    page_size = int(request.args.get("page_size", 200))
    min_followers = int(request.args.get("min_followers", 10000))  # default 10k filter
    cursor = request.args.get("cursor")
    workers = max(1, min(int(request.args.get("workers", 5)), 8))  # cap threads 1..8

    # 1) Resolve target user id from username
    user_obj = _get("https://api.hikerapi.com/v1/user/by/username", {"username": username})
    user_id = user_obj.get("id") or user_obj.get("pk")
    if not user_id:
        return jsonify(error="could not resolve user id for username", username=username), 400

    # 2) Fetch one page of followers (ids/usernames) via v2 followers
    fol_params = {"user_id": user_id, "page_size": page_size}
    if cursor:
        fol_params["cursor"] = cursor
    page = _get("https://api.hikerapi.com/v2/user/followers", fol_params)
    items = page.get("items") or page.get("results") or []

    # 3) Enrich each follower via v1 user/by/id -> followers_count
    def enrich(it):
        pk = it.get("id") or it.get("pk")
        if not pk:
            # Try fallback by username if present
            if it.get("username"):
                info = _get("https://api.hikerapi.com/v1/user/by/username", {"username": it["username"]})
            else:
                return None
        else:
            info = _get("https://api.hikerapi.com/v1/user/by/id", {"user_id": pk})

        cnt = info.get("followers_count") or (info.get("edge_followed_by") or {}).get("count", 0) or 0
        return {
            "username": info.get("username") or it.get("username"),
            "followers_count": int(cnt),
            "full_name": info.get("full_name", ""),
            "pk": info.get("id") or info.get("pk") or pk,
            "is_private": bool(info.get("is_private")),
        }

    users = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(enrich, it) for it in items]
        for f in as_completed(futures):
            try:
                row = f.result()
                if not row:
                    continue
                if row["followers_count"] >= min_followers:
                    users.append(row)
            except Exception:
                # skip individual failures
                pass

    return jsonify({
        "account_scraped": username,
        "returned": len(users),
        "next_cursor": page.get("next_cursor") or page.get("next_max_id"),
        "users": users
    })

if __name__ == "__main__":
    # For local testing
    port = int(os.environ.get("PORT", "3000"))
    app.run(host="0.0.0.0", port=port)
