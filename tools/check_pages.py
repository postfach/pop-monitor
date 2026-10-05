"""Load every review page in a headless browser and fail on any script error.

  pip install playwright   (Chromium must be installed)
  python3 tools/check_pages.py http://localhost:8080 SITE_ID
"""
import sys
from playwright.sync_api import sync_playwright

base = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
site = sys.argv[2] if len(sys.argv) > 2 else ""
pages = ["/", "/sites", "/site", "/sites/import", "/health", "/bookings"]
if site:
    pages += [f"/?site={site}", f"/site?site={site}", f"/site?copy={site}", f"/roi?site={site}"]
failed = 0
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    for path in pages:
        errors.clear()
        page.goto(base + path)
        page.wait_for_timeout(2000)
        print(f"{'FAIL' if errors else 'ok  '} {path}" + (f": {errors}" if errors else ""))
        failed += bool(errors)
    browser.close()
sys.exit(1 if failed else 0)
