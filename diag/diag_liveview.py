"""Probe 2 for the dunlin (Mini 2K+) PTZ camera.

The legacy owl thumbnail command returns 409 "Mini is offline" (code 1929) for dunlin.
This probe: (1) dumps the PTZ entry from homescreen.ring_devices, (2) starts a v2 owl
live view exactly like the Blink app / reference lib, (3) closes it with commands/{id}/done,
(4) watches whether the cloud thumbnail updates afterwards.

Run:  sudo docker exec -i blink-app python - < /mnt/Random/blink/diag/diag_liveview.py
Uses the stored access token only; never refreshes/rotates tokens.
"""

import asyncio
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


async def main():
    creds = json.load(open(CREDS_FILE))
    base = f"https://rest-{creds['region_id']}.immedia-semi.com"
    acct = creds["account_id"]
    h = {**HEADERS, "Authorization": f"Bearer {creds['token']}"}

    async with ClientSession() as s:
        async def call(method, url, body=None):
            async with s.request(method, url, headers=h,
                                 data=None if body is None else json.dumps(body)) as r:
                txt = await r.text()
                return r.status, txt

        async def homescreen():
            st, txt = await call("GET", f"{base}/api/v4/accounts/{acct}/homescreen")
            if st != 200:
                raise SystemExit(f"homescreen HTTP {st}: {txt[:300]}")
            return json.loads(txt)

        hs = await homescreen()
        owl = next(o for o in hs["owls"] if o["name"] == CAMERA_NAME)
        net, oid = owl["network_id"], owl["id"]
        ring = [d for d in hs.get("ring_devices", []) if d.get("name") == CAMERA_NAME]
        print(f"[{ts()}] ring_devices entry for {CAMERA_NAME}:\n{json.dumps(ring, indent=1)}")
        print(f"[{ts()}] thumbnail before: ts={owl.get('thumbnail_timestamp')}")
        before = owl.get("thumbnail_timestamp")

        lv_url = f"{base}/api/v2/accounts/{acct}/networks/{net}/owls/{oid}/liveview"
        st, txt = await call("POST", lv_url, {"intent": "liveview", "motion_event_start_time": None})

        safe = txt
        try:
            j = json.loads(txt)
            if isinstance(j, dict) and j.get("server"):
                j["server"] = j["server"].split("?")[0][:60] + "...<redacted>"
                safe = json.dumps(j)
        except ValueError:
            pass
        print(f"\n[{ts()}] POST v2 liveview -> HTTP {st}\n  body: {safe[:1500]}")

        cmd_id = None
        try:
            cmd_id = json.loads(txt).get("command_id") or json.loads(txt).get("id")
        except (ValueError, AttributeError):
            pass

        if cmd_id:
            curl = f"{base}/accounts/{acct}/networks/{net}/commands/{cmd_id}"
            for i in range(10):
                await asyncio.sleep(1)
                st, txt = await call("GET", curl)
                print(f"[{ts()}] poll {i} HTTP {st}: {txt[:300]}")
            st, txt = await call("POST", f"{curl}/done")
            print(f"[{ts()}] POST done -> HTTP {st}: {txt[:300]}")

        for i in range(15):
            await asyncio.sleep(4)
            o = next(o for o in (await homescreen())["owls"] if o["id"] == oid)
            now = o.get("thumbnail_timestamp")
            print(f"[{ts()}] homescreen #{i}: thumbnail_timestamp={now}"
                  f"{'  <-- CHANGED' if now != before else ''}")
            if now != before:
                break


asyncio.run(main())
