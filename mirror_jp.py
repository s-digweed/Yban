#!/usr/bin/env python3
"""
JP-IPTV mirror.

Pulls reaperc's Japanese playlists from GitFlic and the merged EPG from
GitLab Pages, and commits them here so players only ever talk to
raw.githubusercontent.com.

A fetch is only written to disk if it validates. If upstream is down or
returns junk, the previously committed copy is left untouched, so the
mirror degrades to "last known good" instead of breaking.
"""

import gzip
import io
import os
import sys

import requests

GITFLIC_RAW = "https://gitflic.ru/project/reaperc/jp-iptv/blob/raw?file={}"
EPG_URL = "https://jp-epg-26f0ce.gitlab.io/k3v9qm/guide.xml.gz"
EPG_OUT = "jp_epg.xml.gz"

PLAYLISTS = [
    "JP.m3u",
    "JP_Categories.m3u",
    "JP_NOVPN.m3u",
    "JP_NOVPN_Categories.m3u",
    "JP_Safe.m3u",
    "JP_Safe_Categories.m3u",
    "JP_Backup.m3u",
    "JP_Categories_Backup.m3u",
    "JP_Auto.m3u",
]

# GitFlic blocks bots, so present as a browser
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 45


def valid_m3u(text: str) -> tuple[bool, str]:
    if not text.lstrip().startswith("#EXTM3U"):
        return False, "missing #EXTM3U header"
    n = text.count("#EXTINF")
    if n < 10:
        return False, f"only {n} channels, looks truncated"
    return True, f"{n} channels"


def valid_epg(raw: bytes) -> tuple[bool, str]:
    if raw[:2] != b"\x1f\x8b":
        return False, "not gzip"
    try:
        xml = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    except OSError as exc:
        return False, f"bad gzip ({exc})"
    if b"<tv" not in xml[:4000]:
        return False, "no <tv> root element"
    return True, f"{xml.count(b'<programme')} programmes, {len(raw)//1024} KB gz"


def fetch(url: str) -> bytes:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content


def main() -> None:
    ok = skipped = 0

    for name in PLAYLISTS:
        url = GITFLIC_RAW.format(name)
        try:
            raw = fetch(url)
            text = raw.decode("utf-8", errors="replace")
            good, note = valid_m3u(text)
            if not good:
                print(f"  SKIP {name}: {note} - keeping previous copy")
                skipped += 1
                continue
            with open(name, "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"  OK   {name}: {note}")
            ok += 1
        except requests.RequestException as exc:
            have = " (keeping previous copy)" if os.path.exists(name) else " (NO local copy!)"
            print(f"  FAIL {name}: {exc}{have}")
            skipped += 1

    # EPG
    try:
        raw = fetch(EPG_URL)
        good, note = valid_epg(raw)
        if good:
            with open(EPG_OUT, "wb") as fh:
                fh.write(raw)
            print(f"  OK   {EPG_OUT}: {note}")
            ok += 1
        else:
            print(f"  SKIP {EPG_OUT}: {note} - keeping previous copy")
            skipped += 1
    except requests.RequestException as exc:
        have = " (keeping previous copy)" if os.path.exists(EPG_OUT) else " (NO local copy!)"
        print(f"  FAIL {EPG_OUT}: {exc}{have}")
        skipped += 1

    print(f"\nupdated {ok}, skipped {skipped}")

    # Only hard-fail if we got nothing at all AND have no history to fall back on
    if ok == 0 and not any(os.path.exists(p) for p in PLAYLISTS):
        sys.exit("ERROR: nothing fetched and no local copies exist")


if __name__ == "__main__":
    main()
