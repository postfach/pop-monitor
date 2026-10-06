The weekly Vault pull writes vault-bookings.json here.
The booked artwork is pulled from SharePoint by the creatives-sync container into its own
Docker volume (docs/sharepoint-setup.md). A hand-made pull can still go in
creatives/vault-creatives.json + creatives/<screen>/images/... (the newest library wins).
The receiver reads this folder read-only. See docs/vault-pull.md.
