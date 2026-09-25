"""Getting past Alibaba's anti-bot on aliexpress.ru with a real browser.

The storefront's JSON endpoints are plain HTTP, but a "cold" client — one
that never ran the site's JavaScript — is soon answered with an
``_____tmd_____/punish`` captcha page. One short visit to the home page in
headless Chromium mints the cookies the anti-bot expects; after that a normal
HTTP session carrying those cookies and the same User-Agent is accepted for
weeks, so the browser runs only when a session is missing or got blocked.

Sometimes (typically after many fresh sessions from one IP) the anti-bot shows
an image captcha even to the browser. Headless mode then fails with a clear
error; with ``AE_HEADLESS=0`` a visible window opens and waits for a person to
solve it once.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

HOME_URL = "https://aliexpress.ru/"
PUNISH_MARKER = "_____tmd_____"


class AntibotError(RuntimeError):
    """The browser could not obtain a usable session (captcha, no browser)."""


@dataclass
class BrowserSession:
    user_agent: str
    brands: str  # Sec-CH-UA value the browser itself sends
    platform: str
    cookies: dict[str, str]
    minted_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BrowserSession":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


def mint(proxy: str | None = None, headless: bool = True, captcha_wait: float = 180.0) -> BrowserSession:
    """Open the storefront in Chromium and return the cookies it was given."""
    try:
        return _mint(proxy, headless, captcha_wait)
    except _BrowserMissing:
        _install_chromium()
        return _mint(proxy, headless, captcha_wait)


class _BrowserMissing(Exception):
    pass


def _on_captcha(page) -> bool:
    try:
        return PUNISH_MARKER in page.url or "punish" in page.url
    except Exception:  # page closed / navigating
        return False


def _mint(proxy: str | None, headless: bool, captcha_wait: float) -> BrowserSession:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                headless=headless,
                # The "new" headless mode is a real Chromium; the old headless
                # shell is fingerprinted much more easily.
                channel="chromium",
                proxy={"server": proxy} if proxy else None,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except PlaywrightError as e:
            if "Executable doesn't exist" in str(e):
                raise _BrowserMissing() from e
            raise AntibotError(f"Could not start Chromium: {e}") from e
        try:
            probe = browser.new_page()
            user_agent = probe.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
            probe.close()

            ctx = browser.new_context(
                user_agent=user_agent, locale="ru-RU", timezone_id="Europe/Moscow",
                viewport={"width": 1440, "height": 900},
            )
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = ctx.new_page()
            try:
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45_000)
            except PlaywrightError as e:
                raise AntibotError(f"aliexpress.ru did not load in the browser: {e}") from e
            # The anti-bot scripts set their cookies a few seconds after load.
            page.wait_for_timeout(5_000)
            if _on_captcha(page):
                if headless:
                    raise AntibotError(
                        "aliexpress.ru shows a captcha to a fresh browser session from this IP. "
                        "Wait 10-30 minutes, or set AE_HEADLESS=0 once: a browser window will "
                        "open and wait for you to solve the captcha; the session is then reused."
                    )
                log.warning("Captcha shown; waiting up to %.0fs for it to be solved in the window", captcha_wait)
                deadline = time.monotonic() + captcha_wait
                while _on_captcha(page) and time.monotonic() < deadline:
                    page.wait_for_timeout(1_000)
                if _on_captcha(page):
                    raise AntibotError("The captcha was not solved in time.")
                page.wait_for_timeout(3_000)
            cookies = {
                c["name"]: c["value"] for c in ctx.cookies()
                if c["domain"].lstrip(".").endswith("aliexpress.ru")
            }
            platform = page.evaluate("navigator.userAgentData ? navigator.userAgentData.platform : ''")
            brands = page.evaluate(
                "navigator.userAgentData ? navigator.userAgentData.brands"
                ".map(b => `\"${b.brand}\";v=\"${b.version}\"`).join(', ') : ''"
            )
        finally:
            browser.close()

    if len(cookies) < 3:
        raise AntibotError("aliexpress.ru set almost no cookies; the session would be rejected.")
    log.info("aliexpress.ru session minted in %.1fs (%d cookies)", time.monotonic() - started, len(cookies))
    return BrowserSession(user_agent=user_agent, brands=brands, platform=platform or "Linux", cookies=cookies)


def _install_chromium() -> None:
    """First run under uvx has no browser yet: fetch Playwright's Chromium.

    Output goes to stderr — stdout belongs to the MCP stdio transport.
    """
    log.warning("Playwright Chromium is not installed; installing it (one-time, ~150 MB)")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        stdout=sys.stderr, stderr=sys.stderr, check=False,
    )
    if result.returncode != 0:
        raise AntibotError(
            "Chromium is required for the aliexpress.ru anti-bot check and could not be "
            "installed automatically. Run: python -m playwright install chromium"
        )
