# PoP Monitor

Automated proof of play (PoP) and fault detection for digital billboards, using Axis cameras.
Each camera watches one billboard. The software spots every creative change, captures a
high-quality snapshot of each play, sends it to the LUMO backend, and alerts ops when a
screen goes black, freezes, shows "no signal" or drops a slot.

Spec: *PoP Monitor — Axis ACAP spec* (LUMO doc). This repository is the v1 build of it.

## How it fits together

```
Axis camera ──RTSP 720p──▶ pop-gateway (one worker per camera)
            ◀──VAPIX JPEG──   │  ROI warp → features → change detector → PoP record
                              │                        └→ fault monitor → alert
                              ▼
                         on-disk outbox ──HTTPS──▶ LUMO backend (/v1/pop, /v1/alerts, /v1/heartbeat)
                              └──────────────────▶ Teams / Slack webhooks (alerts only)
```

* **v1 runs everything through the gateway**, so every camera model works (M5525-E,
  P3225-LVE Mk II, P3227-LVE, P3267-LV, M5526-E) whatever its AXIS OS version.
* The detection logic is one dependency-free C library (`core/`). The same code can later
  run on-camera as an ACAP on the ARM models (`acap/`, phase 2).

## Repository layout

| Path | What it is |
| --- | --- |
| `core/` | `popcore` detection library (C99, no dependencies) and its unit tests |
| `gateway/` | `pop-gateway` service: RTSP input, VAPIX snapshots, outbox, uploader, alerts |
| `tools/popreplay.c` | Runs the core over a recorded video: the field-trial tuning tool |
| `tools/roi-picker.html` | Open in a browser, load a camera snapshot, drag the four corners |
| `tools/receiver.py` | Interim backend + review page (checks token, HMAC, hashes) |
| `deploy/` | Portainer / Docker Compose stack, trial config, image build script |
| `tools/fake_camera.py`, `sim_scene.py`, `make_test_video.py` | Synthetic billboard for end-to-end tests |
| `acap/` | On-camera ACAP scaffold (phase 2): builds with ACAP Native SDK 12.5, not yet run on a camera |

## Build

Ubuntu 24.04:

```sh
sudo apt-get install build-essential cmake pkg-config libavformat-dev libavcodec-dev \
  libavutil-dev libswscale-dev libcurl4-openssl-dev libturbojpeg0-dev libcjson-dev libssl-dev
cmake -B build && cmake --build build -j
ctest --test-dir build --output-on-failure      # popcore unit tests (~30 s)
```

Or as a container: `docker build -f gateway/Dockerfile -t pop-gateway .`

## Run with Docker / Portainer (recommended for the trial)

`deploy/docker-compose.yml` is a two-container stack:

* **gateway**: the PoP monitor itself.
* **receiver**: an interim backend that stores PoP images and alerts and shows them on a
  review page at `http://DOCKER-HOST:8080`. Use it until the real backend exists, then point
  `server_url` at the backend and drop it.

1. On the Docker host (any Linux x86-64 host that can reach the cameras on ports 80 and 554):
   ```sh
   git clone <this repo> /opt/pop-monitor-src && sh /opt/pop-monitor-src/deploy/build-images.sh
   mkdir -p /opt/pop-monitor && cp /opt/pop-monitor-src/deploy/config.trial.json /opt/pop-monitor/config.json
   ```
   Edit `/opt/pop-monitor/config.json`: camera IPs, ROI, rotation profile.
2. In Portainer: **Stacks > Add stack > Web editor**, paste `deploy/docker-compose.yml`, and
   add the variables from `deploy/stack.env.example`. Deploy.
3. Open `http://DOCKER-HOST:8080` to see PoP images, alerts and camera status.
4. Change the config: edit the file on the host, then restart the gateway container in Portainer.

Images are built on the host rather than by Portainer because Portainer's "Pull and
redeploy" doesn't reliably rebuild `build:` images. To build in CI instead,
`.github/workflows/images.yml` pushes both images to GitHub Container Registry; change the
image names in the stack to match.

Record trial footage and replay it from inside the gateway container (ffmpeg and popreplay
are included):

```sh
docker exec -it <gateway-container> sh -c 'ffmpeg -rtsp_transport tcp -i "rtsp://USER:PASS@CAMERA/axis-media/media.amp?resolution=1280x720&fps=10" -t 600 -c copy /var/lib/pop-gateway/board.mp4'
docker exec -it <gateway-container> popreplay /var/lib/pop-gateway/board.mp4 --roi ... --slots 6 --slot-ms 10000
```

## Run the gateway directly

1. Create a camera user (viewer rights are enough) on each camera.
2. Set each preset so the board fills a good part of the frame, with some surroundings for
   context. Lock focus, use fixed exposure for the board, force day mode, turn WDR off, and
   keep the shutter at 1/100 s or slower to avoid LED banding.
3. Set each camera's region on the review page (**Edit region**, see below), or offline with
   `tools/roi-picker.html` and a saved snapshot.
4. Copy `gateway/config.example.json` to `/etc/pop-gateway/config.json`. Secrets can be
   written as `${ENV_VAR}` anywhere in a string value.
5. Check it: `pop-gateway -c /etc/pop-gateway/config.json --check`
6. Run it with systemd (`gateway/pop-gateway.service`) or Docker. Set `TZ=Pacific/Auckland`
   so operating hours and dayparts use local time, and make sure the host runs NTP.

### Key settings

| Setting | Default | Notes |
| --- | --- | --- |
| `profile.slot_count`, `profile.slot_ms` | unset | Rotation profile. Drives all triggers (below). `takeover: true` = one creative |
| `dayparts` | none | Profiles by time of day, e.g. 6 x 10 s by day, 4 x 15 s overnight |
| `operating_hours` | `00:00-24:00` | Black screens outside these hours don't alert |
| `analysis_fps` | 5 | Detection frame rate (stream is requested at 10 fps) |
| `snapshot.delay_ms` | 1000 | Wait after the creative settles before capturing |
| `snapshot.burst` / `interval_ms` | 3 / 300 | Frames per play; the best one is kept |
| `snapshot.compression` | 15 | VAPIX JPEG compression (lower = higher quality) |
| `snapshot.image_policy` | `every_play` | or `first_per_hour` (metadata every play, images hourly per creative) |
| `ptz_check` | true | Alerts and pauses detection if a PTZ camera leaves its preset |
| `host` | (required) | Camera IP or DNS name (no port) |
| `http_port` / `rtsp_port` | 80 / 554 | Non-standard or port-forwarded ports for remote cameras |
| `use_https` / `camera_tls_insecure` | false / false | VAPIX over HTTPS; accept the camera's self-signed certificate |
| `rtsp_transport` | `tcp` | `http` tunnels the video over the web port, when only one port is forwarded |
| `rtsp_url` / `vapix_url` | built | Full overrides, if a camera needs something unusual |

Triggers derived from the rotation profile (N slots of S ms):

| Trigger | Rule | 6 x 10 s |
| --- | --- | --- |
| Ignore changes faster than | 0.5 x S | 5 s |
| Expected change window | boundary ± 15% of S | ± 1.5 s |
| Frozen alert | no change for 2 x S | 20 s |
| Blank slot | blank for 0.5–1.5 x S | 5–15 s |
| Loop changed | plays per 2 loops not 2N ± 1 | 11–13 expected |
| Missing creative | fewer than N distinct creatives in 2 loops | under 6 |
| Coverage (heartbeat) | plays per hour vs 3600 / S | out of 360 |

## Managing sites from the review page

The **Sites** page lists every camera with its status, rotation and plays per hour.
**Add site** opens a form for the site ID, display name, model, host, web and RTSP ports,
video transport (RTSP, or tunnelled over the web port), HTTPS, camera login, rotation (slot
count and length, or takeover), operating hours and the PTZ check. Saving starts the camera
straight away and takes you to the region editor. **Settings** edits an existing site; saving
restarts only that camera. **Test camera** fetches a snapshot with the saved settings.

The first change made on the Sites or region pages creates `cameras.json` next to the queue
(in the Docker volume). From then on that file is the camera list and the `cameras` array in
`config.json` is only used if `cameras.json` is deleted. `defaults` (such as the camera
user and password) still come from `config.json` and `.env`.

* Back it up: `docker compose cp gateway:/var/lib/pop-gateway/cameras.json .`
* A password typed on the Settings page is saved in `cameras.json`. Where cameras share a
  login, leave it blank so the default from `.env` is used instead.
* Settings the form doesn't show (detector tuning, time-of-day profiles, snapshot options)
  are kept when a site is edited, and can be set in `config.json` under `defaults`.

## Editing a camera's region live

On the review page, click **Edit region** next to a camera. The editor fetches a fresh frame
from the camera; drag the four corners onto the lit screen (a magnifier appears while dragging,
arrow keys nudge the last corner), set the inset, check the straightened preview, and click
**Save and apply**. The gateway re-warps from the next frame and restarts detection for that
camera so the edit itself isn't logged as a play.

Region edits are saved by the gateway in `cameras.json` (see above), so they survive restarts.

Behind the page: the gateway serves a small live-settings API on port 8090 (`control_port`,
0 disables it), reachable only inside the Docker network and protected by the API key; the
receiver forwards `/api/*` to it. Set `REVIEW_USER` and `REVIEW_PASSWORD` to require a login
for the review page.

## Backend API (what the LUMO backend must implement)

All requests carry `Authorization: Bearer <api_key>`.

**`POST /v1/pop`** and **`POST /v1/alerts`**: `multipart/form-data` with parts
`metadata` (JSON), `frame` (full-resolution JPEG, PoP only) and `roi` (straightened board
JPEG). Headers: `Idempotency-Key: <event_id>` and, when `hmac_key` is set,
`X-PoP-Signature: sha256=<hex HMAC-SHA256 of the metadata bytes>`.
Reply 2xx to accept, 409 for a duplicate `event_id`. Other 4xx responses (except 401, 403,
408 and 429) move the record to `queue/dead/`; anything else is retried with backoff
(5 s doubling to 10 min). Records are never dropped while the backend is down, except
when the queue passes `queue_max_mb`: then the oldest full frames go first, and metadata and
ROI images are kept as long as possible.

PoP metadata example:

```json
{
  "event_id": "4f0c…", "type": "creative_change",
  "site_id": "AKL-BRITOMART-01", "camera": "britomart-north", "camera_model": "M5525-E",
  "gateway_id": "akl-gw-01",
  "play_started_at": "2026-10-05T07:41:10.200Z", "settled_at": "2026-10-05T07:41:10.900Z",
  "captured_at": "2026-10-05T07:41:12.180Z", "ntp_synced": true,
  "sequence": 18342, "local_creative_id": "c-0007", "phash": "e1c3a5f0b2d49687",
  "previous_creative_ms": 10020, "confidence": 0.94, "is_blank": false, "video_creative": false,
  "roi": [[0.18,0.16],[0.84,0.22],[0.82,0.80],[0.19,0.86]],
  "profile": {"slot_count": 6, "slot_ms": 10000},
  "quality": {"sharpness": 812, "clipped": 0.002, "banding": 1.1, "usable": true,
              "burst_index": 1, "burst_captured": 3},
  "frame_width": 1920, "frame_height": 1080,
  "frame_sha256": "…", "roi_sha256": "…", "app_version": "0.1.0"
}
```

Alert metadata: `type: "fault"`, `fault`, `state` (`raised`, `cleared`, `occurred`),
`severity`, `since`, `at`, `detail`. Faults: `black`, `blank`, `frozen`, `blank_slot`,
`loop_changed`, `missing_creative`, `partial_failure`, `blurred`, `camera_moved`,
`stream_lost`, `snapshot_failed`, `poor_pop_image`.

**`POST /v1/heartbeat`** (JSON, every `heartbeat_s`): per camera `connected`, `state`,
`fps`, `last_frame_at`, `last_play_at`, `plays_last_hour`, `expected_per_hour`,
`coverage`, `active_faults`, plus queue depth. **The backend should alert when heartbeats
stop**: a gateway that is offline can't report itself.

`local_creative_id` is per camera and only groups repeat plays. Matching a play to a booked
campaign happens on the backend (e.g. against the creative files in Vault).

## Field trial: tuning on real footage

Record a few loops from a real board (day and night), then replay:

```sh
ffmpeg -rtsp_transport tcp -i "rtsp://user:pass@CAMERA/axis-media/media.amp?resolution=1280x720&fps=10" \
       -t 600 -c copy board.mp4
build/popreplay board.mp4 --roi 0.18,0.16 0.84,0.22 0.82,0.80 0.19,0.86 \
       --slots 6 --slot-ms 10000 --csv trace.csv
```

It prints every play and fault with its video timestamp. `trace.csv` has the per-frame cut
score, state, brightness and sharpness, which is useful for charting why a change was or
wasn't counted. Adjust `detector` settings in the config to match.

## End-to-end test without a camera

```sh
cd tools
python3 make_test_video.py /tmp/board.mp4          # 200 s synthetic rotation (~4 min to render)
python3 receiver.py --api-key test-key --hmac-key test-secret --out /tmp/received &
python3 fake_camera.py --port 8081 &
../build/pop-gateway -c test-config.json            # rtsp_url=/tmp/board.mp4, "realtime": true,
                                                    # vapix_url=http://127.0.0.1:8081
```

The synthetic scene has 4 x 8 s slots (two static creatives, one with an animated element,
one full-motion video and a crossfade), then a blank slot for two loops and 16 s of black screen.
Expected: 23 plays, `blank_slot` twice, `missing_creative`, `black` raised and cleared.

## Known limitations (v1)

* **Detection timing vs the camera clock:** the gateway stamps frames when they arrive
  (normally within a second of the camera). Keep the gateway on NTP.
* **Back-to-back video creatives** with no still moment between them are split at the
  expected slot boundary, so they need a rotation profile.
* **Banding check** compares frames within a burst, so it needs a burst of 3 or more.
* **Blur check** compares each creative with its own first sighting. It needs a few loops of
  history after a restart.
* **First play after a restart** isn't logged: the creative on screen at start-up becomes the
  reference, and logging begins at the next change.
* **ACAP** (`acap/`) is a scaffold. It builds to an `.eap` with ACAP Native SDK 12.5 (aarch64;
  `dist/` has a build) but hasn't run on a camera yet. Missing: burst scoring, uploads (needs
  libcurl bundled), Axis events, settings page.
