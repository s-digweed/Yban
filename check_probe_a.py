#!/usr/bin/env python3
"""
check_probe_a.py — hand-paced probe of one host's channels.

    python3 check_probe_a.py --limit 5           # quick look
    python3 check_probe_a.py                     # full pass
    python3 check_probe_a.py --interval 90       # slower still
    python3 check_probe_a.py --ua vlc            # compare user-agents
    python3 check_probe_a.py --host 58.82.168.138

Deliberately slow: ONE request per minute by default, which is slower than a
person flipping channels. Some hosts answer anything faster with a block page,
and those blocks are usually temporary and triggered by the request pattern
itself. So the tool's job is to stay under that threshold and show you exactly
where and how a host starts refusing.

This is a manual instrument, not a background job. Run it, watch it, stop it.
autoheal deliberately does not probe hosts listed in its SKIP_HOSTS; this is how
you check them by hand instead.

The user-agent defaults to a common player, because that is what viewers
actually use and it is their experience we care about. --ua vlc sends what
autoheal sends, which tells you whether the two are treated differently.

Nothing is written and no playlist is modified. Ctrl-C prints a partial summary.

Stdlib only.
"""

import argparse
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent
PLAYLIST = REPO / "JP.m3u"

# default target: the host carrying most of the list, and the one currently in
# autoheal's SKIP_HOSTS
DEFAULT_HOST = "akariko-bck1.sankuria.sbs"

UAS = {
    "tivimate": "TiviMate/4.7.0 (Linux; Android 11)",
    "vlc": "VLC/3.0.20 LibVLC/3.0.20",
    "ott": "OTT Navigator/1.6.9.5 (Linux; Android 12)",
    "exo": "ExoPlayerLib/2.18.1",
    "kodi": "Kodi/20.2 (Linux; Android 11) InputStream.Adaptive",
}


def channels(host, path=None):
    """[(name, url)] for every channel on `host` in the playlist, in order."""
    out, name = [], None
    src = Path(path) if path else PLAYLIST
    for line in src.read_text(encoding="utf-8", errors="replace").split("\n"):
        s = line.strip()
        s = s[len("#PARKED#"):] if s.startswith("#PARKED#") else s
        if s.startswith("#EXTINF"):
            name = s.rsplit(",", 1)[-1].strip()
        elif s.startswith("http") and name:
            if host in s:
                out.append((name, s))
            name = None
    return out


def probe(url, ua, timeout):
    """-> (label, http_code_or_None, detail). Never raises."""
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = getattr(r, "status", 200)
            body = r.read(256)
            if code == 200 and body:
                return "ALIVE", code, f"{len(body)}+ bytes"
            return "EMPTY", code, "no data"
    except urllib.error.HTTPError as e:
        try:
            # read well past the first KB: edge block pages put the error code
            # (1015 rate-limit vs 1020 firewall rule) far down the HTML, and
            # that distinction decides whether waiting will help at all
            body = e.read(20000).decode("utf-8", "ignore")
        except Exception:                                  # noqa: BLE001
            body = ""
        low = body.lower()
        # Cloudflare brands its 5xx pages too. A 502/500/503 wearing Cloudflare
        # styling means the EDGE reached the ORIGIN and the origin failed — the
        # opposite of being blocked. Only treat 403/429 block pages as a block.
        if e.code >= 500:
            return "ORIGIN-ERR", e.code, "origin failed behind the CDN"
        if "attention required" in low or "cloudflare" in low:
            m = (re.search(r"error code[: ]+(\d+)", low)
                 or re.search(r"\berror\s+(10\d\d)\b", low)
                 or re.search(r"cf-error-code[^0-9]*(\d+)", low))
            ray = re.search(r"ray id[: ]+([a-f0-9]{10,})", low)
            detail = f"error {m.group(1)}" if m else "block page"
            if ray:
                detail += f" ray {ray.group(1)[:12]}"
            return "EDGE-BLOCK", e.code, detail
        if "automated scanning" in low:
            return "SCAN-BLOCK", e.code, "automated scanning not permitted"
        if "channel limit" in low:
            return "BUSY", e.code, "channel limit reached"
        if "missing params" in low:
            return "GATED", e.code, "missing params"
        snippet = " ".join(body.split())[:60]
        return "HTTP", e.code, snippet or "no body"
    except urllib.error.URLError as e:
        if isinstance(e.reason, (ConnectionRefusedError, socket.gaierror)):
            return "DEAD", None, str(e.reason)
        return "TIMEOUT", None, str(e.reason)[:60]
    except Exception as e:                                 # noqa: BLE001
        return "TIMEOUT", None, str(e)[:60]


def fetch_text(url, hdrs, timeout, limit=8000):
    """-> (body, final_url_after_redirects).

    The final URL matters. Xtream endpoints redirect /live/<u>/<p>/<id>.m3u8 to
    the real stream path, and the segment names inside the playlist are relative
    to THAT, not to the URL we asked for. Resolving them against the original
    gives paths that 403 or 404 while the channel plays perfectly in a player.
    """
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit), r.geturl()


def looks_like_ts(data):
    """True if this is raw MPEG-TS rather than a playlist.

    Xtream panels commonly serve /live/<u>/<p>/<id>.m3u8 as a plain TS stream
    instead of HLS. Those channels play perfectly, so treating "no #EXTM3U" as
    a failure marks working channels dead. TS is self-identifying: 188-byte
    packets each starting with the sync byte 0x47.
    """
    if len(data) < 188 * 3:
        return False
    return all(data[i] == 0x47 for i in (0, 188, 376))


def deep_probe(url, ua, timeout, seg_delay=0.0):
    """Follow the manifest to a real video segment.

    A 200 on the .m3u8 only means something answered. Xtream panels in
    particular serve a well-formed playlist for channels whose segments are
    long dead, so the shallow check reports them alive and the player shows
    nothing. This resolves the playlist (master -> variant -> segment) and
    fetches the first segment; only actual bytes count as working.
    """
    hdrs = {"User-Agent": ua}
    try:
        raw, cur = fetch_text(url, hdrs, timeout)    # cur = post-redirect URL
    except urllib.error.HTTPError as e:
        return ("ORIGIN-ERR" if e.code >= 500 else "HTTP"), e.code, f"manifest {e.code}"
    except Exception:                                   # noqa: BLE001
        return "TIMEOUT", None, "manifest unreachable"

    # a raw transport stream IS a working channel, just not HLS
    if looks_like_ts(raw):
        return "ALIVE", 200, f"raw MPEG-TS, {len(raw)}+b"

    text = raw.decode("utf-8", "ignore")
    if "#EXTM3U" not in text:
        head = " ".join(text.split())[:50]
        return "NOT-HLS", 200, f"{len(raw)}b, neither playlist nor TS: {head}"

    def first_uri(body):
        return next((l.strip() for l in body.splitlines()
                     if l.strip() and not l.startswith("#")), None)

    # a master playlist points at variants; step down one level
    if "#EXT-X-STREAM-INF" in text:
        v = first_uri(text)
        if not v:
            return "NO-VARIANT", 200, "master playlist with no variants"
        try:
            raw, cur = fetch_text(urllib.parse.urljoin(cur, v), hdrs, timeout)
            text = raw.decode("utf-8", "ignore")
        except Exception:                               # noqa: BLE001
            return "NO-SEGMENT", None, "variant unreachable"

    seg = first_uri(text)
    if not seg:
        return "NO-SEGMENT", 200, "playlist lists no segments"
    seg_url = urllib.parse.urljoin(cur, seg)
    if seg_delay:
        time.sleep(seg_delay)          # let a held connection slot expire
    try:
        req = urllib.request.Request(seg_url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(2048)
        if len(data) < 188:                             # smaller than one TS packet
            return "EMPTY-SEG", 200, f"segment only {len(data)}b"
        return "ALIVE", 200, f"segment {len(data)}+b OK"
    except urllib.error.HTTPError as e:
        # 403 on the SEGMENT after a good manifest is almost never "gone". On an
        # Xtream line the manifest request already opened a stream session and
        # holds a connection slot for several seconds, so the immediate segment
        # request looks like a second concurrent connection and gets refused.
        # Only a 404/410 actually means the video is missing.
        if e.code in (401, 403, 429):
            return "UNVERIFIED", e.code, (f"segment {e.code} — manifest fine; "
                                          "likely a connection-slot limit")
        return "DEAD-SEG", e.code, f"segment {e.code} — manifest served but no video"
    except Exception:                                   # noqa: BLE001
        return "UNVERIFIED", None, "segment unreachable — inconclusive"


def slug_of(url):
    if "/stream/jp/" in url:
        return url.split("/stream/jp/", 1)[-1].split("/")[0]
    return url.rsplit("/", 1)[-1].split("?")[0][:24]


def main():
    # Channel names are Japanese. When stdout is a console Python uses UTF-8,
    # but piping (PowerShell's Tee-Object, > file, | findstr) switches it to the
    # legacy code page, which cannot encode them and crashes mid-run. Force
    # UTF-8 and never let an unencodable character kill a long scan.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--playlist", metavar="FILE",
                    help="read from this m3u instead of JP.m3u — e.g. a file of "
                         "candidate slugs you want to test")
    ap.add_argument("--interval", type=float, default=60.0,
                    help="seconds between requests (default 60)")
    ap.add_argument("--limit", type=int, help="stop after N channels")
    ap.add_argument("--start", type=int, default=0,
                    help="skip the first N (resume where you stopped)")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--ua", default="tivimate",
                    help="tivimate | vlc | ott | exo | kodi, or a literal string")
    ap.add_argument("--match", metavar="TEXT", action="append",
                    help="only channels whose name or slug contains TEXT "
                         "(case-insensitive). Repeatable, and accepts a "
                         "comma-separated list. Use this rather than running the "
                         "script once per channel in a shell loop: --interval "
                         "only paces requests WITHIN a run, so N separate runs "
                         "fire N requests back to back with no gap at all.")
    ap.add_argument("--shuffle", action="store_true",
                    help="check in random order instead of playlist order. "
                         "Useful when you stop a run early: successive partial "
                         "runs then sample the whole list rather than "
                         "re-checking the same first N channels every time.")
    ap.add_argument("--seed", type=int,
                    help="fix the shuffle order so a run can be repeated exactly")
    ap.add_argument("--seg-delay", type=float, default=0.0, metavar="SEC",
                    help="wait this long between the manifest and the segment "
                         "fetch. On an Xtream line the manifest holds a "
                         "connection slot for a few seconds, so 6-8 here avoids "
                         "the segment being refused as a second connection.")
    ap.add_argument("--deep", action="store_true",
                    help="follow the manifest to a real video segment. A 200 on "
                         "the .m3u8 only proves something answered — Xtream "
                         "panels serve valid playlists for channels whose "
                         "segments are dead. Costs 2-3 requests per channel.")
    args = ap.parse_args()

    ua = UAS.get(args.ua.lower(), args.ua)
    chans = channels(args.host, args.playlist)
    if args.match:
        terms = [t.strip().lower()
                 for chunk in args.match for t in chunk.split(",") if t.strip()]
        chans = [c for c in chans
                 if any(t in c[0].lower() or t in slug_of(c[1]).lower()
                        for t in terms)]
        if not chans:
            print(f"Nothing matches {terms} on {args.host}.\n"
                  "Names and slugs available:", file=sys.stderr)
            for n, u in channels(args.host, args.playlist)[:200]:
                print(f"  {n:30s} {slug_of(u)}", file=sys.stderr)
            return 1
    if args.shuffle:
        rng = random.Random(args.seed)
        rng.shuffle(chans)
    chans = chans[args.start:]
    if args.limit:
        chans = chans[:args.limit]
    if not chans:
        print(f"No channels on {args.host} in JP.m3u", file=sys.stderr)
        return 1

    print(f"{len(chans)} channels on {args.host}")
    print(f"user-agent : {ua}")
    if args.shuffle:
        print(f"order      : random"
              + (f" (seed {args.seed})" if args.seed is not None else ""))
    if args.deep:
        print("depth      : following manifests to a real segment")
    print(f"pace       : 1 request / {args.interval:.0f}s  "
          f"-> about {len(chans) * args.interval / 60:.0f} min total")
    print("Ctrl-C to stop; a summary prints either way.\n")

    tally, done = Counter(), 0
    try:
        for i, (name, url) in enumerate(chans, 1):
            if i > 1:
                time.sleep(args.interval)
            t0 = time.time()
            label, code, detail = (deep_probe(url, ua, args.timeout, args.seg_delay)
                                   if args.deep else probe(url, ua, args.timeout))
            tally[label] += 1
            done = i
            print(f"  [{i:3d}/{len(chans)}] {time.strftime('%H:%M:%S')} "
                  f"{label:11s} {str(code or '-'):>4}  {name[:26]:26s} "
                  f"{slug_of(url):24s} {detail} ({time.time()-t0:.1f}s)", flush=True)
    except KeyboardInterrupt:
        print("\n  stopped.")

    print("\n" + "=" * 60)
    print(f"  checked {done} of {len(chans)}")
    for k, v in tally.most_common():
        print(f"    {v:4d}  {k}")
    if tally.get("EDGE-BLOCK"):
        print("\n  Blocked at the edge on some requests. Leave it longer before "
              "trying again;\n  going faster or sending more will not help.")
    if tally.get("HTTP") or tally.get("ORIGIN-ERR"):
        print("\n  404 = the slug is gone from this host (channel really dropped)."
              "\n  5xx = the host reached the origin and it failed; often "
              "transient, and normal\n        for a sub-channel that only "
              "broadcasts during split programming.")
    if tally.get("ALIVE") and done and not tally.get("EDGE-BLOCK"):
        print("\n  Clean at this pace. If a full pass stays clean, this host can "
              "come out of\n  SKIP_HOSTS in autoheal.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
