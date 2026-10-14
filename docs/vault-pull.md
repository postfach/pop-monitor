# Vault bookings pull

> Screens whose player is in Doohly now take their bookings from Doohly instead
> (`docs/doohly.md`). The Vault pull below is still used for any other screen.

The review page compares what each site played with what Vault says is booked on
its screen. It reads `vault-bookings.json`, written each week by a scheduled task
that uses the Vault connector, or uploaded on the Bookings page. When Vault (or
Doohly) API access is available, a small importer can write the same file and
nothing else needs to change.

## Where it goes

* Docker Desktop: `deploy/desktop/bookings/vault-bookings.json` (mounted read-only
  into the receiver at `/bookings`).
* Portainer: the folder set by `POP_BOOKINGS_DIR` (default `./bookings` next to the
  stack file).
* An upload on the Bookings page is stored in the receiver's data volume. The newer of
  the two files is used.

## Format

```json
{
  "format": "pop-monitor/vault-bookings",
  "version": 1,
  "generated_at": "2026-10-05T18:00:00Z",
  "source": "vault-connector",
  "weeks": ["2026-10-05", "2026-10-12"],
  "proposals": [
    {
      "id": 14867,
      "campaign_id": "LUMO_19867",
      "name": "Act Epsom Electorate 2026",
      "advertiser": {"name": "ACT Party"},
      "status": "Booked",
      "start_date": "2026-10-05",
      "end_date": "2026-11-01",
      "updated_at": "2026-10-02T11:20:00+13:00",
      "url": "https://www.vaultcms.nz/dashboard/proposals/14867/slots",
      "screens": [
        {"name": "Parnell", "city": "Auckland", "region": "Auckland",
         "first_week": "2026-10-19", "last_week": "2026-10-26", "weeks": 2,
         "plays_per_loop": 3, "slot_types": ["normal", "sta"]}
      ]
    }
  ]
}
```

`proposals` is a list of booked campaigns exactly as the Vault connector's `proposal`
tool returns them (no money fields are needed or kept). A site is linked to a screen by
its **Vault screen name** setting (matched ignoring case and extra spaces).

What Vault gives, and its limits:

* `plays_per_loop` is the most plays per loop in any week of the booking, not a
  week-by-week figure. `weeks` lower than the span from `first_week` to `last_week`
  means some weeks in between aren't booked; the Bookings page marks these "some weeks".
* Where in the loop the slots fall (back to back or spread), and which artwork belongs
  to which campaign, aren't in Vault. Which artwork belongs to which campaign comes from
  SharePoint instead (see "Creatives pull" below); anything else is assigned on the
  Bookings page by hand (a campaign, Programmatic or House). Doohly's playlist would give both.

## The scheduled task

Runs weekly (Monday early morning NZ time) on the computer that runs Docker, because it
writes into the folder above. Its instructions:

1. List booked proposals running this week and next week with the Vault `proposals`
   tool (`statuses: ["Booked"]`, `from` = this Monday, `to` = next Sunday,
   `per_page: 75`, every page), fields `campaign_id, name, advertiser.name, status,
   start_date, end_date, updated_at, url`.
2. Read the previous `vault-bookings.json` if there is one. For proposals whose
   `updated_at` is unchanged, reuse its `screens`; read the others with the `proposal`
   tool (fields `campaign_id, name, advertiser, status, start_date, end_date,
   updated_at, url, screens`). The first run reads all of them (about 200 a week).
3. Write the file in the format above, to a temporary name first, then rename it over
   `vault-bookings.json`, so the receiver never reads a half-written file.
4. Report how many campaigns and screens it holds, how many were re-read, and any
   proposals it could not read.

## Creatives pull (artwork matching)

**This is now automatic**: the `creatives-sync` container does it with Microsoft Graph,
on its own schedule and whenever a new Vault pull arrives. Set-up and behaviour are in
[`docs/sharepoint-setup.md`](sharepoint-setup.md). The rest of this section describes the
format it writes (also accepted as a zip upload on the Bookings page) and the manual pull
it replaced.

The booked artwork lives in SharePoint: Sales site, `00_Sales (NEW FOLDER)/Booked Campaigns/
Creative+MIs/<year>/<month>/LUMO_<id> <name>` (animated campaigns under `<year>/Subtle/`).
Each campaign folder has the artwork for each screen size and usually a Material
Instructions spreadsheet that says, per screen and week, which file plays, from when to
when, and at what rotation. The pull turns that into `creatives/vault-creatives.json` plus
the images, next to `vault-bookings.json` (so `deploy/desktop/bookings/creatives/`), or a zip
of that folder uploaded on the Bookings page.

```json
{
  "format": "pop-monitor/vault-creatives",
  "version": 1,
  "generated_at": "2026-10-05T21:01:28Z",
  "source": "SharePoint Creative+MIs via Microsoft 365 connector",
  "weeks": ["2026-10-05"],
  "screens": [{"name": "Beach", "width": 576, "height": 396}],
  "creatives": [
    {"campaign_id": "LUMO_19023", "screen": "Beach",
     "name": "PAL020 Raspberry Launch Billboards_Teaser_Beach 576x396.png",
     "file": "beach/images/LUMO_19023/PAL020 Raspberry Launch Billboards_Teaser_Beach 576x396.png",
     "kind": "image", "from": "2026-10-05", "to": "2026-10-07", "rotation": null,
     "via": "mi", "folder_url": "https://lumodigital.sharepoint.com/sites/LUMO/Sales/..."}
  ],
  "missing": [{"campaign_id": "LUMO_19180", "screen": "Beach", "reason": "no creative folder"}]
}
```

`file` is relative to the folder holding the JSON. `from`/`to` (dates the file plays,
from the Material Instructions) and `rotation` (%) are optional; `via` says how the file
was chosen: `mi` (named in the Material Instructions for that screen), `filename` (the
file name names the screen) or `size` (the file matches the screen's pixel size).
Videos are listed with `kind: "video"` and no file; they aren't matched yet.

Steps, per monitored Vault screen and per campaign booked on it this week (from
`vault-bookings.json`):

1. Find the campaign's folders: list `Creative+MIs/<year>/<this month>`, the previous month
   and `Subtle/<n>.<month>`, matching folder names on the 5-digit id (names vary:
   `LUMO_19762_Wolfbrook`, `LUMO_14460 & LUMO_17421 …`); otherwise search folders for
   `LUMO_<id>` and keep hits under `/Creative+MIs/`. Prefer the newest month. Skip `POPs`,
   `Contracts`, `Archive`, `Not Booked`, `Wrong spec`.
2. Read the Material Instructions (`*Material Instructions*.xlsx`, `*MI*.xlsx`, or a
   `Booking Update` sheet): rows whose Site Name is `LUMO-<screen>` overlapping the week;
   creative columns give file names, often with a date range in the header ("5th Oct - 7th
   October") and a rotation %.
3. Pick the files for the screen: named in the Material Instructions (tolerating typos and
   missing extensions), else named for the screen, else the screen's pixel size. Where a
   folder has weekly subfolders (`w.c 05.10.26`), use this week's.
4. Download each image (the connector's `read_resource` saves it) into
   `creatives/<screen-slug>/images/<campaign_id>/<file name>` and write the manifest;
   record campaigns with nothing usable in `missing` with the reason.

The first pull for three screens took about 220 connector calls (most of them folder
listings). Next weeks can reuse files already pulled when the folder hasn't changed.
`creatives-sync` now does this directly with Microsoft Graph (`Sites.Selected`, read on the
LUMO site only), reusing unchanged files and parsed Material Instructions between runs.

Fields `creatives-sync` adds: `note` (why the file was chosen), `hours` (day-parting from the
MI's Start/End Time, e.g. `09:00-17:00`), and per missing entry `name` and `folder_url`.

What was learned from the first pull (week of 5 Oct, Curletts In / Albany Expressway In /
Beach): 91 of 96 campaign-screen pairs had artwork; missing were campaigns with no
Creative+MIs folder yet (Mitre 10 LUMO_19180, Kernel LUMO_19766), videos only (KidsCan),
or Material Instructions naming no file (ASB). Some Material Instructions disagree with
the Vault booking (CUPRA, Asahi, ACT), one has day-parting (Anchor: Real Good 9am–5pm,
Wake Up to Milk overnight; not modelled yet), and one gives its rotation only as a picture
of a table (Chemist Warehouse).
