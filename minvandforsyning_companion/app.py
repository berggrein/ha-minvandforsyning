import json
import os
import re
import threading
import time
import random
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from playwright.sync_api import sync_playwright

APP = FastAPI()

# -----------------------------------------------------------------------------
# Files (Supervisor writes /data/options.json; we keep extra UI-only overrides)
# -----------------------------------------------------------------------------
OPTIONS_PATH = os.environ.get("ADDON_OPTIONS", "/data/options.json")
UI_OVERRIDES_PATH = os.environ.get("UI_OVERRIDES_PATH", "/data/ui_overrides.json")
LAST_GOOD_PATH = os.environ.get("LAST_GOOD_PATH", "/data/last_good.json")

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: str, data: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        # Never crash because of persistence
        pass


def load_options() -> dict[str, Any]:
    base = _read_json(OPTIONS_PATH)
    overrides = _read_json(UI_OVERRIDES_PATH)
    merged = dict(base)
    # Only allow a small subset of overrides from the UI
    for k in (
        "idle_poll_seconds",
        "probe_after_minutes",
        "probe_poll_seconds",
        "probe_max_minutes",
        "min_poll_seconds",
        "jitter_seconds",
        "keep_last_on_error",
        "allow_decrease",
        "decrease_tolerance_m3",
    ):
        if k in overrides:
            merged[k] = overrides[k]
    return merged


def load_last_good() -> dict[str, Any]:
    data = _read_json(LAST_GOOD_PATH)
    if data.get("reading_m3") is not None:
        return data
    return {"reading_m3": None, "read_at_iso": None, "updated_utc": None}


def save_last_good(data: dict[str, Any]) -> None:
    _write_json(LAST_GOOD_PATH, data)


LAST_GOOD: dict[str, Any] = load_last_good()

STATE: dict[str, Any] = {
    "ok": False,
    "error": "Not scraped yet",
    "reading_m3": None,
    "read_at_iso": None,
    "scraped_at_utc": None,
    "raw": None,
    "stale": True,
    "mode": "idle",
    "next_poll_in_seconds": None,
    "last_success_utc": None,
    "fail_count": 0,
}


def parse_dom_text(txt: str) -> Tuple[Optional[float], Optional[str], str]:
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

    STATE["fail_count"] = int(STATE.get("fail_count") or 0) + 1
    STATE["scraped_at_utc"] = _utc_now_iso()
    STATE["error"] = error or (result.get("error") if isinstance(result, dict) else "Unknown error")
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

    if fail_count > 0:
        base = int(base * min(4.0, 1.0 + (fail_count * 0.5)))
        base = min(base, 900)

    base = max(int(base), int(min_poll_seconds))
    jitter = random.randint(0, max(0, int(jitter_seconds)))
    return base + jitter


def poll_loop(email: str, password: str) -> None:
    global STATE

    last_change_utc: Optional[float] = None
    probe_started_utc: Optional[float] = None
    last_seen_read_at: Optional[str] = LAST_GOOD.get("read_at_iso")

    while True:
        opts = load_options()

        idle_poll_seconds = int(opts.get("idle_poll_seconds", 1800))
        probe_after_minutes = int(opts.get("probe_after_minutes", 45))
        probe_poll_seconds = int(opts.get("probe_poll_seconds", 120))
        probe_max_minutes = int(opts.get("probe_max_minutes", 20))
        min_poll_seconds = int(opts.get("min_poll_seconds", 30))
        jitter_seconds = int(opts.get("jitter_seconds", 15))

        keep_last_on_error = bool(opts.get("keep_last_on_error", True))
        allow_decrease = bool(opts.get("allow_decrease", False))
        decrease_tolerance_m3 = float(opts.get("decrease_tolerance_m3", 0.0005))

        try:
            result = scrape_once(email, password)
            _apply_result_to_state(result, None, keep_last_on_error, allow_decrease, decrease_tolerance_m3)

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
            _apply_result_to_state(None, str(e), keep_last_on_error, allow_decrease, decrease_tolerance_m3)

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


# -----------------------------------------------------------------------------
# API endpoints used by the HA integration
# -----------------------------------------------------------------------------

@APP.get("/state")
def get_state():
    if LAST_GOOD.get("reading_m3") is None:
        raise HTTPException(status_code=503, detail=STATE)
    return STATE


@APP.get("/state_raw")
def get_state_raw():
    return STATE


# -----------------------------------------------------------------------------
# Simple UI (Ingress)
# -----------------------------------------------------------------------------

_UI_CSS = """
<style>
body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; margin: 0; padding: 0; }
header { padding: 16px 18px; border-bottom: 1px solid rgba(0,0,0,0.08); display:flex; align-items:center; justify-content:space-between; gap: 12px; }
h1 { font-size: 18px; margin: 0; }
main { padding: 18px; max-width: 900px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
.card { border: 1px solid rgba(0,0,0,0.08); border-radius: 12px; padding: 12px; }
.label { font-size: 12px; opacity: 0.75; }
.value { font-size: 18px; font-weight: 650; margin-top: 2px; }
small { opacity: 0.75; }
nav { display:flex; gap: 8px; flex-wrap: wrap; }
nav a { text-decoration:none; padding: 8px 10px; border-radius: 999px; border: 1px solid rgba(0,0,0,0.1); color: inherit; }
nav a.active { background: rgba(0,0,0,0.06); }
form { display: grid; gap: 10px; }
fieldset { border: 1px solid rgba(0,0,0,0.08); border-radius: 12px; padding: 12px; }
legend { padding: 0 8px; }
input[type="number"], input[type="text"] { padding: 10px; border-radius: 10px; border: 1px solid rgba(0,0,0,0.12); width: 100%; box-sizing: border-box; }
input[type="checkbox"] { transform: scale(1.1); }
.row { display:grid; grid-template-columns: 1fr 1fr; gap: 10px; }
@media (max-width: 560px) { .row { grid-template-columns: 1fr; } }
button { padding: 10px 12px; border-radius: 10px; border: 0; background: black; color: white; font-weight: 650; cursor: pointer; }
pre { white-space: pre-wrap; word-break: break-word; background: rgba(0,0,0,0.04); padding: 10px; border-radius: 12px; }
.notice { border-left: 4px solid rgba(0,0,0,0.35); padding: 10px 12px; background: rgba(0,0,0,0.03); border-radius: 10px; }
</style>
"""


def _tab_link(tab: str, active: str) -> str:
    cls = "active" if tab == active else ""
    label = {"info": "Info", "login": "Login", "network": "Network", "scraper": "Scraper"}.get(tab, tab)
    return f'<a class="{cls}" href="/ui?tab={tab}">{label}</a>'


def _ui_shell(active_tab: str, content: str) -> str:
    return f"""
<!doctype html>
<html lang="da">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Minvandforsyning</title>
{_UI_CSS}
</head>
<body>
<header>
  <h1>Minvandforsyning Companion</h1>
  <nav>
    {_tab_link("info", active_tab)}
    {_tab_link("login", active_tab)}
    {_tab_link("network", active_tab)}
    {_tab_link("scraper", active_tab)}
  </nav>
</header>
<main>
{content}
</main>
<script>
async function refresh() {{
  try {{
    const r = await fetch('/state_raw', {{ cache: 'no-store' }});
    const j = await r.json();
    const el = document.getElementById('live_json');
    if (el) el.textContent = JSON.stringify(j, null, 2);
    const v = document.getElementById('live_value');
    if (v) v.textContent = (j.reading_m3 ?? 'n/a');
    const t = document.getElementById('live_time');
    if (t) t.textContent = (j.read_at_iso ?? 'n/a');
    const m = document.getElementById('live_mode');
    if (m) m.textContent = (j.mode ?? 'n/a');
    const n = document.getElementById('live_next');
    if (n) n.textContent = (j.next_poll_in_seconds ?? 'n/a');
    const e = document.getElementById('live_error');
    if (e) e.textContent = (j.error ?? '');
  }} catch (e) {{}}
}}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


def _render_info() -> str:
    link = "https://www.minvandforsyning.dk/forbrug"
    return f"""
<div class="cards">
  <div class="card">
    <div class="label">Seneste aflæsning (m³)</div>
    <div class="value" id="live_value">n/a</div>
    <small>Vises stabilt via cache selv ved midlertidige scrape-fejl</small>
  </div>
  <div class="card">
    <div class="label">Aflæst tidspunkt</div>
    <div class="value" id="live_time">n/a</div>
    <small>Fra teksten over grafen på /forbrug</small>
  </div>
  <div class="card">
    <div class="label">Polling mode</div>
    <div class="value" id="live_mode">n/a</div>
    <small>Næste poll om: <b id="live_next">n/a</b> sek</small>
  </div>
</div>

<div style="height: 14px"></div>

<div class="notice">
  <b>Direkte link til servicen</b><br>
  <a href="{link}" target="_blank" rel="noreferrer">{link}</a>
</div>

<div style="height: 14px"></div>

<details>
  <summary>Live JSON (debug)</summary>
  <pre id="live_json">{{}}</pre>
</details>

<div style="height: 14px"></div>
<div class="notice" id="live_error"></div>
"""


def _render_login() -> str:
    return """
<h2>Login</h2>
<div class="notice">
  <b>Begrænsninger lige nu</b><br>
  Dette add-on er pt. kun testet med en allerede oprettet bruger på minvandforsyning.dk
  som har 1 og kun 1 tilknyttet måler.<br><br>
  Login-oplysninger sættes i add-on Configuration i Home Assistant (Supervisor).
</div>

<p>
Når login er sat korrekt, kan du gå til Info fanen og se om aflæsning opdateres.
</p>
"""


def _render_network() -> str:
    return """
<h2>Network</h2>
<div class="notice">
  Add-on lytter internt på port <b>8080</b>.
  Hvis du vil tilgå den direkte uden Ingress, kan du ændre port-mapping i add-on UI under Network (Supervisor).
</div>

<ul>
  <li>Ingress anbefales, så du slipper for port-eksponering.</li>
  <li>Direkte endpoints: <code>/state</code> og <code>/state_raw</code></li>
</ul>
"""


def _render_scraper(opts: dict[str, Any], overrides: dict[str, Any]) -> str:
    def num(name: str, default: Any) -> str:
        v = overrides.get(name, opts.get(name, default))
        return f'<input name="{name}" type="number" step="1" value="{v}"/>'

    def chk(name: str, default: bool) -> str:
        v = bool(overrides.get(name, opts.get(name, default)))
        checked = "checked" if v else ""
        return f'<label><input name="{name}" type="checkbox" {checked}/> {name}</label>'

    def flt(name: str, default: float) -> str:
        v = overrides.get(name, opts.get(name, default))
        return f'<input name="{name}" type="text" value="{v}"/>'

    return f"""
<h2>Scraper</h2>
<div class="notice">
  Her kan du justere polling-intervaller. Ændringerne gemmes lokalt i add-on'et og slår igennem uden restart.
</div>

<form method="post" action="/ui/save_scraper">
  <fieldset>
    <legend>Intervaller</legend>
    <div class="row">
      <div><div class="label">idle_poll_seconds</div>{num("idle_poll_seconds", 1800)}</div>
      <div><div class="label">probe_after_minutes</div>{num("probe_after_minutes", 45)}</div>
    </div>
    <div class="row">
      <div><div class="label">probe_poll_seconds</div>{num("probe_poll_seconds", 120)}</div>
      <div><div class="label">probe_max_minutes</div>{num("probe_max_minutes", 20)}</div>
    </div>
    <div class="row">
      <div><div class="label">min_poll_seconds</div>{num("min_poll_seconds", 30)}</div>
      <div><div class="label">jitter_seconds</div>{num("jitter_seconds", 15)}</div>
    </div>
  </fieldset>

  <fieldset>
    <legend>Stabilitet</legend>
    {chk("keep_last_on_error", True)}<br>
    {chk("allow_decrease", False)}<br><br>
    <div class="label">decrease_tolerance_m3</div>
    {flt("decrease_tolerance_m3", 0.0005)}
  </fieldset>

  <button type="submit">Gem</button>
</form>

<div style="height: 14px"></div>

<div class="notice">
  <b>Status</b><br>
  Sidste succes: <b id="live_time">n/a</b><br>
  Næste poll om: <b id="live_next">n/a</b> sek<br>
  Mode: <b id="live_mode">n/a</b>
</div>
"""


@APP.get("/ui", response_class=HTMLResponse)
def ui(request: Request):
    tab = (request.query_params.get("tab") or "info").strip().lower()
    opts = _read_json(OPTIONS_PATH)
    overrides = _read_json(UI_OVERRIDES_PATH)

    if tab == "login":
        content = _render_login()
    elif tab == "network":
        content = _render_network()
    elif tab == "scraper":
        content = _render_scraper(opts, overrides)
    else:
        tab = "info"
        content = _render_info()

    return HTMLResponse(_ui_shell(tab, content))


@APP.post("/ui/save_scraper")
async def ui_save_scraper(request: Request):
    form = await request.form()
    current = _read_json(UI_OVERRIDES_PATH)

    def _get_int(key: str, default: int) -> int:
        try:
            return int(str(form.get(key, default)))
        except Exception:
            return default

    def _get_float(key: str, default: float) -> float:
        try:
            return float(str(form.get(key, default)))
        except Exception:
            return default

    current["idle_poll_seconds"] = _get_int("idle_poll_seconds", 1800)
    current["probe_after_minutes"] = _get_int("probe_after_minutes", 45)
    current["probe_poll_seconds"] = _get_int("probe_poll_seconds", 120)
    current["probe_max_minutes"] = _get_int("probe_max_minutes", 20)
    current["min_poll_seconds"] = _get_int("min_poll_seconds", 30)
    current["jitter_seconds"] = _get_int("jitter_seconds", 15)

    current["keep_last_on_error"] = ("keep_last_on_error" in form)
    current["allow_decrease"] = ("allow_decrease" in form)
    current["decrease_tolerance_m3"] = _get_float("decrease_tolerance_m3", 0.0005)

    _write_json(UI_OVERRIDES_PATH, current)
    return RedirectResponse(url="/ui?tab=scraper", status_code=303)


# -----------------------------------------------------------------------------
# Boot
# -----------------------------------------------------------------------------

def main():
    opts = _read_json(OPTIONS_PATH)
    email = opts.get("email", "")
    password = opts.get("password", "")

    if not email or not password:
        # Don't crash hard: show UI and error until user configures
        STATE["ok"] = False
        STATE["error"] = "Udfyld email og password i add-on Configuration (Supervisor)"
    else:
        t = threading.Thread(target=poll_loop, args=(email, password), daemon=True)
        t.start()

    import uvicorn
    uvicorn.run(APP, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
