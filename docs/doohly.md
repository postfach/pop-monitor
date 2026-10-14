# Doohly: bookings, slots and players

Doohly is the playout system, so it knows exactly what each screen is meant to play.
With a Doohly API token, `creatives-sync` reads it (read only: it only ever sends GET
requests) and the Bookings page uses it in place of the weekly Vault pull for every
screen whose player is in Doohly.

| What | From Doohly | Was |
|---|---|---|
| What is booked on a screen, by week | the player's live bookings (booked, reserved, paused) | Vault pull: first week, last week and a count of weeks |
| Plays per loop | each booking's plays per loop, averaged over the days it is on (a booking for 4 days × 1 and 3 days × 2 counts 1.4) | the most in any week |
| Slot type | Sold, bonus, non-guaranteed bonus ("ng bonus"), filler | Vault slot types |
| Which artwork | the creatives booked on the player, found in SharePoint by file name, with their weighting (% of the campaign's plays) and day-parting | the Material Instructions |
| Day or night | the player's GPS location | Auckland unless `POP_SITE_COORDS` is set |
| Rotation (slot count and length) | the frame's loop policy: the site's rotation is kept the same automatically | set by hand |
| Player status | online or offline, last seen; a Teams/Slack alert after 15 minutes offline | — |

A campaign booked only as a **non-guaranteed bonus** plays when the loop has room, so it
shows as *bonus if room* and isn't counted in true coverage (like Vault's "maybe").
Each campaign row links to its Doohly booking(s).

Screens without a Doohly player still use the Vault pull, and the SharePoint Material
Instructions are still used when Doohly's file names aren't found in the campaign folder.

## Setting up

1. Put the token in `.env` (Docker Desktop: `deploy/desktop/.env`; Portainer: the stack's
   environment variables):
   ```
   DOOHLY_API_TOKEN=...
   ```
2. Rebuild and restart: `docker compose up -d --build`.

Within a minute the Bookings page header says *Bookings from Doohly (…, N bookings on N
screens)*, and each site shows *player online*.

Sites are matched to Doohly players by name: the player `LUMO-Albany Expressway IN` is
the Vault screen `Albany Expressway In`. A site whose player has a different name is
listed in the header as *not found in Doohly*; set `DOOHLY_DEVICES` for it
(`SITE_ID=Doohly player name;SITE_ID=...`). The main frame is the player's largest frame
(not the name plate).

## Setting up sites from Doohly

The **Sites** page lists every Doohly player under *Screens in Doohly* (test players left
out), with its address, screen size, loop and whether it is online. Screens already set up
as a site are hidden (tick *show active ones too* to see them, ticked).

Tick a screen to set it up: the Add site form opens with the site ID (e.g.
`ALBANY-EXPRESSWAY-IN`), name, Vault screen and rotation (slot count and length from the
player's loop) filled in. Add the camera's address and login, save, and set its region as
usual. Nothing is monitored until then: a screen only becomes a site once it has a camera.

A screen counts as set up when a site's Vault screen name is the player's name without
`LUMO-` (ignoring case), so the bookings, artwork and day/night link up by themselves.
Lumo Lite players come through as e.g. `LITE-Chartwell`; change the Vault screen in the
form if Vault names it differently.

The screen sizes and loops are read a few players at a time (Doohly takes a few seconds
per player), so after the first start the list fills in over about half an hour; until then
those columns show `?`, and the form leaves the rotation blank.

## Rotation from Doohly

Each site's rotation (slots in the loop and slot length) follows its Doohly player's loop
policy: within 5 minutes of a difference, the receiver changes the site's settings on the
gateway (which restarts that camera's stream) and logs `rotation: <site> set to 20 x 8 s
from Doohly`. The Sites page marks those sites *· Doohly*, and the site's Settings page shows
the Doohly loop and the last change.

Untick **Keep in step with Doohly's loop** on a site's Settings page to set its rotation by
hand; the Bookings page then warns if it differs from Doohly. Takeover sites and sites with
time-of-day rotations (from the config file) are always left alone.

## When it runs

* Every 5 minutes (`DOOHLY_DEVICE_MIN`): the players (online, last seen, location, frames
  and loop policy).
* Every 30 minutes (`DOOHLY_INTERVAL_MIN`): their bookings for this week and next. Only
  new or changed bookings are read in full. When they change, the artwork sync runs
  straight away.

It writes `doohly.json` into the artwork volume, which the receiver reads.

## What Doohly's API can't do (yet)

* **Creative files can't be downloaded** (Doohly's file links need signed URLs), so the
  images still come from SharePoint, matched by name. In the first run, all 63 files booked
  on the three trial screens were found by their exact name. HTML creatives (live
  content, e.g. a weather ad) can't be matched.
* **No play counts.** The Reporting tab's plays per day aren't in the public API.
* **The token can change bookings.** The API also creates and deletes campaigns and
  bookings, so keep the token private. PoP Monitor never calls those endpoints.

Questions for Doohly: signed creative URLs or a download endpoint, a play-count endpoint,
a read-only token, rate limits, and what a booking's External ID is (a Vault slot?).
