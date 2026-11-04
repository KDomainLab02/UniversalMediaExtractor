# -*- coding: utf-8 -*-
"""
One-off helper: export browser cookies to a local file.

Usage: python export_cookies.py edge|chrome|firefox

After exporting, pick the same browser in the web UI and this file is used
automatically, so you no longer need to close the browser.
"""
import os
import sys

from yt_dlp import YoutubeDL

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("edge", "chrome", "firefox"):
        print("Usage: python export_cookies.py edge|chrome|firefox")
        return 1
    browser = sys.argv[1]
    out = os.path.join(BASE_DIR, f"cookies_{browser}.txt")
    print(f"Reading cookies from {browser} "
          f"(make sure the browser is fully closed, including tray background)...")
    try:
        with YoutubeDL({"cookiesfrombrowser": (browser, None, None, None),
                        "quiet": True, "no_warnings": True}) as ydl:
            ydl.cookiejar.save(out, ignore_discard=True, ignore_expires=True)
    except Exception as e:
        print(f"Export failed: {e}")
        low = str(e).lower()
        if "could not copy" in low or "cookie database" in low:
            print("Reason: the browser is running and its cookie database is locked.")
            print("Fix: quit the browser completely (including the tray icon) and retry.")
        return 1
    print(f"Exported: {out}")
    print("Done. Select this browser in the web UI and the file is used automatically.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
