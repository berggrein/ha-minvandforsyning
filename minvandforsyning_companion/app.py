import json
import os
import re
import threading
import time
import random
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from fastapi import FastAPI, HTTPException
from playwright.sync_api import sync_playwright

APP = FastAPI()

# --- Persistence (keeps sensor stable across restarts) ------------------------

LAST_GOOD_PATH = os.environ.get("LAST_GOOD_PATH", "/data/last_good.json")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_last_good() -> dict[str, Any]:
    try:
        with open(LAST_GOOD_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("reading_m3") is not None:
            return data
    except Exception:
        pass
    return {
        "reading_m3": None,
        "read_at_iso": None,
        "updated_utc": None,
    }


def save_last_good(data: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(LAST_GOOD_PATH), exist_ok=True)
        with open(LAST_GOOD_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        # Never crash polling because persistence failed
        pass


LAST_GOOD: dict[str, Any] = load_last_good()

# --- Public state returned to HA integration ---------------------------------

STATE: dict[str, Any] = {
    "ok": False,
    "error": "Not scraped yet",
    "reading_m3": None,
    "read_at_iso": None,
    "scraped_at_utc": None,
    "raw": None,
    # Diagnostics
    "stale": True,
    "mode": "idle",
    "next_poll_in_seconds": None,
    "last_success_utc": None,
    "fail_count": 0,
}


def load_options() -> dict[str, Any]:
    path = os.environ.get("ADDON_OPTIONS", "/data/options.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(f"Kunne ikke læse options.json ({path}): {e}")


def parse_dom_text(txt: str) -> Tuple[Optional[float], Optional[str], str]:
    """
    Extracts:
      - reading_m3 (float) from: "aflæst til: 442,675 m3"
      - read_at_iso from: "kl. 22.40, d. 17.12.2025"
    """
    raw = re.sub(r"\s+", " ", (txt or "")).strip()

    m_val = re.search(r"aflæst til:\s*([0-9]+,[0-9]+)", raw, re.IGNORECASE)
    m_time = re.search(
        r"kl\.\s*([0-9]{1,2}\.[0-9]{2}),\s*d\.\s*([0-9]{2}\.[0-9]{2}\.[0-9]{4})",
        raw,
        re.IGNORECASE,
    )

    reading_m3: Optional[float] = None
    read_at_iso: Optional[str] = None

    if m_val:
        reading_m3 = float(m_val.group(1).replace(",", "."))

    if m_time:
        hhmm = m_time.group(1).replace(".", ":")
        ddmmyyyy = m_time.group(2)
        day, month, year = ddmmyyyy.split(".")
        read_at_iso = f"{year}-{month}-{day}T{hhmm}:00"

    return reading_m3, read_at_iso, raw


def scrape_once(email: str, password: str) -> dict[str, Any]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(
            "https://www.minvandforsyning.dk/LoginIntermediate",
            wait_until="domcontentloaded",
        )
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
    scraped_at_utc = _utc_now_iso()

    return {
        "ok": True,
        "error": None,
        "reading_m3": reading_m3,
        "read_at_iso": read_at_iso,
        "scraped_at_utc": scraped_at_utc,
        "raw": raw,
    }


def _update_last_good(
    new_reading: Optional[float],
    new_read_at_iso: Optional[str],
    allow_decrease: bool,
    decrease_tolerance_m3: float,
) -> bool:
    global LAST_GOOD
    if new_reading is None:
        return False

    old = LAST_GOOD.get("reading_m3")
    if old is None:
        LAST_GOOD = {
            "reading_m3": float(new_reading),
            "read_at_iso": new_read_at_iso,
            "updated_utc": _utc_now_iso(),
        }
        save_last_good(LAST_GOOD)
        return True

    if allow_decrease:
        ok = True
    else:
        ok = new_reading >= (float(old) - float(decrease_tolerance_m3))

    if ok:
        # Never go backwards
        clamped = max(float(new_reading), float(old))
        LAST_GOOD = {
            "reading_m3": clamped,
            "read_at_iso": new_read_at_iso or LAST_GOOD.get("read_at_iso"),
            "updated_utc": _utc_now_iso(),
        }
        save_last_good(LAST_GOOD)
        return True

    return False


def _apply_result_to_state(
    result: Optional[dict[str, Any]],
    error: Optional[str],
    keep_last_on_error: bool,
    allow_decrease: bool,
    decrease_tolerance_m3: float,
) -> None:
    global STATE

    if result and result.get("ok"):
        updated = _update_last_good(
            result.get("reading_m3"),
            result.get("read_at_iso"),
            allow_decrease,
            decrease_tolerance_m3,
        )
        if updated:
            STATE["last_success_utc"] = _utc_now_iso()
            STATE["fail_count"] = 0

        STATE.update(result)
        STATE["stale"] = False
        STATE["ok"] = LAST_GOOD.get("reading_m3") is not None
        STATE["reading_m3"] = LAST_GOOD.get("reading_m3")
        STATE["read_at_iso"] = LAST_GOOD.get("read_at_iso")
        STATE["error"] = None
        return

    # Failure path
    STATE["fail_count"] = int(STATE.get("fail_count") or 0) + 1
    STATE["scraped_at_utc"] = _utc_now_iso()
    STATE["error"] = error or (
        result.get("error") if isinstance(result, dict) else "Unknown error"
    )
    STATE["raw"] = None
    STATE["stale"] = True

    if keep_last_on_error and LAST_GOOD.get("reading_m3") is not None:
        STATE["ok"] = True
        STATE["reading_m3"] = LAST_GOOD.get("reading_m3")
        STATE["read_at_iso"] = LAST_GOOD.get("read_at_iso")
    else:
        STATE["ok"] = False
        STATE["reading_m3"] = None
        STATE["read_at_iso"] = None


def _compute_mode(last_change_utc: Optional[float], probe_after_minutes: int) -> str:
    if last_change_utc is None:
        return "probe"
    age = time.time() - last_change_utc
    return "probe" if age >= (probe_after_minutes * 60) else "idle"


def _compute_sleep_seconds(
    mode: str,
    idle_poll_seconds: int,
    probe_poll_seconds: int,
    min_poll_seconds: int,
    jitter_seconds: int,
    fail_count: int,
) -> int:
    base = idle_poll_seconds if mode == "idle" else probe_poll_seconds

    # Backoff on repeated errors (cap at 15 minutes)
    if fail_count > 0:
        base = int(base * min(4.0, 1.0 + (fail_count * 0.5)))
        base = min(base, 900)

    base = max(int(base), int(min_poll_seconds))
    jitter = random.randint(0, max(0, int(jitter_seconds)))
    return base + jitter


def poll_loop(email: str, password: str, options: dict[str, Any]) -> None:
    global STATE

    idle_poll_seconds = int(options.get("idle_poll_seconds", 1800))
    probe_after_minutes = int(options.get("probe_after_minutes", 45))
    probe_poll_seconds = int(options.get("probe_poll_seconds", 120))
    probe_max_minutes = int(options.get("probe_max_minutes", 20))
    min_poll_seconds = int(options.get("min_poll_seconds", 30))
    jitter_seconds = int(options.get("jitter_seconds", 15))

    keep_last_on_error = bool(options.get("keep_last_on_error", True))
    allow_decrease = bool(options.get("allow_decrease", False))
    decrease_tolerance_m3 = float(options.get("decrease_tolerance_m3", 0.0005))

    last_change_utc: Optional[float] = None
    probe_started_utc: Optional[float] = None
    last_seen_read_at: Optional[str] = LAST_GOOD.get("read_at_iso")

    while True:
        try:
            result = scrape_once(email, password)
            _apply_result_to_state(
                result,
                None,
                keep_last_on_error,
                allow_decrease,
                decrease_tolerance_m3,
            )

            new_read_at = STATE.get("read_at_iso")
            new_val = STATE.get("reading_m3")

            changed = False
            if new_read_at and new_read_at != last_seen_read_at:
                changed = True
                last_seen_read_at = new_read_at
            elif last_seen_read_at is None and new_val is not None:
                changed = True
                last_seen_read_at = new_read_at

            if changed:
                last_change_utc = time.time()
                probe_started_utc = None

        except Exception as e:
            _apply_result_to_state(
                None,
                str(e),
                keep_last_on_error,
                allow_decrease,
                decrease_tolerance_m3,
            )

        mode = _compute_mode(last_change_utc, probe_after_minutes)

        if mode == "probe":
            if probe_started_utc is None:
                probe_started_utc = time.time()
            elif (time.time() - probe_started_utc) > (probe_max_minutes * 60):
                mode = "idle"
                probe_started_utc = None

        STATE["mode"] = mode

        sleep_seconds = _compute_sleep_seconds(
            mode=mode,
            idle_poll_seconds=idle_poll_seconds,
            probe_poll_seconds=probe_poll_seconds,
            min_poll_seconds=min_poll_seconds,
            jitter_seconds=jitter_seconds,
            fail_count=int(STATE.get("fail_count") or 0),
        )
        STATE["next_poll_in_seconds"] = int(sleep_seconds)
        time.sleep(int(sleep_seconds))


@APP.get("/state")
def get_state():
    # If we've never had a good reading, return 503 so you notice.
    if LAST_GOOD.get("reading_m3") is None:
        raise HTTPException(status_code=503, detail=STATE)
    return STATE


@APP.get("/state_raw")
def get_state_raw():
    return STATE


if __name__ == "__main__":
    opts = load_options()
    email = opts.get("email", "")
    password = opts.get("password", "")

    if not email or not password:
        raise RuntimeError("Du skal udfylde email og password i add-on options")

    t = threading.Thread(target=poll_loop, args=(email, password, opts), daemon=True)
    t.start()

    import uvicorn

    uvicorn.run(APP, host="0.0.0.0", port=8080)
