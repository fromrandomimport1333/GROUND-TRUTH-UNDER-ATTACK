"""Download the WegenerNet L2 v8 5-min station zips from the public cloud share.

The share URL is printed on the portal (Data download -> Cloud access -> "WegenerNet
Cloud Archive") after a free login at https://wegenernet.org/portal. It is a public
Nextcloud share; the share token is the basic-auth username for WebDAV. The URL is
secret-ish (do not commit it); pass it via the WEGENERNET_SHARE_URL env var.

usage: WEGENERNET_SHARE_URL=https://cloud.uni-graz.at/s/XXXX uv run python -m data_prep.download
"""

import os
import sys
import urllib.request
from pathlib import Path

BD_FILES = [
    "WN_L2_V8_2024_1_BD_2023-01-01d00h00m_2024-01-01d00h00m_UTC.zip",
    "WN_L2_V8_2025_1_BD_2024-01-01d00h00m_2025-03-01d00h00m_UTC.zip",
]
RAW = Path("data/raw/wegenernet")


def webdav_url(share_url: str, path: str) -> tuple[str, str]:
    """(file URL, basic-auth user) for a path under the 2025/1 release of a Nextcloud share."""
    base, token = share_url.rstrip("/").rsplit("/s/", 1)
    return f"{base}/public.php/webdav/2025/1/{path}", token


def download(share_url: str, dest: Path, files: list[str]) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for name in files:
        url, token = webdav_url(share_url, f"BD/{name}")
        target = dest / name
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, token, "")
        opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(mgr))
        with opener.open(url) as r:
            remote_size = int(r.headers.get("Content-Length", -1))
            if target.exists() and target.stat().st_size == remote_size:
                out.append(target)
                continue
            with open(target, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
        out.append(target)
    return out


def main() -> int:
    share = os.environ.get("WEGENERNET_SHARE_URL")
    if not share:
        print(
            "Set WEGENERNET_SHARE_URL. Get it: register at https://wegenernet.org/portal,\n"
            "then Data download -> Cloud access -> copy the 'WegenerNet Cloud Archive' link.",
            file=sys.stderr,
        )
        return 1
    paths = download(share, RAW, BD_FILES)
    for p in paths:
        print(f"{p}  {p.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
