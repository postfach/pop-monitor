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
| Rotation check | the frame's loop policy: a warning when the site's slot count or length differs | — |
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
