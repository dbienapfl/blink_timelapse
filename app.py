import os
import time
import hashlib
import asyncio
import logging
import threading
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from aiohttp import ClientSession
from blinkpy import api
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from blinkpy.helpers.util import json_load
from flask import Flask, send_from_directory, render_template_string, redirect, url_for

CAMERA_NAME = os.getenv("CAMERA_NAME", "Timelapse")
# Blink sync module / network the camera belongs to
SYNC_NAME = os.getenv("SYNC_NAME", "FAFO")
CAPTURE_INTERVAL = int(os.getenv("CAPTURE_INTERVAL", "90"))
# Timelapse output size; frames are scaled/padded to this
TIMELAPSE_SIZE = os.getenv("TIMELAPSE_SIZE", "1280x720")
BASE_DIR = Path("/data/blink")
CREDS_FILE = Path("/app/creds/blink_creds.json")

FRAMES_DIR = BASE_DIR / "frames"
VIDEOS_DIR = BASE_DIR / "videos"
LATEST_JPG = BASE_DIR / "latest.jpg"

# Show blinkpy's own warnings/errors (it swallows HTTP failures and only logs them)
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
# Hide per-request access logs (the dashboard polls latest.jpg every 30s)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

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


async def capture_once(blink, camera, state):
  """Ask the camera for a new snapshot, then pull the fresh thumbnail via blinkpy."""
  before_thumb = camera.thumbnail
  resp = await camera.snap_picture()
  if not resp:
    print(f"[Blink] Snapshot request for '{CAMERA_NAME}' got no response")
    return
  if not await api.wait_for_command(blink, resp):
    print(f"[Blink] Snapshot command did not complete: {resp}")
  # Refresh only this camera's network; blinkpy downloads the thumbnail when its URL changes.
  await blink.sync[SYNC_NAME].refresh()

  if camera.thumbnail == before_thumb:
    print("[Blink] Thumbnail unchanged this cycle")
  img = camera.image_from_cache
  if not img:
    print(f"[Blink] No image cached yet for '{CAMERA_NAME}'")
    return

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
    print(f"[Blink] Waiting for {CREDS_FILE}; run auth.py first.")
    await asyncio.sleep(15)

  async with ClientSession() as session:
    blink = Blink(session=session)
    blink.auth = Auth(await json_load(str(CREDS_FILE)), no_prompt=True, session=session)
    await blink.start()
    await blink.save(str(CREDS_FILE))

    if SYNC_NAME not in blink.sync:
      print(f"[Blink] ERROR: network '{SYNC_NAME}' not found; have {list(blink.sync)}. Capture stopped.")
      return
    cameras = blink.sync[SYNC_NAME].cameras
    if CAMERA_NAME not in cameras:
      print(f"[Blink] ERROR: camera '{CAMERA_NAME}' not in '{SYNC_NAME}'; have {list(cameras)}. Capture stopped.")
      return
    camera = cameras[CAMERA_NAME]
    print(f"[Blink] Using '{CAMERA_NAME}' on '{SYNC_NAME}' (type={camera.camera_type or 'default'})")

    state = {"last_hash": None}
    saved_token = blink.auth.token
    while True:
      start_ts = time.time()
      try:
        await capture_once(blink, camera, state)
      except Exception as e:
        print(f"[Blink] Capture error: {type(e).__name__}: {e}")
      # blinkpy refreshes the OAuth token on its own; persist it whenever it rotates.
      if blink.auth.token != saved_token:
        await blink.save(str(CREDS_FILE))
        saved_token = blink.auth.token
        print("[Blink] Token refreshed and saved")
      await asyncio.sleep(max(15, CAPTURE_INTERVAL - (time.time() - start_ts)))


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
