import json
import re
import threading
import time
import random
from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import FastAPI, HTTPException
from playwright.sync_api import sync_playwright

APP = FastAPI()

STATE = {
    "ok": False,
    "reading_m3": None,
    "read_at_iso": None,
    "scraped_at_utc": None,
    "error": None,
}

LAST_GOOD = None
LAST_CHANGE_TS = None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_options():
    with open("/data/options.json", "r", encoding="utf-8") as f:
        return json.load(f)


def parse_text(txt: str) -> Tuple[Optional[float], Optional[str]]:
    val = None
    ts = None

    m_val = re.search(r"aflæst til:\s*([0-9]+,[0-9]+)", txt)
    m_time = re.search(
        r"kl\.\s*([0-9]{1,2}\.[0-9]{2}),\s*d\.\s*([0-9]{2}\.[0-9]{2}\.[0-9]{4})",
        txt,
    )

    if m_val:
        val = float(m_val.group(1).replace(",", "."))

    if m_time:
        hhmm = m_time.group(1).replace(".", ":")
        d, m, y = m_time.group(2).split(".")
        ts = f"{y}-{m}-{d}T{hhmm}:00"

    return val, ts


def scrape(email: str, password: str) -> Tuple[float, str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto("https://www.minvandforsyning.dk/LoginIntermediate", wait_until="domcontentloaded")
        page.get_by_role("button", name="Fortsæt med Google/Microsoft/E-mail").click()

        page.wait_for_url(re.compile("id.ramboll.com"))
        page.fill("#signInName", email)
        page.fill("#password", password)
        page.click("#next")

        page.wait_for_url(re.compile("minvandforsyning.dk"))
        page.goto("https://www.minvandforsyning.dk/forbrug", wait_until="domcontentloaded")

        loc = page.locator("span.dynamicText").filter(has_text="aflæst til").first
        loc.wait_for(timeout=60000)
        txt = loc.inner_text()

        browser.close()

    return parse_text(txt)


def poll_loop():
    global LAST_GOOD, LAST_CHANGE_TS

    opts = load_options()

    email = opts["email"]
    password = opts["password"]

    idle_poll = int(opts.get("idle_poll_seconds", 1800))
    probe_poll = int(opts.get("probe_poll_seconds", 120))
    probe_after = int(opts.get("probe_after_minutes", 45)) * 60
    probe_max = int(opts.get("probe_max_minutes", 20)) * 60
    jitter = int(opts.get("jitter_seconds", 15))

    keep_last = bool(opts.get("keep_last_on_error", True))
    allow_decrease = bool(opts.get("allow_decrease", False))
    tol = float(opts.get("decrease_tolerance_m3", 0.0005))

    probe_started = None

    while True:
        try:
            val, read_at = scrape(email, password)

            if val is not None:
                if LAST_GOOD is None:
                    LAST_GOOD = val
                    LAST_CHANGE_TS = time.time()
                else:
                    if allow_decrease or val >= LAST_GOOD - tol:
                        if val > LAST_GOOD:
                            LAST_CHANGE_TS = time.time()
                        LAST_GOOD = max(LAST_GOOD, val)

                STATE.update(
                    {
                        "ok": True,
                        "reading_m3": LAST_GOOD,
                        "read_at_iso": read_at,
                        "scraped_at_utc": utc_now(),
                        "error": None,
                    }
                )

        except Exception as e:
            STATE["error"] = str(e)
            STATE["scraped_at_utc"] = utc_now()
            if not keep_last:
                STATE["ok"] = False

        now = time.time()
        in_probe = LAST_CHANGE_TS and (now - LAST_CHANGE_TS) > probe_after

        if in_probe:
            if probe_started is None:
                probe_started = now
            if (now - probe_started) > probe_max:
                in_probe = False
                probe_started = None

        base = probe_poll if in_probe else idle_poll
        sleep_for = base + random.randint(0, jitter)
        time.sleep(max(10, sleep_for))


@APP.get("/state")
def get_state():
    if STATE["reading_m3"] is None:
        raise HTTPException(status_code=503, detail=STATE)
    return STATE


if __name__ == "__main__":
    threading.Thread(target=poll_loop, daemon=True).start()

    import uvicorn

    port = int(load_options().get("port", 8080))
    uvicorn.run(APP, host="0.0.0.0", port=port)
