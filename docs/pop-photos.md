# Best PoP photos into SharePoint

Every evening and morning the clearest proof-of-play photo of each campaign at each
site goes into SharePoint, ready for client reporting, with no one picking through
thousands of camera shots:

```
Sales / 00_Sales (NEW FOLDER) / Booked Campaigns / PoP Images /
  2026 / 10 - October / Curletts In /
    LUMO_19391_HBO.Max_Curletts-In_2026-10-13_1828_Day_Score98_v1.jpg
    LUMO_19391_HBO.Max_Curletts-In_2026-10-13_2215_Night_Score81_v1.jpg
    LUMO_19023_Pals_Curletts-In_2026-10-13_1105_Day_Score92_v1.jpg
    LUMO_19023_Pals_Curletts-In_2026-10-13_1611_Day_Score90_v2.jpg      (second execution)
```

File name: campaign ID, advertiser (spaces as periods, from the Doohly campaign; from an imported Vault
pull only for screens not in Doohly; left out if there is none), site (its Vault screen name), date and time the photo was
taken, Day or Night, score (0 to 100), and the version (v1, v2, … one per booked artwork
file, so a campaign with several executions gets a photo of each). The photo is the
full camera frame, so the billboard is seen in its surroundings. Files uploaded under an
older naming are renamed to the current one at the next run (same photo, time and score).

## When, and what replaces what

| Time (NZ) | Uploads |
|---|---|
| 8:00 pm | the best **daytime** photo taken since the last 8 pm run |
| 8:00 am | the best **night** photo taken since the last 8 am run |

* The first run after a campaign starts at a site puts its best photo up.
* Each later run looks at the new photos and replaces the one in SharePoint only with a
  photo that **scores higher** (the new file is added, the old one deleted). The photo
  already up is scored again alongside the new ones, so a dull or rainy day can't make
  an average photo look better than it is.
* The year and month folder is the month the campaign was first seen at the site, and
  stays that way, so a long campaign's photos stay together.
* Creatives not linked to a campaign on the Bookings page (unassigned, programmatic,
  house) are never uploaded. When an unassigned creative is linked later, its photos are
  picked up from the next run.
* A missed run (computer off, container down) catches up at the next start, up to 3 days
  back. A failed run is retried hourly and posts one alert to Teams/Slack (and one when
  it recovers).

The **Bookings** page shows how many photos are up, when each run last ran and the next
times, under the artwork sync line, with **Upload day photos now** and **Upload night photos
now** buttons and the latest uploads (each links to the file in SharePoint). A button
does exactly what the scheduled run does, over the photos since the last run (the last
24 hours the first time), so it is the way to check the set-up without waiting for 8 pm.
The scheduled runs still happen as usual afterwards.

## How a photo is scored

Only photos of the same creative in the same light are compared (day against day,
night against night). The score (`tools/popscore.py`) combines:

| Part | What it catches |
|---|---|
| Clarity | Fine detail (text, edges, layout) agreeing with the booked artwork and with the creative's typical look in its other photos. Glare, sunstrike, rain on the lens, haze and anything in front of the screen wipe out or distort detail. |
| Coverage | The two worst parts of the screen (of 8 tiles) still agreeing: a hot spot, a raindrop or a truck covering one corner. |
| Layout | The broad light and dark areas agreeing: a sun reflection or veiling glare across the face. |
| Sharpness | Against this creative's usual sharpness in that light (rain, fog, focus). |
| Contrast | Against its usual contrast (washed out by the sun or haze). |
| Clipping | Blown-out pixels beyond what is normal for it (sunstrike; some bloom at night is normal). |

One bad part caps the score, so a photo that is sharp but half covered by glare can't
score well. Day and night come from the sun's position at the site (twilight is
skipped, as the light changes by the minute) and are checked against the photo's own
brightness.

On test photos made from this month's booked artwork with glare, sunstrike, haze,
rain on the lens, blur, a vehicle in front and night bloom added, the photo chosen was a
clean one every time (36 of 36 campaign versions, day and night). Real footage will
show where the weights need tuning: look at a few uploaded photos and the ones next to
them on the review page after the first week.

## Setting up

The app that reads the artwork also writes the photos, so it needs **write** access to
the LUMO site (it was set up read only). Once, as an admin, from the repo:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\sharepoint\setup-sharepoint-app.ps1 -PermissionOnly
```

That changes the app's site grant from read to write and keeps its secret, so nothing
else changes. Sites.Selected still limits it to the LUMO site, and the uploader only
writes under `PoP Images` (only files it uploaded itself are ever deleted).

Then rebuild and restart (`docker compose up -d --build`, or build the images and update
the Portainer stack). On first start both runs are due, so the last 24 hours of day and
night photos go up straight away.

## Settings (environment)

| Variable | Service | Default | |
|---|---|---|---|
| `POP_UPLOAD` | creatives-sync | `1` | `0` turns the uploads off |
| `POP_ROOT` | creatives-sync | `00_Sales (NEW FOLDER)/Booked Campaigns/PoP Images` | folder in the Sales library |
| `POP_DAY_AT` / `POP_NIGHT_AT` | creatives-sync | `20:00` / `08:00` | local time (TZ) |
| `POP_CATCHUP_DAYS` | creatives-sync | `3` | how far back a missed run catches up (photos are kept 3 days) |
| `POP_RETRY_MIN` | creatives-sync | `60` | retry after a failed run |
| `POP_DEFAULT_COORDS` | receiver | `-36.85,174.76` (Auckland) | for sunrise and sunset |
| `POP_SITE_COORDS` | receiver | | sites elsewhere: `SITE_ID=lat,lon;SITE_ID=lat,lon` (e.g. Christchurch `-43.53,172.62`) |
| `POP_BEST_SAMPLES` | receiver | `240` | photos scored per version per run, spread over the window |

What is up there, with each photo's score and parts, is in the sync volume's
`.sync/pop-uploads.json`; deleting that file makes the next run start afresh (the files
already in SharePoint stay, and new ones are added beside them).

## Receiver API (for creatives-sync)

`POST /pop/best` (bearer API key) with `{"since": unix, "until": unix, "phase": "day"|"night",
"current": {"SITE|LUMO_x|version": event_id}}` returns the best photo per campaign, site
and version in that window, with its score and parts, and the score of the current
photo re-scored alongside. `GET /pop/frame/<event_id>.jpg` returns the full frame.
