# کورد کالۆ (KurdCalo) — Web MVP

Upload a photo of a meal, get calories/protein/carbs/fat back in Sorani
Kurdish. This is a web rebuild of the original KurdCaloBot Telegram bot -
the AI/nutrition logic is reused from that project; the Telegram-specific
parts are not.

---

## 1. What's actually in this project

```
kurdcalo-web/
├── backend/            FastAPI app - deploys to Render (see §3 for why
│   ├── app.py           not Cloudflare Workers)
│   ├── quota.py          NEW - anonymous device+IP daily quota
│   ├── vision.py          REUSED verbatim from the bot - Gemini calls,
│   ├── gemini_queue.py     retries, pacing, circuit breaker, batching
│   ├── knowledge.py        REUSED verbatim - loads the food glossary
│   ├── nutrition.py        REUSED verbatim - BMR/TDEE calculator
│   │                        (present but NOT wired into this MVP's UI -
│   │                         see §6)
│   ├── data/              REUSED verbatim - the actual food knowledge
│   ├── requirements.txt
│   ├── render.yaml         Render Blueprint (optional - see §4)
│   └── .env.example
└── frontend/            Static site - deploys to Cloudflare Pages
    ├── index.html        No framework, no build step
    ├── style.css          Mobile-first, RTL, Kurdish
    ├── app.js
    └── config.js           The ONE file you edit (your backend's URL)
```

**Reused unmodified from the original bot repo:** `vision.py`,
`gemini_queue.py`, `knowledge.py`, `nutrition.py`, and the `data/` food
glossary. None of these ever imported anything Telegram-specific - that
was checked before reuse, not assumed.

**Deliberately NOT included in this MVP** (present in the original
project, left out here to keep the surface area small, per the brief):
- `bot.py` - Telegram-only, nothing in it applies to a website.
- `storage.py` - built around permanent Telegram-account meal history.
  This MVP stores no history at all; `quota.py` is a small, new,
  purpose-built replacement for just the daily-limit piece.
- `reviewer.py` - an optional second-opinion quality check that needs
  its own separate API key. Can be added later; not required for a
  working MVP.

---

## 2. Account & secrets checklist

You need, total:

1. **A Google account** with a Gemini API key - free, from
   https://aistudio.google.com/apikey. This is the only real secret in
   the whole project.
2. **A GitHub account** (free) - to host the code Render and Cloudflare
   Pages deploy from.
3. **A Render account** (free, no credit card) - runs the backend.
4. **A Cloudflare account** (free, no credit card) - hosts the frontend.

No payment method is required anywhere in this list.

---

## 3. Why the backend runs on Render, not Cloudflare Workers

You asked to prefer Workers if migration would be reliable, and to
propose the simplest alternative otherwise - here's that reasoning,
checked against Cloudflare's current (Sept 2026) documentation rather
than assumed:

Cloudflare's Python Workers reached general availability on
2026-09-21, which is genuinely new and worth knowing about. But
Cloudflare's own standard-library docs state that **threading is
non-functional in the Workers WebAssembly VM**. `gemini_queue.py`
depends on `asyncio.to_thread(...)` to run `vision.py`'s blocking
`urllib`-based HTTP calls to Gemini without blocking the event loop -
that mechanism requires real OS threads under the hood, which Workers'
Python runtime doesn't support. On top of that, `gemini_queue.py`'s
persistent background worker tasks (started once, running indefinitely
between requests, holding pacing/budget/circuit-breaker state in
memory) have no direct equivalent in Workers' per-invocation execution
model - that would need a rewrite onto Durable Objects, a different
concurrency pattern entirely.

Porting this specific codebase to Workers would mean rewriting the
networking layer and the concurrency model - not a port, a rewrite of
exactly the two things you asked me not to touch. Render runs the
existing Python code - including its `asyncio` queue and its `urllib`
calls - completely unmodified, which is the lower-risk choice for an
MVP. Cloudflare Pages is still used for the frontend, which is a
perfect fit for it (static files, nothing Python-specific involved).

---

## 4. Deploy the backend (Render) - step by step

You can do all of this from a phone browser; no terminal required.

### 4a. Get your Gemini API key
1. Go to https://aistudio.google.com/apikey, sign in, click "Create API
   key". Copy it somewhere safe for a moment - you'll paste it into
   Render in step 4d, nowhere else.

### 4b. Put the code on GitHub
1. Go to https://github.com, sign in (or create a free account).
2. Click the "+" in the top right → "New repository". Name it
   `kurdcalo-web`, keep it Public or Private (either works), click
   "Create repository".
3. On the new repo's page, click "uploading an existing file" (or Add
   file → Upload files).
4. Upload the **entire contents** of the `kurdcalo-web` folder from
   this ZIP - both the `backend/` and `frontend/` folders, keeping
   their structure. On a phone, your file picker may only let you
   select files one level at a time; if so, upload `backend/` and
   `frontend/` as separate uploads into the repo root, or use GitHub's
   "Upload folder" option if your browser offers it.
5. Commit directly to the `main` branch.

### 4c. Create the Render web service
1. Go to https://render.com, sign up (GitHub login is the fastest
   option) - no credit card needed for the free tier.
2. Click "New +" → "Web Service".
3. Connect the `kurdcalo-web` GitHub repo you just created.
4. Render should detect `backend/render.yaml` and offer to use it as a
   Blueprint. If it does, accept it and skip to 4d. If not, fill in
   manually:
   - **Root Directory**: `backend`
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: Free

### 4d. Set the environment variables
In the service's "Environment" tab, add:

| Key | Value |
|---|---|
| `GEMINI_API_KEY` | the key from step 4a |
| `GEMINI_MODEL_POOL` | `gemini-2.5-flash:18:5,gemini-2.5-flash-lite:18:10` |
| `BATCH_WINDOW_MS` | `400` |
| `DAILY_SCAN_LIMIT` | `4` |
| `MAX_UPLOAD_MB` | `8` |
| `FRONTEND_ORIGIN` | *(leave blank for now - come back after step 5)* |

The `GEMINI_MODEL_POOL` numbers were verified against this project's
real Google AI Studio quota as of the original bot's last check -
re-verify at https://aistudio.google.com/rate-limit before trusting
them long-term, since Google can change free-tier limits without
notice.

5. Click "Create Web Service" (or "Deploy"). Wait for the build to
   finish - a few minutes. Your backend URL will look like
   `https://kurdcalo-api-xxxx.onrender.com` - copy it, you need it next.

6. Visit `https://your-backend-url.onrender.com/api/health` in a
   browser. You should see `{"ok":true}`. If you see an error, check
   the Render dashboard's "Logs" tab.

---

## 5. Deploy the frontend (Cloudflare Pages) - step by step

### 5a. Point the frontend at your backend
1. In the `frontend/` folder, open `config.js`.
2. Replace the placeholder URL with your real Render backend URL from
   step 4:
   ```js
   const API_BASE_URL = "https://kurdcalo-api-xxxx.onrender.com";
   ```
3. Save, and make sure this change is in your GitHub repo (either edit
   it directly on GitHub's web editor, or re-upload the file).

### 5b. Deploy to Cloudflare Pages
**Option A - fastest, no GitHub needed:**
1. Go to https://dash.cloudflare.com → "Workers & Pages" → "Create" →
   "Pages" → "Upload assets".
2. Upload the `frontend/` folder's contents directly (drag and drop
   works on desktop; on Android, use the file picker and select the
   folder if your browser supports it, or select all files inside it).
3. Give it a project name (e.g. `kurdcalo`), click "Deploy".
4. Your site will be live at `https://kurdcalo.pages.dev` (or whatever
   name you chose).

**Option B - connects to GitHub, auto-redeploys on future edits:**
1. "Workers & Pages" → "Create" → "Pages" → "Connect to Git".
2. Select your `kurdcalo-web` repo.
3. Set **Root directory** to `frontend`. Leave build command/output
   directory blank (this is a plain static site, no build step).
4. Deploy.

### 5c. Connect the two together
1. Copy your Cloudflare Pages URL (e.g. `https://kurdcalo.pages.dev`).
2. Back in Render, open your backend service → Environment → set
   `FRONTEND_ORIGIN` to that exact URL (no trailing slash).
3. Save - Render will redeploy automatically.

**You're live.** Open your Pages URL on a phone and try uploading a
meal photo.

---

## 6. What's intentionally not built yet

- **No accounts, no login.** Quota is tracked anonymously per
  device+IP (see §8). Fine for an MVP; add accounts later if needed.
- **No payments.** Explicitly out of scope per the brief.
- **No BMR/calorie-target calculator UI**, even though `nutrition.py`
  (which does this) was copied into the backend. It's pure-function,
  zero cost, kept for a future "set your daily targets" screen - but
  wiring it up wasn't part of the photo-analysis MVP requested, so I
  didn't build a UI for it or claim that feature works today.
- **No permanent meal history.** Each scan is stateless - analyzed,
  returned, and not stored. This matches "do not store uploaded
  photos permanently" taken to its natural conclusion: nothing about
  the photo is kept after the response is sent.
- **`reviewer.py`'s second-opinion quality check** isn't wired in -
  it needs a second, separate API key and is a quality upgrade, not
  core functionality.

---

## 7. Testing performed vs. what you should still check

**I could test, and did:**
- All backend Python files compile cleanly (`py_compile`).
- `vision.py`/`gemini_queue.py`/`knowledge.py` import correctly from
  their new location with the relocated `data/` folder.
- `quota.py`'s daily-limit logic, including a genuine concurrent-
  threads stress test (30 simultaneous requests against a limit of 5 -
  exactly 5 granted, 25 correctly rejected, no race condition).
- The exact response shape `app.py` expects from
  `gemini_queue.submit_photo_job()` (`total_kcal`, per-food fields,
  the `no_food` path) - verified against the real function using the
  original project's own mock-Gemini test harness, not assumed.
- A real bug, found and fixed before shipping: returning a custom
  `JSONResponse` from a FastAPI error path discards cookies set on a
  separately-injected `response` parameter - a known FastAPI gotcha.
  Every response in `app.py` now sets the device-id cookie through one
  explicit, consistent path instead of relying on that.

**I could NOT test** (this sandbox has no outbound network access, so
`pip install fastapi` fails here):
- Actually running `uvicorn app:app` and hitting it with real HTTP
  requests.
- Live CORS/cookie behavior in an actual browser.

**Before you rely on this**, please do a 5-minute local smoke test:
```bash
cd backend
pip install -r requirements.txt
export GEMINI_API_KEY=your_real_key
uvicorn app:app --reload
```
Then open `frontend/index.html` directly in a browser (or serve it
with `python3 -m http.server` from the `frontend/` folder), temporarily
set `config.js`'s `API_BASE_URL` to `http://localhost:8000`, and try a
real upload before deploying.

---

## 8. Known limitations - read before you trust the quota numbers

**Render's free tier has an ephemeral filesystem.** The service spins
down after 15 minutes of no traffic and spins back up on the next
request - and when it does, any local file (including `quota.db`, and
`gemini_queue.py`'s own in-memory daily Gemini budget tracking, which
isn't a file at all and resets even harder) starts over from zero.

What this actually means for a low-traffic MVP:
- Your "4 scans/day" limit is really "4 scans per continuous-traffic
  session" - it can reset more than once a day if the site goes quiet
  for 15+ minutes.
- `gemini_queue.py`'s own internal daily-budget protection (the `18`
  in `GEMINI_MODEL_POOL`) resets the same way. In the worst case this
  means the app could make more real Gemini calls across a day than
  that number implies.
- This is **not** a safety or billing risk - Gemini's free tier has no
  payment method attached, so there's nothing to overspend. The
  consequence of exceeding Google's real limit is more `429` errors
  shown to users on a bad day, not a bill.

If you want this fully solid later: either upgrade to a Render paid
plan and attach a persistent disk (cheap, simplest fix), or move
`quota.py`'s storage to Cloudflare D1 (a real SQL database, free,
genuinely persistent, and supports the exact same atomic
`INSERT ... ON CONFLICT DO UPDATE ... WHERE ... RETURNING` SQL this
file already uses and was tested with - so the logic itself wouldn't
need to change, just where it's stored).

**The anonymous device+IP quota is a reasonable free-tier limiter, not
a strong anti-abuse system.** A user can clear cookies to look like a
new device; `quota.py` also checks IP as a second signal specifically
to raise that bar, but a determined user on a changing IP can still
get around it. Good enough for "stop accidental overuse," not
sufficient if real monetary cost were on the line later.

---

## 9. Local development

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env   # then edit .env with your real GEMINI_API_KEY
export $(cat .env | xargs)   # or use a tool like python-dotenv/direnv
uvicorn app:app --reload
```

Then open `frontend/index.html` in a browser, with `config.js` pointed
at `http://localhost:8000`.
