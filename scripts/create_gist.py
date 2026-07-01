#!/usr/bin/env python3
"""One-off helper: create the secret Gist that stores India Fear & Greed history.

Usage:
    GITHUB_GIST_TOKEN=<pat-with-gist-scope> python scripts/create_gist.py

Prints the new gist id. Set it as GITHUB_GIST_ID in your environment / Render.
Re-running creates another gist — you only need to do this once.
"""

import json
import os
import sys
import urllib.error
import urllib.request

GIST_FILENAME = "india_fear_greed_history.json"


def main() -> int:
    token = os.environ.get("GITHUB_GIST_TOKEN")
    if not token:
        print("ERROR: set GITHUB_GIST_TOKEN (a PAT with the 'gist' scope).", file=sys.stderr)
        return 1

    payload = {
        "description": "India Fear & Greed Index — rolling 30-day history",
        "public": False,
        "files": {
            GIST_FILENAME: {"content": json.dumps({"history": []}, indent=2)}
        },
    }
    req = urllib.request.Request(
        "https://api.github.com/gists",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "fear-greed-api",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        print(f"ERROR: GitHub returned {exc.code}: {exc.read().decode()[:300]}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"ERROR: could not reach GitHub: {exc}", file=sys.stderr)
        return 1

    gist_id = data.get("id")
    print("Gist created.")
    print(f"  GITHUB_GIST_ID={gist_id}")
    print(f"  URL: {data.get('html_url')}")
    print("\nAdd GITHUB_GIST_ID (and your token) to Render's environment variables.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
