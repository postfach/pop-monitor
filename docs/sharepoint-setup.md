# Artwork sync from SharePoint (creatives-sync)

`creatives-sync` is the third container in the stack. It keeps the booked-artwork
library behind the Bookings page current, straight from SharePoint, so artwork
matching needs no one in the loop:

```
SharePoint: LUMO site / Sales / 00_Sales (NEW FOLDER) / Booked Campaigns / Creative+MIs
      │  Microsoft Graph, app-only, this site only (Sites.Selected)
      │  reads Creative+MIs; writes the best PoP photos to Booked Campaigns/PoP Images
      ▼
creatives-sync ──writes──► pop-creatives volume: vault-creatives.json + images
      ▲                          │ (read-only)
      │ which campaigns/screens  ▼
      └────────────────────── receiver (Bookings page, artwork matching)
```

## One-off setup (about 5 minutes, Global Admin)

On any Windows PC, in a PowerShell window in the repo's `deploy\desktop` folder:

```powershell
powershell -ExecutionPolicy Bypass -File ..\sharepoint\setup-sharepoint-app.ps1 -EnvFile .env
docker compose up -d --build
```

Sign in with your admin account when the browser opens and accept the permissions it
asks for (they are for you, to create the app; the app itself gets far less). The
script:

1. creates the app registration **PoP Monitor - creatives sync** (single tenant);
2. adds the Microsoft Graph application permission **Sites.Selected** and grants admin
   consent. On its own Sites.Selected opens no site at all;
3. grants the app **write** on `https://lumodigital.sharepoint.com/sites/LUMO` only (it
   reads the artwork and puts the best PoP photos in `Booked Campaigns/PoP Images`: see
   [`pop-photos.md`](pop-photos.md); `-SiteAccess read` keeps it read only, and then the
   photo uploads fail with an access error);
4. creates a client secret valid for 24 months;
5. signs in as the app and lists `Creative+MIs` to prove it works;
6. writes `AZ_TENANT_ID`, `AZ_CLIENT_ID`, `AZ_CLIENT_SECRET` and `AZ_SECRET_EXPIRES`
   into `.env`.

For **Portainer**, run it without `-EnvFile` and paste the four printed values into the
stack's environment variables, then build the images (`sh deploy/build-images.sh`
now builds three) and update the stack.

Then open the **Bookings** page: under the Vault pull line it shows *Artwork synced from
SharePoint …* within a minute of the container starting.

### Doing it by hand instead

1. Entra admin centre > App registrations > New registration: name
   `PoP Monitor - creatives sync`, single tenant, no redirect URI.
2. API permissions > Add > Microsoft Graph > Application permissions > `Sites.Selected` >
   Add, then **Grant admin consent**.
3. Certificates & secrets > New client secret (24 months). Copy the value.
4. Give the app write on the site. Graph Explorer (signed in as admin, consent to
   `Sites.FullControl.All`):
   * `GET https://graph.microsoft.com/v1.0/sites/lumodigital.sharepoint.com:/sites/LUMO` → copy `id`
   * `POST https://graph.microsoft.com/v1.0/sites/{id}/permissions` with
     `{"roles":["write"],"grantedToIdentities":[{"application":{"id":"<client id>","displayName":"PoP Monitor - creatives sync"}}]}`
   (or PnP PowerShell: `Grant-PnPAzureADAppSitePermission -AppId <client id> -DisplayName "PoP Monitor - creatives sync" -Site https://lumodigital.sharepoint.com/sites/LUMO -Permissions Write`).
   An app set up earlier with read only: `setup-sharepoint-app.ps1 -PermissionOnly` (or
   `PATCH https://graph.microsoft.com/v1.0/sites/{id}/permissions/{permission id}` with
   `{"roles":["write"]}`) adds write and keeps the secret.
5. Put the tenant ID, client ID, secret and its end date in `.env` (or the stack
   variables) as `AZ_TENANT_ID`, `AZ_CLIENT_ID`, `AZ_CLIENT_SECRET`, `AZ_SECRET_EXPIRES`.

## What it does, and when

It runs:

* when it starts;
* within 5 minutes of the inputs changing: a new Vault pull (the receiver also nudges it
  straight away when one is uploaded), or a site linked to a different Vault screen;
* every `SYNC_INTERVAL_MIN` minutes (default 240) to pick up new or replaced artwork;
* when someone clicks **Sync now** on the Bookings page.

For each monitored site's Vault screen and each campaign booked on it this week or next:

1. **Find the campaign's folders.** The year folders under `Creative+MIs` (this year and
   last) are read once per run, including `Subtle/<n>.<month>`, and every folder with a
   5-digit LUMO id in its name is indexed (`LUMO_19762_Wolfbrook`, `LUMO_14460 & LUMO_17421 …`).
   Months older than `MONTHS_BACK` (8) are skipped; a campaign not found is looked up
   with Graph search. `POPs`, `Contracts`, `Archive`, `Not Booked`, `Wrong spec`, `Old`
   and `Superseded` folders are ignored.
2. **Read the Material Instructions.** Every `*Material Instructions*` / `*MI*` /
   `Booking Update` workbook in the campaign's folders is parsed (cached by version, so
   each is downloaded once). The newest one (by "Instructions last updated") with a row
   for the screen in that week decides which files play, their dates (from the column
   header such as "5th Oct - 7th October", else the creative or booking dates), rotation
   and day-parting times. Each workbook's **Screen Specs** tab gives every screen's pixel
   size, remembered across runs.
3. **Pick the files.** Named in the MI (typos, a missing extension and 2x artwork are
   tolerated), else files named for the screen plus same-size files named for no other
   screen, else the screen's size. Weekly subfolders (`w.c 05.10.26`) narrow it to the
   week. `via` in the manifest says which rule picked each file (`mi`, `filename`,
   `size`), and `note` says why.
4. **Download** only new or changed files, write `vault-creatives.json` in one step, and
   remove images no longer booked.

Campaigns with nothing usable are listed under `missing` with the reason (no folder, MI
names a file that isn't there, only videos, no artwork of the screen's size). The
Bookings page shows the reason on hover and links to the campaign's folder.

A run that fails never replaces the last good library; if one campaign's folder can't
be read, its files from the previous run are kept. The first failure and the recovery
are posted to the Teams/Slack webhooks, and the Bookings page shows the error. It warns
30 days before the client secret expires (page and Teams).

A first run for three screens takes about 120 Graph calls and a few seconds; later runs
about 70 calls and download nothing unless artwork changed. Graph's limits are far above
this.

## Dynamic creatives (DCO)

Doohly books a dynamic creative (one that changes with the weather or time of day) as an
**HTML zip**: a few hundred bytes that load the ad live, with no picture in it. When a
campaign's Doohly booking has one, creatives-sync looks for the renders of its variants in
**IT / 6. LUMO Labs** instead of Creative+MIs:

1. **The campaign's folder**: one whose name has the campaign's `LUMO_` number, or that holds
   a file that does (usually a copy of its MI), e.g.
   `3.2026/Q3/Bentley - Weather Triggers` (it has `Copy of Bentley - LUMO_19409 - … Material
   Instructions.xlsx`). Failing that, the newest folder named for the advertiser.
2. **The screen's subfolder**: named for the screen, with or without `LUMO-`
   (`Supplied Files/LUMO-Greenlane`, `LUMO_Bentley_export_video/Anzac`). Failing that, files of
   the screen's size that aren't in another screen's subfolder.
3. **Every variant** in it becomes the campaign's artwork for that screen (images, or frames
   of videos: 6 per video, 2 s apart, as the renders barely move). Where a variant is there as
   both, the image is used. A name like `GRE_SUN-HL_1214-1510_Sunny.png` is shown as *Sunny
   12:14-15:10 (SUN-HL)*.

The Bookings page shows the campaign as **DCO · 30 variants** with one picture, and a matched
creative names the variant it looked like. The variants of one campaign never count against
each other in the match margin, only other campaigns do. If nothing is found, the campaign is
in the *without artwork* list with the reason; a render uploaded there also works.

LUMO Labs is walked (this year's folders, plus last year's in January and February, down to
the campaign folders' files: about 150 Graph calls) at most every 12 hours, and again as soon
as a DCO campaign isn't in it. The app's access to the LUMO site covers the IT library, so no
setup is needed. (Graph's search doesn't work with this app's site-only access, which is why
the folders are walked.)

## Settings (environment)

| Variable | Default | |
| --- | --- | --- |
| `AZ_TENANT_ID`, `AZ_CLIENT_ID`, `AZ_CLIENT_SECRET` | | From the setup script. Or `AZ_CERT_PATH` + `AZ_CERT_THUMBPRINT` for a certificate |
| `AZ_SECRET_EXPIRES` | | `yyyy-mm-dd`; enables the expiry warning |
| `SP_SITE` | `lumodigital.sharepoint.com:/sites/LUMO` | Site, as host:path |
| `SP_LIBRARY` | `Sales` | Document library (name or URL segment) |
| `SP_ROOT` | `00_Sales (NEW FOLDER)/Booked Campaigns/Creative+MIs` | Folder in the library |
| `SYNC_INTERVAL_MIN` | 240 | Full refresh interval |
| `SYNC_SCREENS` | (monitored sites) | Comma-separated Vault screens, or `all` for every booked screen |
| `MONTHS_BACK` | 8 | Month folders indexed |
| `DCO_LIBRARY`, `DCO_ROOT` | `IT`, `6. LUMO Labs` | Where the renders of dynamic (HTML) creatives are |
| `DCO_INDEX_HOURS` | 12 | How long the walk of `DCO_ROOT` is reused |
| `DCO_VIDEO_FRAME_EVERY_S`, `DCO_VIDEO_MAX_FRAMES` | 2, 6 | Frames taken from a video render |

## Checking and troubleshooting

* Logs: `docker compose logs -f creatives-sync` (one line per run with the counts).
* Status: the line under the Vault pull on the Bookings page, or
  `docker compose exec receiver python3 -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8080/bookings/sync').read())"`.
* A run by hand: `docker compose exec creatives-sync python3 /app/creatives_sync.py --once`.

| Error | Fix |
| --- | --- |
| `not set up yet` | The `AZ_*` variables are empty: run the setup script, then `docker compose up -d` |
| `sign-in failed: invalid_client` | Wrong or expired secret: run the setup script again |
| `Graph 403 … accessDenied` | Consent or the site grant is missing: run the setup script again (it is safe to repeat) |
| `no document library called 'Sales'` | Set `SP_LIBRARY` to the library's name |
| `Graph 404 … itemNotFound` on `root:/…` | The folder moved or was renamed: set `SP_ROOT` |
| `the receiver has no Vault pull yet` | The weekly Vault pull hasn't run: see `docs/vault-pull.md` |

To remove access later: delete the app registration in Entra (that removes the site
grant too).
