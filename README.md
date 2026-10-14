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
| `tools/popscore.py` | PoP photo quality score (glare, sunstrike, rain, blur, occlusion, clarity) and day/night |
| `sync/` | `creatives-sync`: booked artwork from SharePoint via Microsoft Graph, bookings and players from Doohly (`doohly.py`), and the best PoP photos back into SharePoint (`pop_upload.py`), with tests and a mock Graph |
| `deploy/sharepoint/` | One-off PowerShell set-up of the app registration and site grant |
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

`deploy/docker-compose.yml` is a three-container stack:

* **gateway**: the PoP monitor itself.
* **receiver**: an interim backend that stores PoP images and alerts and shows them on a
  review page at `http://DOCKER-HOST:8080`. Use it until the real backend exists, then point
  `server_url` at the backend and drop it.
* **creatives-sync**: keeps the booked-artwork library for the Bookings page current from
  SharePoint (optional: without its `AZ_*` settings it stays idle and says so on the page).

1. On the Docker host (any Linux x86-64 host that can reach the cameras on ports 80 and 554):
   ```sh
   git clone <this repo> /opt/pop-monitor-src && sh /opt/pop-monitor-src/deploy/build-images.sh
   mkdir -p /opt/pop-monitor && cp /opt/pop-monitor-src/deploy/config.trial.json /opt/pop-monitor/config.json
   ```
   Edit `/opt/pop-monitor/config.json`: camera IPs, ROI, rotation profile.
2. In Portainer: **Stacks > Add stack > Web editor**, paste `deploy/docker-compose.yml`, and
   add the variables from `deploy/stack.env.example`. Deploy.
3. Open `http://DOCKER-HOST:8080` to see PoP images, alerts and camera status. The
   **Refresh** menu (top right) sets how often the page updates, from every 5 s to every hour
   or off; the choice is remembered in that browser, and `?refresh=SECONDS` in the address sets
   it for a bookmark or wall screen.
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

**Time zones.** `TZ` in `.env` (default `Pacific/Auckland`) sets the time shown to people:
the review page, gateway log and Teams/Slack alerts. Records sent to the backend keep UTC
timestamps (`...Z`) so nothing is ambiguous across daylight-saving changes or regions.

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

**Site ID and display name.** The site ID is the permanent key: it appears in URLs
(`/site?site=AKL-BRITOMART-01`), in the API, in the creative library file name and as
`site_id` in every record sent to the backend, so it should match the site code used in
Vault. The display name ("Britomart North") is what people read: the review page, Sites
list, region editor, Teams/Slack alerts and the gateway log all show it, falling back to
the site ID when it is blank. Records carry it as `site_name`. Changing the site ID of an
existing site keeps its creative library.

The first change made on the Sites or region pages creates `cameras.json` next to the queue
(in the Docker volume). From then on that file is the camera list and the `cameras` array in
`config.json` is only used if `cameras.json` is deleted. `defaults` (such as the camera
user and password) still come from `config.json` and `.env`.

* Back it up: `docker compose cp gateway:/var/lib/pop-gateway/cameras.json .`
* A password typed on the Settings page is saved in `cameras.json`. Where cameras share a
  login, leave it blank so the default from `.env` is used instead.
* Settings the form doesn't show (detector tuning, time-of-day profiles, snapshot options)
  are kept when a site is edited, and can be set in `config.json` under `defaults`.

**Review page.** With all sites shown, the PoP section has only the newest image from each
site. Click a site's name (or **All PoP from this site**) to see its recent plays. Every
image tile is the same size; portrait boards are shown whole with black bars at the sides.

**Old records.** The review page only shows sites the gateway currently has. Renaming a site
ID moves its stored PoP and alerts to the new ID. Records left under an ID that no longer
exists (a removed site, or one renamed before this version) are listed at the bottom of the
Sites page under **Records from sites that no longer exist**, where **Delete** removes them
and their images from the receiver.

## Coverage alert

Coverage is slot time accounted for ÷ slot time the rotation should produce. Bookings are
share of time, so one campaign can hold two or more slots in a row and play as one long run
(or back-to-back copies). Each play is credited with its length in slots, capped at that
creative's usual run, so a booking that fills 2 slots counts as 2 slots delivered, not 1
play. A creative that holds over past its usual run (a stuck player) isn't credited for the
extra time.

The gateway raises `low_coverage` when coverage over the last **30 minutes** falls below
**90%**, and clears it once it is 2 points above the threshold again. It is only measured
while the site is in operating hours, the video is connected and a slot length is set (not
for takeovers); the count restarts after a restart, a video outage, a region edit or a
rotation change, and the first judgement waits for a full window, so restarts don't raise
it. Set the threshold and window per site on the Settings page (0 = off), or for every site
in `config.json`: `"defaults": {"coverage_min_pct": 90, "coverage_window_min": 30}`.

Low coverage usually means similar creatives back to back aren't being told apart, the region
needs adjusting, or the slot length is wrong. Coverage far above 100% means the slot length is
set too long, or animated creatives are being split into several plays. The review page shows
hourly coverage, and "warming up" during a site's first hour of counting.

The fault checks also allow for share of time:

* **Loop changed** means completed plays stop fitting whole slots (more than 40% off the slot
  grid over two loops, twice in a row), not that the order of creatives changed.
* **Missing creative** compares the creatives seen in recent whole loops with how many the
  loop should hold: the number of certain Vault bookings when the site is linked to a Vault
  screen (see Bookings), otherwise slots minus the extra slots each creative takes up.
* **Frozen** waits for the current creative's usual run plus one slot before raising.

## Bookings vs delivery (Vault)

The **Bookings** page compares what each site played this week with what Vault says is
booked on its screen, and works out **true coverage**: booked slots delivered ÷ booked slots,
leaving out house and unbooked fill.

1. A weekly pull of booked campaigns is saved as `vault-bookings.json` in the bookings
   folder (`deploy/desktop/bookings/`, or `POP_BOOKINGS_DIR` for Portainer), or uploaded with
   **Upload Vault pull or creatives** on the Bookings page. The format and the scheduled task are in
   `docs/vault-pull.md`.
2. Set each site's **Vault screen** on its Settings page (the list is filled from the pull),
   or in the `vault_screen` column of the CSV import.
3. The booked artwork comes from SharePoint (Booked Campaigns / Creative+MIs, using each
   campaign's Material Instructions to pick the file for the screen and its dates). The
   **creatives-sync** container keeps it current automatically: at start, within 5 minutes
   of a new Vault pull or a site's Vault screen changing, every 4 hours, and on **Sync now**
   on the Bookings page. It needs a one-off app registration with access to the LUMO
   site only: run `deploy/sharepoint/setup-sharepoint-app.ps1` (see
   `docs/sharepoint-setup.md`). A library can still be uploaded as a zip with the same
   button (the newest wins). Format: "Creatives pull" in `docs/vault-pull.md`.
4. The page lists the campaigns booked on that screen this week with their plays per loop and
   artwork, and every creative seen with its share of the loop. Each creative's PoP images
   are compared with the booked artwork for the screen (fine detail with the camera's glare,
   vignetting and exposure removed, best of small offsets, median over up to 5 recent images):
   * **artwork match**: at least 40% and at least 8 points ahead of every other campaign. It is
     assigned to that campaign automatically ("Auto: …" in the list).
   * **looks like …?**: 30–40%, or too close to another campaign. Stays unassigned.
   * **matches no booked artwork**: usually programmatic, house, or artwork missing from
     SharePoint.
   * **off schedule**: matched artwork that played outside its dates in the Material
     Instructions (a teaser left on, a new creative started early).
   Anything can be set by hand to a campaign, **Programmatic** or **House**; that always wins
   and is kept for later weeks (a creative linked to a campaign that isn't booked that week is
   counted with house). Video creatives aren't matched yet.

Each site is a collapsed line on load (true coverage, booked vs available slots, unassigned
slots, campaigns not seen, warnings); click it to open the full card (the page remembers which
are open). The gateway often gives one artwork several creative IDs (day and night exposure,
for example), so **Creatives seen** shows one row per artwork: IDs matched to the same booked
artwork or linked to the same campaign are bundled, and unassigned IDs whose PoP images look
alike are bundled too. The row's campaign list sets all its IDs at once; "N IDs" opens the
individual IDs, each of which can still be set on its own.

**Merging IDs at the gateway.** When the receiver is sure two IDs are one artwork, it merges
them: both match the same booked image, or (unassigned) several good PoP images of each look
alike (median at least 0.75), and each has at least 3 plays this week. The newer ID is then read
as the older one everywhere (history included; the review cards show the older ID, with the
reported one on hover), any hand-set link moves to the older ID, and the gateway is told
(`POST /api/cameras/<site_id>/merge-creatives` with `{"merges": [{"from": "c-0042", "into":
"c-0005"}]}`). The gateway keeps the merged ID's fingerprint as another look of the older one
(up to 3 looks per creative, saved in its creative library), so later plays, day or night,
report one ID. Merges are listed in `creative-merges.json` in the receiver's data folder and
shown as "+N merged" on the Bookings page. Set `AUTO_MERGE=0` on the receiver to turn this off;
**Reset creatives** on a site clears its merges.

The report behind the page is worked out in the background about once a minute and when links
or set-up change, so the page and the review cards open straight away. Each creative's artwork
match is worked out once and kept (in `art-matches.json` in the data folder), re-checked every
6 hours (30 minutes while unmatched) or when the artwork library changes.

Each card shows true coverage (booked slots delivered ÷ booked slots, over the certain
bookings that have artwork or creatives linked), booked vs available slots, programmatic, house,
unassigned and blank slots per loop, and how much of the week has been monitored. Campaigns
with no artwork in SharePoint and no creative linked show as "not linked" and aren't judged. Warnings cover unassigned
creatives, linked campaigns whose creatives weren't seen or are under-delivering, and more
plays booked than the loop has slots, plus off-schedule artwork. The review page shows each
PoP's campaign (marked "artwork match" when matched automatically) or Programmatic / House.
The receiver image includes numpy and Pillow for the matching; without them the page works
with links set by hand only.

Limits of the Vault data: it gives each booking's first week, last week and number of weeks,
not which weeks, so a booking with gaps is shown as "may be booked this week" (and left out of
true coverage) unless this is its first or last week. `plays_per_loop` is the most in any
week. Vault or Doohly API access would remove both limits, and Doohly's playlist would also
say which creative belongs to which campaign, so the links could then be made automatically. The receiver also tells the gateway how many
campaigns are certainly booked, which the missing-creative check uses.

### Doohly

With a Doohly API token (`DOOHLY_API_TOKEN`), creatives-sync reads each monitored screen's
Doohly player (read only) and the Bookings page uses its bookings instead of the Vault pull:
exact weeks, plays per loop, sold / bonus / non-guaranteed bonus, the creatives booked with
their weighting and day-parting (images still from SharePoint, by file name), the loop's slot
count and length (a warning if the site's rotation differs), the player's location for day and
night, and a player-offline alert. Details: `docs/doohly.md`.

### Best PoP photos into SharePoint

At 8 pm (daytime photos) and 8 am (night photos) creatives-sync puts the clearest PoP
photo of each campaign, site and artwork version into SharePoint under
`Booked Campaigns/PoP Images/<year>/<MM - Month>/<site>/`, named like
`LUMO_19702_Curletts-In_2026-10-13_1432_Day_Score87_v1.jpg`. Photos are scored for glare,
sunstrike, rain, haze, blur, anything in front of the screen and how clearly the creative
shows (`tools/popscore.py`); a later run replaces a photo only with a higher-scoring one.
Unassigned, programmatic and house creatives are left out. It needs the SharePoint app to
have write access (`setup-sharepoint-app.ps1 -PermissionOnly`). Details and settings:
`docs/pop-photos.md`.

## Storage and clean-up (receiver)

The receiver keeps PoP records, alerts and gateway heartbeats in a SQLite database
(`pop.db` in its data volume, `tools/store.py`) and the photos as files next to it. An hourly
clean-up keeps the disk from filling:

| What | Kept | Setting |
|---|---|---|
| PoP photos (full frame and screen crop) | 3 days | `POP_IMAGE_DAYS` |
| Photos attached to alerts | 30 days | `POP_ALERT_IMAGE_DAYS` |
| PoP and alert records (the details of every play) | 90 days | `POP_RECORD_DAYS` |
| Gateway heartbeats | 7 days | `POP_HEARTBEAT_DAYS` |

The best day and night photo of every campaign is in SharePoint already (8 pm / 8 am), so
3 days is enough. Removing a photo keeps its play: delivery and coverage are unchanged; the
review page shows *photo removed after 3 days*, and artwork matching and the Bookings page
use the photos still there. The creative matching on the gateway doesn't use these photos
at all. The Health page shows the record and photo counts, their size and the free disk.

Upgrading from a version with `records.jsonl`: on first start it is imported into `pop.db`
(about 5 s per 150,000 records) and renamed `records.jsonl.imported`; delete that once
you're happy.

## Adding many sites

* **From Doohly** (with a Doohly token): the Sites page lists the Doohly screens not set up yet;
  tick one and the Add site form opens with its ID, name, Vault screen and rotation filled in,
  so only the camera's address and region are left (`docs/doohly.md`).

* **Duplicate site** (on a site's Settings page) opens the Add site form pre-filled with that
  site's model, ports, transport, rotation, hours and alert settings. Enter the new ID, name
  and host. The password isn't copied.
* **Import from CSV** (on the Sites page): download the template, fill one row per site in
  Excel, upload it, check the preview (each new row is validated by the gateway without being
  created), then Import. Only `site_id` and `host` are required; blank cells use the
  defaults. Tick "Update sites that already exist" to bulk-edit: only filled cells change.
  Each new site starts with the full frame as its region, so set the region next (the results
  list links to each one).

## Gateway health

The **Health** page shows each gateway from its latest heartbeat: online or offline, uptime,
cameras connected, records waiting to upload and the age of the oldest, upload counts and
the last error, records rejected by the server, disk space and clock sync, and per site the
video uptime, reconnects, frame rate, coverage and open problems. Warnings are listed at the
top of each gateway.

If a gateway sends no heartbeat for 3 × `heartbeat_s` (at least 3 minutes), the receiver
records a `gateway_offline` alert, shows a banner on the review page, and posts to the Teams
and Slack webhooks set in `.env` (`POP_TEAMS_WEBHOOK`, `POP_SLACK_WEBHOOK`). It posts again
when heartbeats resume. Records made while the gateway's uplink was down are uploaded
when it reconnects.

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
  "site_id": "AKL-BRITOMART-01", "site_name": "Britomart North", "camera_model": "M5525-E",
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
`stream_lost`, `snapshot_failed`, `poor_pop_image`, `low_coverage`. The interim receiver adds
`gateway_offline` (with an empty `site_id` and a `gateway_id`). When a site is edited or
removed, the gateway sends `cleared` for each of its open faults.

**`POST /v1/heartbeat`** (JSON, every `heartbeat_s`): per camera `connected`, `state`,
`fps`, `last_frame_at`, `last_play_at`, `plays_last_hour`, `expected_per_hour`,
`coverage`, `coverage_data_min`, `coverage_window_pct`, `uptime_pct`, `reconnects`,
`active_faults`, plus `health` (the same object as the gateway's `GET /api/health`: uptime,
version, upload counts and last error, queue size and oldest record, rejected records, disk
space, NTP state). **The backend should alert when heartbeats stop**: a gateway that is
offline can't report itself. The interim receiver does this (see Gateway health).

`local_creative_id` (`c-0001`, `c-0002`, …) is per site and only groups repeat plays of the
same artwork, so a 6-slot loop should settle on about six IDs plus one for a blank slot.
Matching a play to a booked campaign happens on the backend (e.g. against the creative files
in Vault). The gateway recognises a repeat by three checks that must all agree: a 16×8
thumbnail of the board (overall shape), a 32×16 map of fine detail with glare, vignetting and
exposure removed (text, edges, layout), and the colour layout relative to the overall tint.
This tolerates day/night exposure changes while keeping apart creatives of similar colour
under the same glare (the coarse thumbnail alone is dominated by the site's lighting). Each
stored creative slowly adapts to the conditions it is seen in, but only from clear matches.
Libraries saved before the detail check gain it the next time each creative is seen; if two
creatives had been merged into one ID, the first one seen keeps the ID and the other gets a
new one (earlier records stay under the old ID). Video creatives are fingerprinted from an average
over their motion. The library (up to 256 creatives per site) is saved every minute to
`creatives/<site_id>.json` in the gateway volume and reloaded on start, so IDs stay the same
across restarts and updates. To start a site's creatives afresh (testing, or a complete
change of campaigns), use **Reset creatives…** on its Settings page. It asks you to type the
site ID to confirm. The gateway forgets the site's creatives and clears its detection
alerts; new creatives continue the ID numbering. Tick the option to also delete the site's
stored PoP records, images, alerts and Bookings links, which restarts IDs at c-0001 (plays
from before the reset that are still queued on the gateway are then discarded on arrival).
The gateway API is `POST /api/cameras/<site_id>/reset-creatives` with
`{"restart_numbering": true|false}`. `camera` is still sent with the same value as
`site_name` for older consumers.

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
