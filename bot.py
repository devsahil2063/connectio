"""
Claude referral link watcher.

Drives a headless Chromium (already logged in via login.py) against X's live
search every POLL_SECONDS, and pushes any new Claude referral link to Slack.

A real browser is used because X fronts its API with Cloudflare bot detection
that no plain HTTP client gets past. One browser is launched and reused for the
whole process lifetime; each cycle is a single page navigation.

Also serves a health endpoint on PORT so free hosts (Hugging Face Spaces) see
the container as alive and an uptime pinger can keep it awake.
"""

import asyncio
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

# Tweet text is full of emoji; a cp1252 console would crash the process on log().
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

STATE_DIR = Path(os.getenv("STATE_DIR", "."))
STATE_PATH = STATE_DIR / "seen.json"
STORAGE_STATE_PATH = Path(os.getenv("STORAGE_STATE_PATH", "storage_state.json"))

QUERY = os.getenv("QUERY", "claude referral")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
# Live search for this term is sparse - results are often hours apart, so a tight
# window drops everything. Dedupe by link is what actually prevents repeats.
MAX_AGE_MINUTES = int(os.getenv("MAX_AGE_MINUTES", "360"))
RESULT_COUNT = int(os.getenv("RESULT_COUNT", "25"))
# This many empty cycles in a row means the scrape is broken, not that X is quiet.
EMPTY_ALERT_CYCLES = int(os.getenv("EMPTY_ALERT_CYCLES", "30"))
PORT = int(os.getenv("PORT", "7860"))
HEADLESS = os.getenv("HEADLESS", "1") != "0"
RUN_ONCE = os.getenv("RUN_ONCE") == "1"
HEALTH = os.getenv("HEALTH", "1") != "0"
# Exit cleanly before a host kills us (GitHub Actions caps jobs at 6h). 0 = no cap.
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", "0"))
SEEN_LIMIT = 2000

SEARCH_URL = "https://x.com/search?q={q}&src=typed_query&f=live"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

LINK_RE = re.compile(
    r"https?://(?:www\.)?claude\.(?:ai|com)/(?:referral|invite|r)/[A-Za-z0-9_\-]+",
    re.IGNORECASE,
)

# JS runs in page context: pull the fields we need out of each result article.
EXTRACT_JS = """
() => Array.from(document.querySelectorAll('article[data-testid="tweet"]')).map(a => {
  const timeEl = a.querySelector('time');
  const statusLink = timeEl ? timeEl.closest('a') : null;
  return {
    text: (a.querySelector('[data-testid="tweetText"]') || {}).innerText || '',
    created: timeEl ? timeEl.getAttribute('datetime') : null,
    permalink: statusLink ? statusLink.href : null,
    hrefs: Array.from(a.querySelectorAll('a[href]')).map(x => x.href),
    // X renders a URL as several spans, so innerText comes back with spaces
    // injected mid-URL. textContent with whitespace stripped rebuilds it.
    linkTexts: Array.from(a.querySelectorAll('a[href]'))
      .map(x => (x.textContent || '').replace(/\\s+/g, '')),
    cards: Array.from(a.querySelectorAll('[data-testid="card.wrapper"] a[href]')).map(x => x.href)
  };
})
"""


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def load_seen() -> list[str]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            log("seen.json corrupt, starting fresh")
    return []


def save_seen(seen: list[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(seen[-SEEN_LIMIT:]))


def ensure_storage_state() -> None:
    """Hosts with ephemeral disks supply the session as a base64 env var."""
    if STORAGE_STATE_PATH.exists():
        return
    blob = os.getenv("STORAGE_STATE_B64")
    if not blob:
        sys.exit(
            f"No {STORAGE_STATE_PATH} and no STORAGE_STATE_B64 set.\n"
            "Run `python login.py` on your own machine first."
        )
    STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORAGE_STATE_PATH.write_bytes(base64.b64decode(blob))
    log(f"restored session from STORAGE_STATE_B64 -> {STORAGE_STATE_PATH}")


async def resolve_short_links(hrefs: list[str]) -> list[str]:
    """X renders outbound links as t.co redirects; follow them to the real URL."""
    out = []
    tco = [h for h in hrefs if h.startswith("https://t.co/")]
    if not tco:
        return out
    async with httpx.AsyncClient(follow_redirects=False, timeout=10) as c:
        for url in tco[:10]:
            try:
                r = await c.get(url, headers={"User-Agent": USER_AGENT})
                target = r.headers.get("location", "")
                # t.co serves an HTML interstitial for some links.
                if not target:
                    m = re.search(r'URL=(https?://[^"\']+)', r.text)
                    target = m.group(1) if m else ""
                if target:
                    out.append(target)
            except Exception:
                continue
    return out


async def extract_links(item: dict) -> list[str]:
    """Referral links from tweet text, card links, and resolved t.co redirects."""
    blob = item.get("text") or ""
    direct = [h for h in item.get("hrefs", []) + item.get("cards", []) if "claude." in h]
    blob += " " + " ".join(direct) + " " + " ".join(item.get("linkTexts", []))

    found = LINK_RE.findall(blob)
    if not found:
        # Link may be hidden behind a t.co shortener; only pay that cost if needed.
        resolved = await resolve_short_links(item.get("hrefs", []))
        found = LINK_RE.findall(" ".join(resolved))
    return list(dict.fromkeys(found))


def alert_slack(text: str) -> None:
    try:
        httpx.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=15).raise_for_status()
    except Exception as e:
        log(f"slack alert failed: {type(e).__name__}: {e}")


def post_to_slack(item: dict, links: list[str]) -> None:
    permalink = item.get("permalink") or "https://x.com/search"
    handle = "unknown"
    m = re.search(r"x\.com/([^/]+)/status/", permalink)
    if m:
        handle = m.group(1)
    link_lines = "\n".join(f"• <{l}|{l}>" for l in links)
    payload = {
        "text": f"Claude referral link from @{handle}: {links[0]}",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*New Claude referral link* — <{permalink}|@{handle}>\n{link_lines}",
                },
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": (item.get("text") or "")[:280] or "_no text_"}],
            },
        ],
    }
    r = httpx.post(SLACK_WEBHOOK_URL, json=payload, timeout=15)
    r.raise_for_status()


async def poll(page, seen: list[str]) -> int:
    seen_set = set(seen)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=MAX_AGE_MINUTES)
    sent = 0

    url = SEARCH_URL.format(q=QUERY.replace(" ", "%20"))
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        await page.wait_for_selector('article[data-testid="tweet"]', timeout=20000)
    except Exception:
        # Genuinely-empty results and a session/challenge wall look the same here.
        if "/login" in page.url or "/logout" in page.url or await page.query_selector(
            '[data-testid="loginButton"], [data-testid="login"], a[href="/login"]'
        ):
            raise RuntimeError("session expired - rerun make_session.py and update STORAGE_STATE_B64")
        # Say what the page actually shows, so a wall is distinguishable from quiet.
        title = await page.title()
        body = await page.evaluate("() => (document.body ? document.body.innerText : '')")
        log(f"no results rendered this cycle | url={page.url} | title={title!r} | page={' '.join(body.split())[:200]!r}")
        return -1

    items = (await page.evaluate(EXTRACT_JS))[:RESULT_COUNT]
    # Unattended, this line is the only proof the scrape is still working.
    newest = max((i.get("created") or "" for i in items), default="?")
    log(f"cycle: {len(items)} results, newest {newest}")

    for item in items:
        created = item.get("created")
        if created:
            when = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if when < cutoff:
                continue
        links = await extract_links(item)
        fresh = [l for l in links if l not in seen_set]
        if not fresh:
            continue
        try:
            post_to_slack(item, fresh)
        except Exception as e:
            log(f"slack post failed: {type(e).__name__}: {e}")
            continue
        for l in fresh:
            seen_set.add(l)
            seen.append(l)
        sent += 1
        log(f"sent {len(fresh)} link(s): {', '.join(fresh)}")

    if sent:
        save_seen(seen)
    return sent


async def health_server() -> None:
    """Minimal HTTP 200 so the host sees a live port and pingers can wake it."""

    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = b"connectio: alive"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    log(f"health endpoint on :{PORT}")
    async with server:
        await server.serve_forever()


async def run_watcher() -> None:
    ensure_storage_state()
    seen = load_seen()
    log(f"query: {QUERY!r} | {len(seen)} links already seen")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                # Without this Chromium advertises itself as automated, which X
                # checks for. It is why the scripted login gets refused.
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx = await browser.new_context(
            storage_state=str(STORAGE_STATE_PATH),
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id=os.getenv("TZ_ID", "Asia/Kolkata"),
        )
        # navigator.webdriver is true under automation; page JS can read it.
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await ctx.new_page()
        launched = time.monotonic()

        async def shutdown(code: int | None = None):
            # Persist the session: X rotates cookies, so the state we started
            # with goes stale if we never write it back.
            try:
                await ctx.storage_state(path=str(STORAGE_STATE_PATH))
            except Exception as e:
                log(f"could not save session: {type(e).__name__}: {e}")
            await browser.close()
            if code is not None:
                sys.exit(code)

        empty_streak = 0
        while True:
            started = time.monotonic()
            try:
                result = await poll(page, seen)
                empty_streak = empty_streak + 1 if result < 0 else 0
                if empty_streak == EMPTY_ALERT_CYCLES:
                    STATE_DIR.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(STATE_DIR / "empty.png"))
                    alert_slack(
                        f":warning: connectio: X search showed no tweets for {empty_streak} "
                        "cycles in a row. The session is probably dead - refresh "
                        "STORAGE_STATE_B64. Screenshot is in the run's artifacts."
                    )
            except RuntimeError as e:
                log(f"FATAL: {e}")
                alert_slack(f":rotating_light: connectio stopped: {e}")
                await shutdown(1)
            except Exception as e:
                log(f"poll cycle error: {type(e).__name__}: {e}")
            if RUN_ONCE:
                await shutdown()
                return
            if MAX_RUNTIME_SECONDS and time.monotonic() - launched > MAX_RUNTIME_SECONDS:
                log(f"hit MAX_RUNTIME_SECONDS ({MAX_RUNTIME_SECONDS}s), exiting for handoff")
                await shutdown()
                return
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(5, POLL_SECONDS - elapsed))


async def main() -> None:
    if RUN_ONCE or not HEALTH:
        await run_watcher()
        return
    # gather() so the health endpoint dies with the watcher, not after it.
    watcher = asyncio.create_task(run_watcher())
    health = asyncio.create_task(health_server())
    await watcher
    health.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
