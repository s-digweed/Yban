#!/usr/bin/env python3
"""Fetch the merged JP EPG. Keeps the previous copy if upstream misbehaves."""
import gzip, io, os, sys, urllib.request

URL = "https://jp-epg-26f0ce.gitlab.io/k3v9qm/guide.xml.gz"
OUT = "yban_epg.xml.gz"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"

try:
    req = urllib.request.Request(URL, headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=60).read()
except Exception as exc:
    print(f"EPG fetch failed: {exc}")
    sys.exit(0 if os.path.exists(OUT) else 1)

if raw[:2] != b"\x1f\x8b":
    print("EPG not gzip - keeping previous copy"); sys.exit(0)
try:
    xml = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
except OSError as exc:
    print(f"EPG bad gzip ({exc}) - keeping previous copy"); sys.exit(0)
if b"<tv" not in xml[:4000]:
    print("EPG has no <tv> root - keeping previous copy"); sys.exit(0)

open(OUT, "wb").write(raw)
print(f"EPG OK: {xml.count(b'<programme')} programmes, {len(raw)//1024} KB gz")
