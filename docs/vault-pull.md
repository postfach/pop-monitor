# Vault bookings pull

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
  to which campaign, aren't in Vault. Each creative is assigned on the Bookings page by hand
  (a campaign, Programmatic or House); nothing is guessed. Doohly's playlist would give both.

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
