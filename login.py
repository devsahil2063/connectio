"""
One-time login. Run this on YOUR OWN PC, not on the server.

Opens a real Chromium window, logs into X with the burner account, and waits
for you to clear any captcha / email code by hand. Saves the session to
storage_state.json and prints a base64 blob to paste into Hugging Face Secrets.

    python login.py
"""

import asyncio
import base64
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

STATE_FILE = Path(os.getenv("STORAGE_STATE_PATH", "storage_state.json"))
USERNAME = os.environ["X_USERNAME"]
EMAIL = os.environ["X_EMAIL"]
PASSWORD = os.environ["X_PASSWORD"]


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await ctx.new_page()

        print("opening login page...")
        await page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded")

        # Best-effort autofill. If X changes the flow, just type it yourself —
        # the script waits for the logged-in state either way.
        try:
            await page.get_by_role("textbox").first.fill(USERNAME, timeout=20000)
            await page.get_by_role("button", name="Next").click(timeout=10000)
            await page.wait_for_timeout(2000)

            # X sometimes interjects an email/phone verification step.
            boxes = page.get_by_role("textbox")
            if await boxes.count() and await boxes.first.get_attribute("type") != "password":
                val = await boxes.first.input_value()
                if not val:
                    await boxes.first.fill(EMAIL)
                    await page.get_by_role("button", name="Next").click()
                    await page.wait_for_timeout(2000)

            await page.get_by_label("Password", exact=False).fill(PASSWORD, timeout=15000)
            await page.get_by_role("button", name="Log in").click(timeout=10000)
        except Exception as e:
            print(f"autofill stopped ({type(e).__name__}) - finish the login by hand")

        print()
        print("=" * 60)
        print("Finish anything left in the browser window: captcha, email code,")
        print("2FA. This waits until x.com/home loads. Do not close the window.")
        print("=" * 60)

        # Wait (up to 10 min) for the timeline, which only renders once logged in.
        await page.wait_for_selector('[data-testid="primaryColumn"]', timeout=600000)
        print("logged in")

        await ctx.storage_state(path=str(STATE_FILE))
        await browser.close()

    blob = base64.b64encode(STATE_FILE.read_bytes()).decode()
    print(f"\nsaved {STATE_FILE} ({STATE_FILE.stat().st_size} bytes)")
    print("\n--- paste this into HF Space secret STORAGE_STATE_B64 ---")
    print(blob)
    print("--- end ---")


if __name__ == "__main__":
    asyncio.run(main())
