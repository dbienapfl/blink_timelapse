"""One-shot diagnostic: trigger an owl (Mini) thumbnail exactly like Blink Android does
and print every response, so we can see why the thumbnail does not refresh.

Run inside the running container (uses its creds; never refreshes/rotates tokens):
    sudo docker exec -i blink-app python - < /mnt/Random/blink/diag/diag_thumbnail.py
"""

import asyncio
import hashlib
import json
import os
import time

from aiohttp import ClientSession

CREDS_FILE = "/app/creds/blink_creds.json"
CAMERA_NAME = os.getenv("CAMERA_NAME", "PTZ")

HEADERS = {
    "APP-BUILD": "ANDROID_29823413",
    "User-Agent": "Blink/59.2 (samsung SM-G998B; Android 14)",
    "LOCALE": "en_US",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "X-Blink-Time-Zone": os.getenv("TZ", "America/Denver"),
}


def ts():
    return time.strftime("%H:%M:%S")


def owl_summary(owl):
    return {k: owl.get(k) for k in (
        "id", "network_id", "name", "type", "status", "enabled", "updated_at",
        "thumbnail", "thumbnail_timestamp", "local_storage_enabled", "snooze",
        "snooze_time_remaining", "privacy_mode", "fw_version")}


async def main():
    creds = json.load(open(CREDS_FILE))
    base = f"https://rest-{creds['region_id']}.immedia-semi.com"
    acct = creds["account_id"]
    h = {**HEADERS, "Authorization": f"Bearer {creds['token']}"}
    if creds.get("expiration_date", 0) < time.time():
        print("!! stored token is expired; let the app refresh it first (or restart the container)")

    async with ClientSession() as s:
        async def homescreen():
            async with s.get(f"{base}/api/v4/accounts/{acct}/homescreen", headers=h) as r:
                if r.status != 200:
                    raise SystemExit(f"homescreen HTTP {r.status}: {(await r.text())[:500]}")
                return await r.json()

        hs = await homescreen()
        print(f"[{ts()}] homescreen device lists:",
              {k: [d.get("name") for d in v] for k, v in hs.items()
               if isinstance(v, list) and v and isinstance(v[0], dict) and "name" in v[0]})
        owl = next((o for o in hs.get("owls", []) if o.get("name") == CAMERA_NAME), None)
        if not owl:
            raise SystemExit(f"'{CAMERA_NAME}' not in owls")
        print(f"[{ts()}] FULL owl record:\n{json.dumps(owl, indent=1)}")
        before = owl_summary(owl)
        net, oid = owl["network_id"], owl["id"]

        url = f"{base}/api/v1/accounts/{acct}/networks/{net}/owls/{oid}/thumbnail"
        async with s.post(url, headers=h) as r:
            body = await r.text()
            print(f"\n[{ts()}] POST thumbnail -> HTTP {r.status}\n  body: {body[:1500]}")
        try:
            cmd = json.loads(body)
        except ValueError:
            cmd = {}
        cmd_id = cmd.get("id") or cmd.get("command_id")

        if cmd_id:
            curl = f"{base}/accounts/{acct}/networks/{net}/commands/{cmd_id}"
            for i in range(60):
                async with s.get(curl, headers=h) as r:
                    txt = await r.text()
                try:
                    st = json.loads(txt)
                except ValueError:
                    st = {}
                print(f"[{ts()}] poll {i:02d} HTTP {r.status}: complete={st.get('complete')} "
                      f"status={st.get('status')} status_code={st.get('status_code')} "
                      f"msg={st.get('status_msg')!r}")
                if i == 0 or st.get("complete"):
                    print(f"  raw: {txt[:1500]}")
                if st.get("complete") or r.status >= 400:
                    break
                await asyncio.sleep(1)
        else:
            print("!! no command id in POST response - nothing to poll")

        for i in range(10):
            await asyncio.sleep(3)
            owl = next(o for o in (await homescreen())["owls"] if o["id"] == oid)
            now = owl_summary(owl)
            changed = {k: (before[k], now[k]) for k in now if now[k] != before[k]}
            print(f"[{ts()}] homescreen #{i}: changed={changed or 'nothing'}")
            if "thumbnail" in changed or "thumbnail_timestamp" in changed:
                break

        thumb = owl.get("thumbnail") or ""
        cands = [thumb]
        if not thumb.split("?")[0].endswith(".jpg"):
            p, _, q = thumb.partition("?")
            cands.append(p + ".jpg" + ("?" + q if q else ""))
        for t in cands:
            u = t if t.startswith("http") else f"{base}{t}"
            async with s.get(u, headers=h) as r:
                data = await r.read()
            print(f"[{ts()}] GET {t} -> HTTP {r.status}, {len(data)} bytes, "
                  f"sha1={hashlib.sha1(data).hexdigest()[:12]}")


asyncio.run(main())
