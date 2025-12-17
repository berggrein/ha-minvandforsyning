# ha-minvandforsyning

Home Assistant **companion add-on** that logs into **minvandforsyning.dk** (via Rambøll IdP) using a headless browser (Playwright),
scrapes the latest meter reading (`m³`) from the **/forbrug** page, and exposes it as JSON over HTTP for Home Assistant to consume.

> Status: **Companion add-on only (v0.1.0)**. A matching HA custom integration can be added later, but is not required to get a working sensor.

---

## What you get

The add-on runs a small web server and exposes:

- `GET /state`  
  Returns the latest successful scrape result. If the scrape has not succeeded yet, it returns **503**.

- `GET /state_raw`  
  Always returns the current internal state (including error messages). Useful for debugging.

Example successful payload:

```json
{
  "ok": true,
  "error": null,
  "reading_m3": 442.627,
  "read_at_iso": "2025-12-17T20:54:00",
  "scraped_at_utc": "2025-12-17T20:58:12Z",
  "raw": "Senest registreret data kl. 20.54, d. 17.12.2025, aflæst til: 442,627 m³"
}
```

---

## Install (Home Assistant OS / Supervised)

### Option A: Local add-on repository (simple)
1. Copy this repository into your HA host add-ons folder:
   - `\<HA>ddons\ha-minvandforsyning\` (Samba/Share), or
   - `/addons/ha-minvandforsyning/` (SSH)

2. In Home Assistant:
   - **Settings → Add-ons → Add-on Store**
   - Menu (⋮) → **Check for updates**
   - You should see **"Minvandforsyning Companion"**

3. Open the add-on → **Configuration**:
   - `email`: your login email
   - `password`: your login password
   - `poll_seconds`: scrape interval (default 1800 = 30 min)

4. Start the add-on.

5. Test:
   - `http://<HA-IP>:8080/state_raw`
   - `http://<HA-IP>:8080/state`

---

## Home Assistant sensor (example)

Once `/state` returns a successful payload, you can create a sensor in HA that reads it.

### REST sensor (example)
Add to `configuration.yaml` (or a package):

```yaml
rest:
  - resource: "http://<HA-IP>:8080/state"
    scan_interval: 1800
    sensor:
      - name: "Vandmåler total"
        value_template: "{{ value_json.reading_m3 }}"
        unit_of_measurement: "m³"
        device_class: water
        state_class: total_increasing
        json_attributes:
          - read_at_iso
          - scraped_at_utc
          - raw
```

Then restart HA. You can now select **"Vandmåler total"** as a *Water* source in Energy.

---

## Notes / Troubleshooting

- This site uses **Blazor Server / SignalR**, so the meter reading is **not** present in the initial HTML response.
  That's why this add-on uses a real browser (Playwright) to wait for the DOM to render.
- If `/state` returns 503:
  - Check `GET /state_raw` to see the latest error.
  - Common issues: wrong credentials, temporary IdP problems, layout changes.

---

## Security

Credentials are stored in the add-on configuration (Supervisor).
If you publish this repo, **do not** commit your credentials.

---

## License

MIT (see `LICENSE`).
