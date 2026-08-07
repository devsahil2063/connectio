# connectio — Claude referral link watcher

Watches X live search every 60s for fresh Claude referral links and posts new ones
to Slack. Runs 24/7 on GitHub Actions, free, no credit card.

## Why a browser and not an API

- X's **official API** dropped its free tier for new developers on 2026-02-06. It is
  pay-per-use at $0.005 per post read, and a search returning 25 posts bills as 25
  reads — about **$5,475/mo** at one search per minute.
- **Third-party scrapers** (Sorsa, GetXAPI, TwitterAPI.io, Apify) are 30–100x cheaper
  but still $22–$274/mo at this rate. Their "free" grants are one-time trial credits.
- The **unofficial Python libraries** (`twikit`, `twscrape`) are dead as of 2026-08.
  X moved to a new web bundle and fronts login + GraphQL with Cloudflare bot
  detection. Both fail with `403 Attention Required! | Cloudflare`.

A real headless Chromium gets through because it *is* the real client: real TLS
fingerprint, real JS execution, real transaction IDs. It costs nothing to run.

## 1. Slack (free, no card)

1. https://api.slack.com/apps → **Create New App** → *From scratch* → pick workspace
2. **Incoming Webhooks** → toggle **On** → **Add New Webhook to Workspace** → pick channel
3. Copy the URL — it goes in a repo secret later

The webhook *is* the bot. No token, no scopes, no `/invite`.

## 2. Install locally

```bash
python -m venv .venv && source .venv/Scripts/activate   # Windows bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env      # then fill it in
```

## 3. Get a session — copy cookies from your own browser

```bash
python make_session.py
```

**X refuses to log in from an automated browser**, whatever the account — it checks
`navigator.webdriver` and the AutomationControlled flag, and the login page is its
most guarded surface. So we never log in with Playwright. Instead:

1. Log into x.com in normal Chrome
2. **F12 → Application → Cookies → `https://x.com`**
3. Copy the values of `auth_token` and `ct0` into `.env` as `X_AUTH_TOKEN` / `X_CT0`
4. `python make_session.py` writes `storage_state.json` and prints a base64 blob

Those two cookies are full account access — treat them as a password. Logging out of
X in Chrome invalidates them, which doubles as the kill switch.

Browsing with an existing session is accepted where scripted login is not.
`bot.py` also hides the automation markers, and writes the refreshed session back to
the Actions cache each run, since X rotates cookies as you browse.

## 4. Test it locally

```bash
RUN_ONCE=1 python bot.py    # one cycle
HEADLESS=0 python bot.py    # watch the browser work
```

Do not skip this. If the DOM selectors are stale you want to know here, not in CI.

## 5. Deploy to GitHub Actions (free, 24/7, no card)

**Public repos get unlimited free Actions minutes.** Your secrets stay secret; only
the code is visible, and there is nothing sensitive in it.

1. Create a **public** repo and push this project.

2. Create a PAT for the chain: **Settings → Developer settings → Personal access
   tokens → Fine-grained** → scope it to this repo → permission **Actions: Read and
   write**. Copy it.

3. Repo **Settings → Secrets and variables → Actions**, add three secrets:

   | Secret | Value |
   |---|---|
   | `SLACK_WEBHOOK_URL` | webhook from step 1 |
   | `STORAGE_STATE_B64` | base64 blob from `make_session.py` |
   | `CHAIN_TOKEN` | the PAT from step 2 |

4. **Actions** tab → **watch** workflow → **Run workflow**.

### How the 24/7 part works

GitHub caps a job at 6 hours, so [watch.yml](.github/workflows/watch.yml) runs for
5h45m polling every 60s, then dispatches the next run before the cap. Four chained
runs per day = continuous coverage.

`CHAIN_TOKEN` is required because the default `GITHUB_TOKEN` cannot start a new
workflow run — dispatches made with it are silently ignored, which would break the
chain after the first link.

The `schedule:` cron every 6h is only a **safety net** that restarts the chain if a
link breaks. Scheduled runs are not the mechanism: GitHub explicitly says scheduled
events can be delayed or dropped, which is exactly wrong for links that expire.

Session and seen-links state ride between runs in `actions/cache`, keyed by run ID so
every run writes a fresh entry and `restore-keys` picks up the newest.

## Tuning

Set these as `env:` in the workflow, or in `.env` locally.

- `QUERY` — search term, default `claude referral`. One page load per cycle.
- `RESULT_COUNT` — results read per cycle, default 25.
- `MAX_AGE_MINUTES` — ignore posts older than this, default 360. Live search for this
  term is **sparse** — observed results were hours apart, not minutes — so a tight
  window silently drops everything. Dedupe is what prevents repeats; this only bounds
  the initial backfill.
- `POLL_SECONDS` — cycle interval, default 60.
- `MAX_RUNTIME_SECONDS` — exit for handoff. 0 disables. CI sets 20700.
- `HEALTH` — set `0` to skip the health port. CI sets 0.
- `LINK_RE` in [bot.py](bot.py) — URL pattern. Widen if Anthropic changes shape.

Dedupe is by **link**, so one link posted by 10 accounts = 1 Slack message. Last 2000
kept in `seen.json`.

## Failure modes, honestly

- **CI runner IP gets challenged.** The real risk of this design: the session was
  born on your home IP and is used from a datacenter. If X objects, the log says
  `session expired` and the job exits 1. No way to know without trying.
- **Session expires** → recopy the cookies, rerun `make_session.py`, update `STORAGE_STATE_B64`, re-run workflow.
- **X reskins the DOM** → `no results rendered`. Fix `EXTRACT_JS` in [bot.py](bot.py).
- **Account suspended** → use another account, recopy cookies.
- **Heavy Actions use** is more than GitHub optimizes for. It's your own repo's app,
  not a ToS violation, but they can throttle.
- Latency is ~1 min plus X's own search indexing lag.
