import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from playwright.sync_api import sync_playwright

APP = FastAPI()

STATE = {
    "ok": False,
    "error": "Not scraped yet",
    "reading_m3": None,
    "read_at_iso": None,
    "scraped_at_utc": None,
    "raw": None,
}


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


def _sleep_with_jitter(base_seconds: int, jitter_seconds: int):
    """Sleep base_seconds plus a small random jitter.

    Jitter avoids many installations hitting the site at the exact same second.
    """
    if jitter_seconds <= 0:
        time.sleep(base_seconds)
        return

    # No imports at top-level to keep startup fast.
    import random

    j = random.randint(0, int(jitter_seconds))
    time.sleep(int(base_seconds) + j)


def load_options():
    path = os.environ.get("ADDON_OPTIONS", "/data/options.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(f"Kunne ikke læse options.json ({path}): {e}")


def parse_dom_text(txt: str):
    txt = re.sub(r"\s+", " ", txt).strip()

    m_val = re.search(r"aflæst til:\s*([0-9]+,[0-9]+)", txt, re.IGNORECASE)
    m_dt = re.search(
        r"kl\.\s*([0-9]{1,2})\.(\d{2}),\s*d\.\s*(\d{2})\.(\d{2})\.(\d{4})",
        txt,
        re.IGNORECASE,
    )

    if not m_val:
        raise ValueError(f"Kunne ikke finde 'aflæst til' i tekst: {txt!r}")

    reading_m3 = float(m_val.group(1).replace(",", "."))

    read_at_iso = None
    if m_dt:
        hh, mm, dd, MM, yyyy = m_dt.group(1), m_dt.group(2), m_dt.group(3), m_dt.group(4), m_dt.group(5)
        read_at_iso = f"{yyyy}-{MM}-{dd}T{hh.zfill(2)}:{mm}:00"

    return reading_m3, read_at_iso, txt


def scrape_once(email: str, password: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto("https://www.minvandforsyning.dk/LoginIntermediate", wait_until="domcontentloaded")
        page.get_by_role("button", name="Fortsæt med Google/Microsoft/E-mail").click()

        page.wait_for_url(re.compile(r"^https://id\.ramboll\.com/"), timeout=60_000)
        page.fill("#signInName", email)
        page.fill("#password", password)
        page.click("#next")

        page.wait_for_url(re.compile(r"^https://www\.minvandforsyning\.dk/"), timeout=60_000)
        page.goto("https://www.minvandforsyning.dk/forbrug", wait_until="domcontentloaded")

        loc = page.locator("span.dynamicText").filter(has_text="aflæst til").first
        loc.wait_for(timeout=60_000)
        txt = loc.inner_text()

        browser.close()

    if not txt:
        raise RuntimeError("Fandt ikke 'aflæst til' tekst i DOM efter login")

    reading_m3, read_at_iso, raw = parse_dom_text(txt)
    scraped_at_utc = _now_utc_iso()

    return {
        "ok": True,
        "error": None,
        "reading_m3": reading_m3,
        "read_at_iso": read_at_iso,
        "scraped_at_utc": scraped_at_utc,
        "raw": raw,
    }


def poll_loop(
    email: str,
    password: str,
    idle_poll_seconds: int,
    probe_after_minutes: int,
    probe_poll_seconds: int,
    probe_max_minutes: int,
    min_poll_seconds: int,
    jitter_seconds: int,
):
    global STATE

    # Adaptive polling state
    last_key = None  # (read_at_iso, reading_m3)
    last_change_ts = None  # when we detected new data (epoch seconds)
    probe_started_ts = None
    mode = "idle"  # idle | probe

    # Error backoff (so we don't hammer on transient failures)
    err_backoff = min_poll_seconds

    # Clamp configs to sensible bounds
    idle_poll_seconds = _clamp_int(idle_poll_seconds, min_poll_seconds, 24 * 3600)
    probe_poll_seconds = _clamp_int(probe_poll_seconds, min_poll_seconds, 3600)
    probe_after_s = _clamp_int(probe_after_minutes, 1, 24 * 60) * 60
    probe_max_s = _clamp_int(probe_max_minutes, 1, 24 * 60) * 60
    jitter_seconds = _clamp_int(jitter_seconds, 0, 300)

    print(
        "[poll] Adaptive polling enabled. "
        f"idle_poll_seconds={idle_poll_seconds}, "
        f"probe_after_minutes={probe_after_minutes}, "
        f"probe_poll_seconds={probe_poll_seconds}, "
        f"probe_max_minutes={probe_max_minutes}, "
        f"min_poll_seconds={min_poll_seconds}, "
        f"jitter_seconds={jitter_seconds}"
    )

    while True:
        now = time.time()

        try:
            result = scrape_once(email, password)
            STATE = result
            err_backoff = min_poll_seconds

            key = (result.get("read_at_iso"), result.get("reading_m3"))
            if key != last_key and result.get("ok"):
                last_key = key
                last_change_ts = now
                mode = "idle"
                probe_started_ts = None
                print(f"[poll] New data detected: {key}")

        except Exception as e:
            STATE = {
                "ok": False,
                "error": str(e),
                "reading_m3": None,
                "read_at_iso": None,
                "scraped_at_utc": _now_utc_iso(),
                "raw": None,
            }
            print(f"[poll] Scrape failed: {e}")

            # Backoff on errors, up to idle_poll_seconds
            err_backoff = min(err_backoff * 2, idle_poll_seconds)
            _sleep_with_jitter(err_backoff, jitter_seconds)
            continue

        # Decide next sleep interval
        if last_change_ts is None:
            # We have not seen a successful value change yet.
            sleep_s = max(min_poll_seconds, min(idle_poll_seconds, 300))
        else:
            age = now - last_change_ts

            if mode == "idle":
                if age >= probe_after_s:
                    mode = "probe"
                    probe_started_ts = now
                    sleep_s = probe_poll_seconds
                    print("[poll] Entering probe mode")
                else:
                    sleep_s = idle_poll_seconds

            else:  # probe
                assert probe_started_ts is not None
                if (now - probe_started_ts) >= probe_max_s:
                    mode = "idle"
                    probe_started_ts = None
                    sleep_s = idle_poll_seconds
                    print("[poll] Probe window expired, returning to idle")
                else:
                    sleep_s = probe_poll_seconds

        sleep_s = _clamp_int(sleep_s, min_poll_seconds, 24 * 3600)
        _sleep_with_jitter(sleep_s, jitter_seconds)


@APP.get("/state")
def get_state():
    if not STATE.get("ok"):
        raise HTTPException(status_code=503, detail=STATE)
    return STATE


@APP.get("/state_raw")
def get_state_raw():
    return STATE


if __name__ == "__main__":
    opts = load_options()
    email = opts.get("email", "")
    password = opts.get("password", "")
    idle_poll_seconds = int(opts.get("idle_poll_seconds", 1800))
    probe_after_minutes = int(opts.get("probe_after_minutes", 45))
    probe_poll_seconds = int(opts.get("probe_poll_seconds", 120))
    probe_max_minutes = int(opts.get("probe_max_minutes", 20))
    min_poll_seconds = int(opts.get("min_poll_seconds", 30))
    jitter_seconds = int(opts.get("jitter_seconds", 15))

    if not email or not password:
        raise RuntimeError("Du skal udfylde email og password i add-on options")

    t = threading.Thread(
        target=poll_loop,
        args=(
            email,
            password,
            idle_poll_seconds,
            probe_after_minutes,
            probe_poll_seconds,
            probe_max_minutes,
            min_poll_seconds,
            jitter_seconds,
        ),
        daemon=True,
    )
    t.start()

    import uvicorn
    uvicorn.run(APP, host="0.0.0.0", port=8080)
