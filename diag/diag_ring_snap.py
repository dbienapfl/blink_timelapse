"""Probe 3: does Ring's snapshot API accept the Blink token for the dunlin PTZ?

Endpoint shapes are the ones used by Ring-ecosystem client libraries (unverified for Blink).
Run:  sudo docker exec -i blink-app python - < /mnt/Random/blink/diag/diag_ring_snap.py
Never refreshes tokens. Saves any returned JPEG to /data/blink/ring_probe_<n>.jpg
"""

import asyncio
import json
import os
import time

from aiohttp import ClientSession

CREDS_FILE = "/app/creds/blink_creds.json"
CAMERA_NAME = os.getenv("CAMERA_NAME", "PTZ")
SNAPS = "https://app-snaps.ring.com/snapshots"


def ts():
    return time.strftime("%H:%M:%S")


async def main():
    creds = json.load(open(CREDS_FILE))
    base = f"https://rest-{creds['region_id']}.immedia-semi.com"
    acct = creds["account_id"]
    blink_h = {"APP-BUILD": "ANDROID_29823413", "User-Agent": "Blink/59.2 (samsung SM-G998B; Android 14)",
               "LOCALE": "en_US", "Accept": "application/json",
               "Authorization": f"Bearer {creds['token']}"}

    async with ClientSession() as s:
        async with s.get(f"{base}/api/v4/accounts/{acct}/homescreen", headers=blink_h) as r:
            hs = await r.json()
        rd = next(d for d in hs["ring_devices"] if d["name"] == CAMERA_NAME)
        rid = rd["id"]
        print(f"[{ts()}] Ring device id {rid}")

        variants = {
            "blink-style": blink_h,
            "ring-style": {"Authorization": f"Bearer {creds['token']}",
                           "User-Agent": "android:com.ringapp",
                           "hardware_id": str(creds.get("hardware_id", "")),
                           "Accept": "application/json"},
        }
        probes = [
            ("GET", "https://api.ring.com/clients_api/ring_devices", None),
            ("POST", f"{SNAPS}/timestamps", {"doorbot_ids": [rid]}),
            ("PUT", f"{SNAPS}/update-all", {"doorbot_ids": [rid], "refresh": True}),
            ("GET", f"{SNAPS}/next/{rid}?extras=force", None),
            ("GET", f"{SNAPS}/image/{rid}", None),
        ]

        n = 0
        for vname, h in variants.items():
            print(f"\n===== headers: {vname}")
            for method, url, body in probes:
                hh = dict(h)
                if "/image/" in url or "/next/" in url:
                    hh["Accept"] = "image/jpeg"
                try:
                    async with s.request(method, url, headers=hh, json=body,
                                         allow_redirects=False, timeout=30) as r:
                        data = await r.read()
                        ctype = r.headers.get("Content-Type", "")
                except Exception as e:
                    print(f"[{ts()}] {method} {url} -> {type(e).__name__}: {e}")
                    continue
                if ctype.startswith("image/") and data:
                    n += 1
                    out = f"/data/blink/ring_probe_{n}.jpg"
                    open(out, "wb").write(data)
                    print(f"[{ts()}] {method} {url} -> HTTP {r.status} IMAGE {len(data)} bytes -> {out}  <-- !!!")
                else:
                    print(f"[{ts()}] {method} {url} -> HTTP {r.status} [{ctype}] "
                          f"{data[:300].decode('utf-8', 'replace')!r}")


asyncio.run(main())
