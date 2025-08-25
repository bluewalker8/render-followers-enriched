
# Followers Enriched API (Render + Flask)

This tiny API exposes `/followers_enriched` which returns a page of a target user's followers **enriched** with their `followers_count`, already filtered to `>= min_followers` (defaults to 10,000).

## Endpoints

- `GET /health` – simple health check.
- `GET /followers_enriched`
  - **Query params:**
    - `username` (required) – target account handle (e.g., `therealbrianmark`)
    - `page_size` (optional, default 200) – followers per page to fetch from HikerAPI
    - `min_followers` (optional, default 10000) – only return users with at least this many followers
    - `workers` (optional, default 5) – parallel lookups to speed up enrichment (1–8)
    - `cursor` (optional) – pass the value returned in the previous response to get the next page

**Example**
```
GET /followers_enriched?username=therealbrianmark&page_size=200&min_followers=10000&workers=5
```

**Response**
```json
{
  "account_scraped": "therealbrianmark",
  "returned": 37,
  "next_cursor": "QVFE...",
  "users": [
    {"username":"example", "followers_count":12345, "full_name":"...", "pk":"...", "is_private":false}
  ]
}
```

## Deploy to Render (Free)

1. Create a new **GitHub repo** and add these files: `app.py`, `requirements.txt`, `Procfile`.
2. On **Render**: click **New → Web Service → Connect** your GitHub repo.
3. Choose the **Free** plan.
4. **Build Command:** `pip install -r requirements.txt`
5. **Start Command:** `gunicorn app:app`
6. **Environment → Add Variable:**
   - Key: `HIKER_API_KEY`
   - Value: *your HikerAPI access key*
7. Click **Create Web Service** and wait for deploy to finish.
8. Test:
   - `https://<your-service>.onrender.com/health`
   - `https://<your-service>.onrender.com/followers_enriched?username=therealbrianmark&page_size=200&min_followers=10000&workers=5`

## Use from Make (Integromat)

1. **HTTP (GET)** to your `/followers_enriched` URL.
2. **Iterator** over `users[]`.
3. **Google Sheets → Add Rows (bulk)**: map `username`, `followers_count`, etc.
4. Loop pages: if `next_cursor` exists, call the HTTP again with `&cursor={{1.next_cursor}}`. Add 1–2s sleep between pages.
5. Keep scenario **concurrency = 1**. Keep `workers` set to 3–5.

## Notes

- HikerAPI charges per **user lookup** during enrichment. Keep `page_size` and `workers` conservative.
- If you hit rate limits (429) or 5xx, the code already retries with backoff.
