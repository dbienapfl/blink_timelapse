"""Probe 4: Blink snapshot service for Ring-based (dunlin) cameras, as captured from the iOS app:
  GET https://app-snaps.blink.com/blink/snapshots/next/{ring_device_id}
      ?extras=force&after-ms=<epoch seconds>&max-wait-ms=1000
  204 = no new image yet (poll again); image = fresh snapshot.

Run:  sudo docker exec -i blink-app python - < /mnt/Random/blink/diag/diag_snaps.py
Never refreshes tokens. Saves the image to /data/blink/snaps_probe.jpg
"""

import asyncio
import json
import os
import time

from aiohttp import ClientSession

CREDS_FILE = "/app/creds/blink_creds.json"
CAMERA_NAME = os.getenv("CAMERA_NAME", "PTZ")


def ts():
    return time.strftime("%H:%M:%S")


async def main():
    creds = json.load(open(CREDS_FILE))
    base = f"https://rest-{creds['region_id']}.immedia-semi.com"
    acct = creds["account_id"]
    h = {
        "Authorization": f"Bearer {creds['token']}",
        "Accept": "*/*",
        "LOCALE": "en_US",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Blink-Time-Zone": os.getenv("TZ", "America/Denver"),
        "APP-BUILD": "ANDROID_29823413",
        "User-Agent": "Blink/59.2 (samsung SM-G998B; Android 14)",
    }

    async with ClientSession() as s:
        async with s.get(f"{base}/api/v4/accounts/{acct}/homescreen",
                         headers={**h, "Accept": "application/json"}) as r:
            hs = await r.json()
        owl = next(o for o in hs["owls"] if o["name"] == CAMERA_NAME)
        rid = owl.get("ring_device_id")
        print(f"[{ts()}] ring_device_id={rid} thumbnail_timestamp before={owl.get('thumbnail_timestamp')}")

        after = int(time.time()) - 2
        url = (f"https://app-snaps.blink.com/blink/snapshots/next/{rid}"
               f"?extras=force&after-ms={after}&max-wait-ms=1000")
        t0 = time.time()
        for i in range(90):
            async with s.get(url, headers=h) as r:
                data = await r.read()
                ctype = r.headers.get("Content-Type", "")
                status = r.status
            el = time.time() - t0
            if status == 200 and data and not ctype.startswith("application/json"):
                open("/data/blink/snaps_probe.jpg", "wb").write(data)
                print(f"[{ts()}] poll {i:02d} (+{el:.1f}s) HTTP 200 [{ctype}] {len(data)} bytes"
                      " -> /data/blink/snaps_probe.jpg  <-- !!!")
                print("  interesting headers:",
                      {k: v for k, v in r.headers.items()
                       if k.lower().startswith(("x-", "content-", "last-", "etag"))
                       and not k.lower().startswith("x-upstream")})
                break
            print(f"[{ts()}] poll {i:02d} (+{el:.1f}s) HTTP {status} [{ctype}] "
                  f"{data[:200].decode('utf-8', 'replace')!r}")
            if status not in (204, 200):
                break
            if i % 10 == 9:
                async with s.get(f"{base}/api/v4/accounts/{acct}/homescreen",
                                 headers={**h, "Accept": "application/json"}) as hr:
                    o = next(o for o in (await hr.json())["owls"] if o["name"] == CAMERA_NAME)
                print(f"[{ts()}]   homescreen thumbnail_timestamp={o.get('thumbnail_timestamp')}")
            await asyncio.sleep(1)

        async with s.get(f"https://app-snaps.blink.com/blink/snapshots/image/{rid}", headers=h) as r:
            data = await r.read()
            print(f"[{ts()}] GET snapshots/image -> HTTP {r.status} [{r.headers.get('Content-Type', '')}] "
                  f"{len(data)} bytes")
            if r.status == 200 and data and r.headers.get("Content-Type", "").startswith("image"):
                open("/data/blink/snaps_image_probe.jpg", "wb").write(data)
                print("  saved /data/blink/snaps_image_probe.jpg")

        await asyncio.sleep(3)
        async with s.get(f"{base}/api/v4/accounts/{acct}/homescreen",
                         headers={**h, "Accept": "application/json"}) as r:
            owl = next(o for o in (await r.json())["owls"] if o["name"] == CAMERA_NAME)
        print(f"[{ts()}] thumbnail_timestamp after={owl.get('thumbnail_timestamp')}")


asyncio.run(main())
