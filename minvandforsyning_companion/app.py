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

        # Vent på at teksten findes i DOM (CSP-safe, ingen eval)
        loc = page.locator("span.dynamicText", has_text="aflæst til").first
        loc.wait_for(state="visible", timeout=60_000)

        txt = loc.inner_text()
                .map(e => e.innerText || '')
                .find(t => t.includes('aflæst til')) || ''"""
        )

        browser.close()

    if not txt:
        raise RuntimeError("Fandt ikke 'aflæst til' tekst i DOM efter login")

    reading_m3, read_at_iso, raw = parse_dom_text(txt)
    scraped_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "ok": True,
        "error": None,
        "reading_m3": reading_m3,
        "read_at_iso": read_at_iso,
        "scraped_at_utc": scraped_at_utc,
        "raw": raw,
    }

def poll_loop(email: str, password: str, poll_seconds: int):
    global STATE
    while True:
        try:
            STATE = scrape_once(email, password)
        except Exception as e:
            STATE = {
                "ok": False,
                "error": str(e),
                "reading_m3": None,
                "read_at_iso": None,
                "scraped_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "raw": None,
            }
        time.sleep(max(60, int(poll_seconds)))

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
    poll_seconds = int(opts.get("poll_seconds", 1800))

    if not email or not password:
        raise RuntimeError("Du skal udfylde email og password i add-on options")

    t = threading.Thread(target=poll_loop, args=(email, password, poll_seconds), daemon=True)
    t.start()

    import uvicorn
    uvicorn.run(APP, host="0.0.0.0", port=8080)
