import os
import json
import time
import hashlib
import asyncio
import threading
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from aiohttp import ClientSession, ClientTimeout
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from blinkpy.helpers.util import json_load
from flask import Flask, send_from_directory, render_template_string, redirect, url_for

CAMERA_NAME = os.getenv("CAMERA_NAME", "PTZ")
CAMERA_SERIAL = os.getenv("CAMERA_SERIAL", "GVP69W036393HEM5")
CAPTURE_INTERVAL = int(os.getenv("CAPTURE_INTERVAL", "90"))
# How long to wait for the homescreen thumbnail to move after a snapshot request
SNAP_TIMEOUT = int(os.getenv("SNAP_TIMEOUT", "75"))
# Upper bound for the 429 back-off
MAX_BACKOFF = int(os.getenv("MAX_BACKOFF", "600"))
# Timelapse output size; frames are scaled/padded to this
TIMELAPSE_SIZE = os.getenv("TIMELAPSE_SIZE", "1280x720")
BASE_DIR = Path("/data/blink")
CREDS_FILE = Path("/app/creds/blink_creds.json")

FRAMES_DIR = BASE_DIR / "frames"
VIDEOS_DIR = BASE_DIR / "videos"
LATEST_JPG = BASE_DIR / "latest.jpg"

# Ring-based Blink cameras (PTZ, newer Minis) take snapshots through the
# app-snaps service, the same path the Blink app and the
# homebridge-blink-cameras-new-api plugin use. blinkpy's snap_picture() hits the
# legacy owl thumbnail endpoint, which these devices ignore.
SNAPS_BASE = "https://app-snaps.blink.com/blink/snapshots"

API_HEADERS = {
    "APP-BUILD": "ANDROID_29823413",
    "User-Agent": "Blink/59.2 (samsung SM-G998B; Android 14)",
    "LOCALE": "en_US",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Blink-Time-Zone": os.getenv("TZ", "America/Denver"),
}

app = Flask(__name__)

HTML = (
    "<!DOCTYPE html><html><head><title>Blink {{ cam }} Dashboard</title>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<style>"
    "body { font-family: system-ui, sans-serif; background: #111; color: #eee; max-width: 1000px; margin: 20px auto; padding: 0 15px; }"
    ".header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }"
    "img, video { width: 100%; border-radius: 8px; background: #000; border: 1px solid #333; }"
    ".grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; margin-top: 15px; }"
    ".card { background: #1c1c1c; padding: 12px; border-radius: 8px; }"
    ".card-title { margin-bottom: 8px; font-weight: 600; color: #ddd; }"
    ".btn { background: #2b6cb0; color: white; border: none; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-size: 0.9em; }"
    ".btn:hover { background: #2c5282; }"
    ".meta { color: #888; font-size: 0.85em; margin-top: 6px; }"
    "</style></head><body>"
    "<div class='header'><h2>{{ cam }} - Live Snapshot Feed</h2>"
    "<form action='/stitch-today' method='post'><button class='btn' type='submit'>Compile Today Timelapse</button></form></div>"
    "<img id='live' src='/latest.jpg' alt='Waiting for first snapshot...'>"
    "<div class='meta' id='status'>Auto-refreshing every 30s (captures every {{ interval }}s)</div>"
    "<h2 style='margin-top: 30px;'>Compiled Daily Timelapses</h2>"
    "{% if videos %}<div class='grid'>{% for vid in videos %}"
    "<div class='card'><div class='card-title'>{{ vid }}</div>"
    "<video controls preload='metadata' src='/videos/{{ vid }}'></video></div>"
    "{% endfor %}</div>{% else %}<p class='meta'>No compiled timelapses yet.</p>{% endif %}"
    "<script>setInterval(() => {"
    "document.getElementById('live').src = '/latest.jpg?t=' + new Date().getTime();"
    "document.getElementById('status').innerText = 'Last refreshed: ' + new Date().toLocaleTimeString();"
    "}, 30000);</script></body></html>"
)


def stitch_date(date_str: str):
  day_dir = FRAMES_DIR / date_str
  if not day_dir.exists() or not any(day_dir.glob("*.jpg")):
    print(f"[FFmpeg] No frames found for {date_str}, skipping.")
    return
  VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
  out_mp4 = VIDEOS_DIR / f"{date_str}.mp4"
  tmp_mp4 = VIDEOS_DIR / f".{date_str}.tmp.mp4"
  w, h = TIMELAPSE_SIZE.lower().split("x")
  vf = (
      f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
      f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
  )
  cmd = [
      "ffmpeg", "-y", "-framerate", "30", "-pattern_type", "glob",
      "-i", f"{day_dir}/*.jpg", "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
      "-movflags", "+faststart", str(tmp_mp4),
  ]
  print(f"[FFmpeg] Stitching {date_str} -> {out_mp4}...")
  res = subprocess.run(cmd, capture_output=True, text=True)
  if res.returncode == 0 and tmp_mp4.exists():
    tmp_mp4.replace(out_mp4)
    print(f"[FFmpeg] Finished {out_mp4}")
  else:
    print(f"[FFmpeg] Error: {res.stderr[-2000:]}")


def nightly_stitch_scheduler():
  last_stitched_date = None
  while True:
    now = datetime.now()
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if now.hour == 0 and now.minute >= 5 and last_stitched_date != yesterday_str:
      stitch_date(yesterday_str)
      last_stitched_date = yesterday_str
    time.sleep(30)


class RateLimited(Exception):
  """Blink answered HTTP 429; the capture loop backs off."""

  def __init__(self, url, retry_after=None):
    super().__init__(f"HTTP 429 from {url.split('?')[0]}")
    self.retry_after = retry_after


async def blink_request(session, blink, method, url, accept="application/json"):
  """Authenticated request; on 401 refresh the OAuth token once and retry,
    on 429 raise RateLimited. Returns (status, content_type, body_bytes)."""
  for attempt in (1, 2):
    headers = {
        **API_HEADERS,
        "Accept": accept,
        "Authorization": f"Bearer {blink.auth.token}",
    }
    async with session.request(method, url, headers=headers) as r:
      body = await r.read()
      status, ctype = r.status, r.headers.get("Content-Type", "")
      retry_after = r.headers.get("Retry-After")
    if status == 429:
      try:
        retry_after = int(retry_after) if retry_after else None
      except ValueError:
        retry_after = None
      raise RateLimited(url, retry_after)
    if status == 401 and attempt == 1:
      print("[Blink] HTTP 401 - refreshing token")
      # refresh=True uses the stored refresh_token. Without it, blinkpy 0.25.x
      # performs a full username/password login, which can trigger 2FA.
      await blink.auth.refresh_tokens(refresh=True)
      await blink.save(str(CREDS_FILE))
      continue
    return status, ctype, body


async def fetch_owl(session, blink):
  url = f"{blink.urls.base_url}/api/v4/accounts/{blink.account_id}/homescreen"
  status, _, body = await blink_request(session, blink, "GET", url)
  if status != 200:
    raise RuntimeError(f"homescreen HTTP {status}: {body[:200]!r}")
  for owl in json.loads(body).get("owls", []):
    if owl.get("name") == CAMERA_NAME or owl.get("serial") == CAMERA_SERIAL:
      return owl
  return None


def is_image(status, ctype, body):
  return status == 200 and bool(body) and not ctype.startswith(("application/json", "text/"))


async def force_snapshot(session, blink, owl):
  """Trigger a fresh capture exactly like the Blink app: ONE snapshots/next request with
    after-ms = the current thumbnail_timestamp, then wait for the homescreen thumbnail to move.
    Returns JPEG bytes only if the service hands the image back directly, otherwise None
    (the caller then downloads the homescreen thumbnail if its timestamp moved)."""
  rid = owl["ring_device_id"]
  before_ts = owl.get("thumbnail_timestamp")
  after = before_ts or int(time.time()) - 2
  url = f"{SNAPS_BASE}/next/{rid}?extras=force&after-ms={after}&max-wait-ms=1000"
  started = time.time()
  status, ctype, body = await blink_request(session, blink, "GET", url, accept="*/*")
  if is_image(status, ctype, body):
    print(f"[Snap] Image returned directly ({len(body)} bytes)")
    return body
  if status != 204:
    print(f"[Snap] Unexpected HTTP {status}: {body[:200]!r}")
    return None

  await asyncio.sleep(20)
  while time.time() - started < SNAP_TIMEOUT:
    cur = await fetch_owl(session, blink)
    if cur and cur.get("thumbnail_timestamp") != before_ts:
      print(f"[Snap] Thumbnail updated after {time.time() - started:.0f}s")
      return None
    await asyncio.sleep(5)
  print(f"[Snap] No new thumbnail within {SNAP_TIMEOUT}s")
  return None


async def legacy_owl_thumbnail(session, blink, owl):
  """Older (non-Ring) Minis: classic owl thumbnail command."""
  url = (
      f"{blink.urls.base_url}/api/v1/accounts/{blink.account_id}"
      f"/networks/{owl['network_id']}/owls/{owl['id']}/thumbnail"
  )
  status, _, body = await blink_request(session, blink, "POST", url)
  print(f"[Blink] Legacy thumbnail POST -> HTTP {status} {body[:200]!r}")
  if status == 200:
    await asyncio.sleep(10)


async def capture_once(session, blink, state):
  owl = await fetch_owl(session, blink)
  if not owl:
    print(f"[Blink] Camera '{CAMERA_NAME}' not found in v4 owls.")
    return
  before_ts = owl.get("thumbnail_timestamp")

  img = None
  if owl.get("ring_device_id"):
    img = await force_snapshot(session, blink, owl)
  else:
    await legacy_owl_thumbnail(session, blink, owl)

  if img is None:
    owl = await fetch_owl(session, blink) or owl
    if owl.get("thumbnail_timestamp") == before_ts and state["last_hash"] is not None:
      print("[Blink] Thumbnail unchanged this cycle")
      return
    thumb = owl.get("thumbnail")
    if not thumb:
      print(f"[Blink] No thumbnail URL yet for {CAMERA_NAME}")
      return
    turl = thumb if thumb.startswith("http") else f"{blink.urls.base_url}{thumb}"
    status, ctype, body = await blink_request(session, blink, "GET", turl, accept="image/*")
    if not is_image(status, ctype, body):
      print(f"[Blink] Failed to download thumbnail: HTTP {status}")
      return
    img = body

  tmp_latest = BASE_DIR / ".latest.tmp.jpg"
  tmp_latest.write_bytes(img)
  tmp_latest.replace(LATEST_JPG)

  digest = hashlib.sha1(img).hexdigest()
  if digest == state["last_hash"]:
    print(f"[Blink] Image identical to last frame ({len(img)} bytes); not saved")
    return
  now = datetime.now()
  day_dir = FRAMES_DIR / now.strftime("%Y-%m-%d")
  day_dir.mkdir(parents=True, exist_ok=True)
  frame_path = day_dir / f"{now.strftime('%H-%M-%S')}.jpg"
  frame_path.write_bytes(img)
  state["last_hash"] = digest
  print(f"[Blink] Saved new frame {frame_path} ({len(img)} bytes)")


async def capture_loop():
  while not CREDS_FILE.exists():
    print(f"[Blink] Waiting for {CREDS_FILE}.")
    await asyncio.sleep(15)

  async with ClientSession(timeout=ClientTimeout(total=30)) as session:
    blink = Blink(session=session)
    creds = await json_load(str(CREDS_FILE))
    blink.auth = Auth(creds, no_prompt=True)
    await blink.start()
    state = {"last_hash": None}
    backoff = 0

    while True:
      start_ts = time.time()
      try:
        await capture_once(session, blink, state)
        if backoff:
          print("[Blink] Requests succeeding again; back to normal interval")
        backoff = 0
      except RateLimited as e:
        backoff = min(max(backoff * 2, CAPTURE_INTERVAL * 2), MAX_BACKOFF)
        if e.retry_after:
          backoff = min(max(backoff, e.retry_after), MAX_BACKOFF)
        print(f"[Blink] Rate limited ({e}); backing off {backoff}s")
      except Exception as e:
        print(f"[Blink] Capture error: {type(e).__name__}: {e}")

      elapsed = time.time() - start_ts
      sleep_for = backoff if backoff else max(15, CAPTURE_INTERVAL - elapsed)
      await asyncio.sleep(sleep_for)


@app.route("/")
def index():
  videos = (
      sorted([f.name for f in VIDEOS_DIR.glob("*.mp4")], reverse=True)
      if VIDEOS_DIR.exists()
      else []
  )
  return render_template_string(HTML, cam=CAMERA_NAME, interval=CAPTURE_INTERVAL, videos=videos)


@app.route("/latest.jpg")
def latest():
  if not LATEST_JPG.exists():
    return "No image captured yet", 404
  return send_from_directory(BASE_DIR, "latest.jpg")


@app.route("/videos/<filename>")
def serve_video(filename):
  return send_from_directory(VIDEOS_DIR, filename)


@app.route("/stitch-today", methods=["POST"])
def stitch_today():
  today_str = datetime.now().strftime("%Y-%m-%d")
  threading.Thread(target=stitch_date, args=(today_str,), daemon=True).start()
  return redirect(url_for("index"))


if __name__ == "__main__":
  BASE_DIR.mkdir(parents=True, exist_ok=True)
  threading.Thread(target=nightly_stitch_scheduler, daemon=True).start()
  threading.Thread(target=lambda: asyncio.run(capture_loop()), daemon=True).start()
  app.run(host="0.0.0.0", port=8080)
