"""
Build storage_state.json from cookies you copy out of your own browser.

X blocks logging in from an automated browser, so we never log in with Playwright.
Instead you log in normally in Chrome, copy two cookies, and this builds the
session file Playwright will reuse.

Where to get the cookies:
  1. Log into x.com in normal Chrome
  2. F12 -> Application tab -> Storage -> Cookies -> https://x.com
  3. Copy the Value of `auth_token` and of `ct0`
  4. Put them in .env as X_AUTH_TOKEN and X_CT0
  5. python make_session.py

These two cookies are full account access. Treat them like a password.
"""

import base64
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

STATE_FILE = Path(os.getenv("STORAGE_STATE_PATH", "storage_state.json"))
AUTH_TOKEN = os.getenv("X_AUTH_TOKEN", "").strip()
CT0 = os.getenv("X_CT0", "").strip()

if not AUTH_TOKEN or not CT0:
    sys.exit(
        "Set X_AUTH_TOKEN and X_CT0 in .env first.\n"
        "Chrome -> F12 -> Application -> Cookies -> https://x.com -> copy auth_token and ct0"
    )

# One year out; X will refresh these as the bot browses.
expires = int(time.time()) + 365 * 24 * 3600


def cookie(name: str, value: str, http_only: bool) -> dict:
    return {
        "name": name,
        "value": value,
        "domain": ".x.com",
        "path": "/",
        "expires": expires,
        "httpOnly": http_only,
        "secure": True,
        "sameSite": "None" if name == "ct0" else "Lax",
    }


state = {
    "cookies": [
        cookie("auth_token", AUTH_TOKEN, True),
        cookie("ct0", CT0, False),
    ],
    "origins": [],
}

STATE_FILE.write_text(json.dumps(state, indent=2))
print(f"wrote {STATE_FILE}")

blob = base64.b64encode(STATE_FILE.read_bytes()).decode()
print("\n--- STORAGE_STATE_B64 (paste into the GitHub repo secret) ---")
print(blob)
print("--- end ---")
print("\nNow verify it works:  RUN_ONCE=1 python bot.py")
