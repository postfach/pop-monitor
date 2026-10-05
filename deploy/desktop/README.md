# Running PoP Monitor on Docker Desktop

For the field trial on a Windows or Mac PC. The PC must stay on and awake while it
monitors (turn off sleep).

1. Install Docker Desktop and start it.
2. Put the project somewhere simple, e.g. `C:\pop-monitor`, and open a terminal in
   `C:\pop-monitor\deploy\desktop`.
3. Create your settings:
   ```
   copy config.example.json config.json
   copy .env.example .env
   ```
   (`cp` on Mac.) Edit `.env` (secrets) and `config.json` (cameras: `host`, `roi`, `profile`).
4. Build and start (the first build takes 5–10 minutes and runs the unit tests):
   ```
   docker compose up -d --build
   ```
5. Open http://localhost:8080 to see PoP images, alerts and camera status.

| To | Do |
| --- | --- |
| See logs | Docker Desktop > Containers > pop-monitor > gateway, or `docker compose logs -f gateway` |
| Change cameras or settings | Edit `config.json`, then `docker compose restart gateway` |
| Check config and camera URLs | `docker compose run --rm gateway --check` (passwords masked) |
| Test a camera's video | `docker compose exec gateway ffprobe -rtsp_transport tcp "rtsp://pop-monitor:PASSWORD@CAMERA-IP:RTSP-PORT/axis-media/media.amp"` |
| Record 10 min for tuning | `docker compose exec gateway ffmpeg -rtsp_transport tcp -i "rtsp://pop-monitor:PASSWORD@CAMERA-IP/axis-media/media.amp?resolution=1280x720&fps=10" -t 600 -c copy /recordings/site1.mp4` (lands in `recordings\`) |
| Replay a recording | `docker compose exec gateway popreplay /recordings/site1.mp4 --roi 0.2,0.2 0.8,0.2 0.8,0.8 0.2,0.8 --slots 6 --slot-ms 10000` |
| Update after new code | `docker compose up -d --build` |
| Stop | `docker compose down` (PoP data is kept in Docker volumes) |
