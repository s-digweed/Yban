#!/usr/bin/env python3
"""
tests_autoheal.py — regression tests for autoheal.py.

Run this after ANY change to autoheal.py, BEFORE deploying:

    python3 tests_autoheal.py

Every test here exists because the bug it guards actually happened in production.
Stdlib only, no network — safe to run anywhere.
"""

import socket
import sys
import urllib.error
import urllib.request

import autoheal as A

FAILURES = []
REAL_CHECK = A.check          # several tests stub A.check; restore it afterwards
REAL_PROBE = A._probe


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}"
          + ("" if ok else f"   (got {got!r}, want {want!r})"))
    if not ok:
        FAILURES.append(label)


def section(name):
    print(f"\n== {name} ==")


# --- helpers ---------------------------------------------------------------

class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, body=b""):
        self.code = code
        self._body = body

    def read(self, n=None):
        return self._body[:n] if n else self._body


def with_probe(fn):
    """Temporarily replace A._probe."""
    orig = A._probe
    A._probe = fn
    return orig


def with_urlopen(fn):
    orig = urllib.request.urlopen
    urllib.request.urlopen = fn
    return orig


# --- 1. dead vs transient classification ----------------------------------
# Bug: 403/429 were treated as dead -> ~22 pointless swaps an hour.
# Bug: haru's 403 "Missing params" IS dead and was being ignored.

def test_classification():
    section("check(): dead vs transient")
    orig = urllib.request.urlopen
    A.THROTTLE = 0            # don't sleep during tests
    A.TRIES = 1
    cases = [
        ("404 Not Found",              FakeHTTPError(404, b"Not Found"),              "dead"),
        ("410 Gone",                   FakeHTTPError(410, b""),                       "dead"),
        ("403 Missing params (haru)",  FakeHTTPError(403, b"Missing params | PR:lct-str"), "dead"),
        ("403 generic (transient)",    FakeHTTPError(403, b"Forbidden"),              "unknown"),
        ("429 Channel limit",          FakeHTTPError(429, b"Channel limit has been reached"), "unknown"),
        ("522 Cloudflare",             FakeHTTPError(522, b"<html>error</html>"),     "unknown"),
        ("503 not available",          FakeHTTPError(503, b"This channel is not available"), "dead"),
    ]
    for label, exc, want in cases:
        def raiser(req, timeout=0, _e=exc):
            raise _e
        urllib.request.urlopen = raiser
        check(label, A.check("http://x"), want)

    def refused(req, timeout=0):
        raise urllib.error.URLError(ConnectionRefusedError())
    urllib.request.urlopen = refused
    check("connection refused", A.check("http://x"), "dead")

    def dns(req, timeout=0):
        raise urllib.error.URLError(socket.gaierror())
    urllib.request.urlopen = dns
    check("DNS failure", A.check("http://x"), "dead")

    def timeout_(req, timeout=0):
        raise urllib.error.URLError(socket.timeout())
    urllib.request.urlopen = timeout_
    check("timeout is transient", A.check("http://x"), "unknown")

    urllib.request.urlopen = orig


# --- 2. retries ------------------------------------------------------------
# Bug: a single probe caught 4 recovered channels mid-flap -> stayed parked.

def test_retries():
    section("check(): retries ride out flapping")
    A.THROTTLE = 0
    A.TRIES = 3
    seq = ["unknown", "unknown", "alive"]
    orig = with_probe(lambda u: seq.pop(0))
    check("flap 522,522,200 -> alive", A.check("x"), "alive")
    A._probe = lambda u: "unknown"
    check("always busy -> unknown", A.check("x"), "unknown")
    A._probe = lambda u: "dead"
    check("always dead -> dead", A.check("x"), "dead")
    A._probe = orig


# --- 3. working links are never churned -----------------------------------

def test_no_churn():
    section("decide_url(): never swap a working link")
    A.THROTTLE = 0
    cur = A.x58_url("cs10")
    naori = A.naori_url("cs10")
    orig = with_probe(lambda u: "alive")

    A.check = lambda u: "alive"
    url, kind, _ = A.decide_url("t", "X", cur, {}, {})
    check("both alive -> keep current (no heal)", kind, None)

    A.check = lambda u: "unknown" if u == cur else "alive"
    url, kind, _ = A.decide_url("t", "X", cur, {}, {})
    check("current 429 -> keep current", url, cur)

    A.check = lambda u: "dead" if u == cur else "alive"
    url, kind, _ = A.decide_url("t", "X", cur, {}, {})
    check("current dead -> heal to alive candidate", kind, "heal")
    A._probe = orig


# --- 4. snap-back hysteresis ----------------------------------------------

def test_snapback():
    section("decide_url(): snap-back needs 2 confirmations")
    A.THROTTLE = 0
    A.CONFIRM = 2
    cur = A.x58_url("cs10")          # rank 3
    better = A.naori_url("cs10")     # rank 1
    A.check = lambda u: "alive"
    st = {}
    _, k1, _ = A.decide_url("t", "X", cur, {}, st)
    check("run 1: no move yet", k1, None)
    _, k2, _ = A.decide_url("t", "X", cur, {}, st)
    check("run 2: snapback", k2, "snapback")

    st = {}
    A.check = lambda u: "alive"
    A.decide_url("t", "X", cur, {}, st)
    A.check = lambda u: "unknown" if u == better else "alive"
    _, k3, _ = A.decide_url("t", "X", cur, {}, st)
    check("blip resets the streak", k3, None)


# --- 4b. pinned links ------------------------------------------------------
# Bug it guards: a hand-picked source (chosen for picture quality) sits on a
# host that HOST_PREF ranks low, so the next snap-back pass "upgraded" it away
# and the manual edit silently vanished.

def test_pinned():
    section("decide_url(): a pinned link is kept, but still heals when dead")
    A.THROTTLE = 0
    A.CONFIRM = 1                      # snapback would fire immediately if unpinned
    tid = "pin-test"
    pin = "http://example.invalid/best_quality"
    better = A.naori_url("cs10")            # a live, high-ranked host
    A.PINNED[tid] = pin
    alt = {A.norm("X"): [better]}      # the channel has a real alternative
    try:
        A.check = lambda u: "alive"
        url, kind, _ = A.decide_url(tid, "X", better, alt, {})
        check("moves onto the pin", (url, kind), (pin, "pin"))

        url, kind, _ = A.decide_url(tid, "X", pin, alt, {})
        check("already pinned: no churn", (url, kind), (pin, None))

        # pin down -> must NOT strand the channel, heals like normal
        A.check = lambda u: "dead" if u == pin else "alive"
        url, kind, _ = A.decide_url(tid, "X", pin, alt, {})
        check("pin dead: heals away", kind, "heal")
        check("pin dead: picks a live source", url, better)

        # an unpinned channel must be completely unaffected
        A.check = lambda u: "alive"
        url, kind, _ = A.decide_url("other", "Y", A.x58_url("cs10"), {}, {})
        check("unpinned still snaps back", kind, "snapback")
    finally:
        A.PINNED.pop(tid, None)
        A.CONFIRM = 2


# --- 4c. skipped hosts -----------------------------------------------------
# SKIP_HOSTS and DEAD_HOSTS are opposites and must stay that way:
#   SKIP = "we cannot see this host, leave its channels exactly alone"
#   DEAD = "this host is gone, migrate every channel off it"
# Using the wrong one is expensive in both directions. Skipping a genuinely
# dead host strands every channel on it; declaring a live-but-unreachable host
# dead migrates 90+ channels onto worse sources for no reason.
#
# This was real: akariko-bck1 went behind Cloudflare (skip, Aug 5) and then
# actually died hours later (dead, Aug 5).

def test_skip_hosts():
    section("SKIP_HOSTS: never probed, never changed, never dropped")
    A.THROTTLE = 0
    # deliberately a host that is NOT in DEAD_HOSTS: the point of this test is
    # that skipping and declaring-dead are opposite behaviours
    ak = "https://skip-test.example/stream/jp/x/stream-output.m3u8?mode=hls"
    A.SKIP_HOSTS.add("skip-test.example")

    probed = []
    A._probe = lambda u: (probed.append(u), "dead")[1]
    check("skipped host returns unknown", A.check(ak), "unknown")
    check("and makes NO request", len(probed), 0)

    probed.clear()
    check("a normal host is still probed", A.check(A.x58_url("cs06")), "dead")
    check("which did make a request", len(probed) > 0, True)

    # the channel must be left exactly as-is
    A.check = lambda u: "unknown" if "akariko" in u else "alive"
    url, kind, _ = A.decide_url("t", "X", ak, {}, {})
    check("skipped channel: no change", (url, kind), (ak, None))

    # and crucially it must NOT be filtered out of the candidate list
    cands = A.candidates("t", "X", ak, {})
    check("skipped host stays a candidate", ak in cands, True)
    A.SKIP_HOSTS.discard("skip-test.example")
    check("SKIP_HOSTS is not DEAD_HOSTS",
          any("skip-test.example" in d for d in A.DEAD_HOSTS), False)


# --- 5. the bs11 trap ------------------------------------------------------
# Bug: BS11 was playing NHK BS. cid 'bs11' == NHK BS; slug 'bs11' == BS11.

def test_bs11_trap():
    section("candidates(): BS11 must never take a bs11 *cid*")
    alt = {A.norm("BS11"): [A.x58_url("bs11"), A.naori_url("bs11")]}
    c = A.candidates("BS11-イレブン_jp", "BS11",
                     "http://haru.charandom.blog/stream/jp/bs11/stream-output.m3u8?mode=hls",
                     alt)
    bad = [u for u in c if "shk_cid=bs11" in u or "gid=bs11" in u or "/bs11.m3u8" in u]
    check("no cid-based bs11 in candidates", bad, [])
    check("slug-based bs11 still allowed", any("/stream/jp/bs11/" in u for u in c), True)


# --- 6. sub-channels ------------------------------------------------------
# Bug (twice): NHK G (Sub Ch.) played NHK G — via shared tvg-id, then via name match.

def test_subchannels():
    section("sub-channels must never inherit the parent stream")
    check("norm() distinguishes sub from parent",
          A.norm("NHK G") != A.norm("NHK G (Sub Ch.)"), True)
    check("same for NHK BS",
          A.norm("NHK BS") != A.norm("NHK BS (Sub Ch.)"), True)

    alt = {A.norm("NHK G"): [A.naori_url("gd01"),
                             A.akariko_bck_url("nhk_g")]}
    c = A.candidates("NHK東京・総合2_jp", "NHK G (Sub Ch.)",
                     "http://haru.charandom.blog/stream/jp/nhk_g_(subch)/stream-output.m3u8?mode=hls",
                     alt)
    bad = [u for u in c if "gd01" in u or "/stream/jp/nhk_g/" in u]
    check("parent stream excluded from sub-ch candidates", bad, [])

    alt2 = {A.norm("NHK BS"): [A.naori_url("bs11")]}
    c2 = A.candidates("NHK・BS2_jp", "NHK BS (Sub Ch.)",
                      "http://haru.charandom.blog/stream/jp/nhk_bs_(subch)/stream-output.m3u8?mode=hls",
                      alt2)
    check("NHK BS parent excluded too",
          [u for u in c2 if "bs11" in u or "/stream/jp/nhk_bs/" in u], [])


# --- 7. host preference ----------------------------------------------------

def test_ranking():
    section("host preference order")
    order = [
        ("akariko-bck1", A.akariko_bck_url("nhk_g"), 0),
        ("naori",        A.naori_url("cs06"),        1),
        ("58.x",         A.x58_url("cs06"),          2),
        ("primehome",    A.primehome_url("cs06"),    3),
    ]
    for name, url, want in order:
        check(f"{name} rank", A.rank(url), want)
    check("unknown host ranks last", A.rank("https://example.com/x.m3u8"), len(A.HOST_PREF))

    # haru locked every stream behind an access code (Aug 2026) and was dropped
    # from HOST_PREF. It must never out-rank a host that actually works, or a
    # healthy channel gets "upgraded" onto a link nobody can play.
    haru = "http://haru.charandom.blog/stream/jp/x/stream-output.m3u8"
    check("haru ranks last, not 2", A.rank(haru), len(A.HOST_PREF))
    for name, url, _ in order:
        check(f"haru never beats {name}", A.rank(haru) > A.rank(url), True)


# --- 8. dead hosts are never used -----------------------------------------

def test_dead_hosts():
    section("dead hosts excluded")
    alt = {A.norm("X"): ["http://akariko.netgenx.site/stream/jp/x/stream-output.m3u8"]}
    c = A.candidates("t", "X", A.naori_url("cs06"), alt)
    check("akariko excluded", [u for u in c if "akariko.netgenx.site" in u], [])


# --- 9. park / unpark round-trip ------------------------------------------
# Requirement: parking must be lossless.

def test_park_roundtrip():
    section("park/unpark is lossless")
    sample = "\n".join([
        '#EXTM3U url-tvg="x"',
        "",
        '#EXTINF:-1 tvg-id="A" tvg-name="Alpha",Alpha',
        "http://host/a.m3u8",
        "",
        '#EXTINF:-1 tvg-id="B" tvg-name="Beta",Beta',
        "http://host/b.m3u8",
        "",
    ])
    lines = sample.split("\n")
    np_, nu = A.park_unpark(lines, {"http://host/a.m3u8"}, {})
    check("parked one", np_, 1)
    check("Alpha hidden", sum(1 for l in lines if l.startswith("#EXTINF")), 1)
    np2, nu2 = A.park_unpark(lines, set(), {"http://host/a.m3u8": "http://host/a.m3u8"})
    check("unparked one", nu2, 1)
    check("round-trip identical", "\n".join(lines), sample)

    # unpark onto a DIFFERENT host (how channels returned via akariko-bck1)
    lines = sample.split("\n")
    A.park_unpark(lines, {"http://host/a.m3u8"}, {})
    A.park_unpark(lines, set(), {"http://host/a.m3u8": "http://new/a.m3u8"})
    check("restored with new url", "http://new/a.m3u8" in lines, True)
    check("no leftover park marks", any(l.startswith(A.PARK_MARK) for l in lines), False)


# --- 10. per-host circuit breaker -----------------------------------------
# Bug: 95 channels x 3 retries hammered akariko-bck1 into 403ing everything.

def test_circuit_breaker():
    section("circuit breaker stops hammering a refusing host")
    A.THROTTLE = 0
    A.TRIES = 3
    calls = {"n": 0}

    def probe(u):
        calls["n"] += 1
        if "blocked.example" in u:
            A._last_code = 403
            return "unknown"
        A._last_code = 200
        return "alive"

    orig = with_probe(probe)
    A.reset_breakers()
    for i in range(30):
        A.check(f"https://blocked.example/{i}.m3u8")
    check("30 checks -> capped requests", calls["n"] <= A.HOST_FAIL_LIMIT, True)
    check("no retry storm on 403", calls["n"], A.HOST_FAIL_LIMIT)

    calls["n"] = 0
    for i in range(10):
        A.check(f"https://good.example/{i}.m3u8")
    check("healthy host unaffected", calls["n"], 10)

    A.reset_breakers()
    A._host_fails["x.example"] = 5
    A._probe = lambda u: (setattr(A, "_last_code", 200), "alive")[1]
    A.check("https://x.example/1.m3u8")
    check("alive reply resets the counter", A._host_fails["x.example"], 0)

    A.reset_breakers()
    check("reset_breakers clears state", A._host_fails, {})
    A._probe = orig


# --- 11. per-host scan cooldown + rotating batch --------------------------
# Added after akariko-bck1 replied "Automated scanning is not permitted" to
# 48 full sweeps a day. Keeps our footprint on a host small and predictable.

def test_scan_pacing():
    section("per-host pacing / cooldown / rotating batch")
    A.FORCE_SCAN = False
    A._last_scan.clear()
    A._scan_now.clear()

    ak = A.akariko_bck_url("nhk_g")
    check("akariko paced slower than default",
          A.throttle_for(ak) > A.throttle_for("http://haru.charandom.blog/x"), True)

    check("first visit allowed", A.may_scan(ak), True)
    A._scan_now.clear()
    check("second visit inside cooldown blocked", A.may_scan(ak), False)

    A.FORCE_SCAN = True
    check("--now overrides cooldown", A.may_scan(ak), True)
    A.FORCE_SCAN = False

    A._scan_now.clear()
    A._last_scan["akariko-bck1.sankuria.sbs"] = 0        # long ago
    check("due again after the interval", A.may_scan(ak), True)

    # rotating batch covers everything without repeating within a cycle
    urls = [f"https://akariko-bck1.sankuria.sbs/{i}" for i in range(99)]
    A._last_scan.pop("batch:akariko-bck1.sankuria.sbs", None)
    n = A.HOST_SCAN_BATCH["akariko-bck1.sankuria.sbs"]
    # however small the batch is, walking ceil(total/batch) sweeps must visit
    # every channel exactly once round. Derived rather than hardcoded, so
    # retuning the batch for a rate limit doesn't silently break the invariant.
    sweeps = -(-99 // n)
    seen, batches = set(), []
    for _ in range(sweeps):
        b = A.batch_slice("akariko-bck1.sankuria.sbs", urls)
        batches.append(b)
        seen |= b
    check("batch size capped", len(batches[0]), n)
    # sweeps advance through the list; a small wrap-around repeat is fine when the
    # batch size doesn't divide the channel count evenly
    check("consecutive sweeps barely overlap",
          len(batches[0] & batches[1]) <= max(1, (n * 2) - 99), True)
    check(f"full coverage in {sweeps} sweeps", len(seen), 99)

    A._last_scan.clear()
    A._scan_now.clear()


# --- 12. live playlists are structurally sound ----------------------------

def test_lint():
    section("live playlists lint clean")
    problems = A.lint_playlists()
    for p in problems:
        print("     !!", p)
    check("no structural problems", problems, [])


def main():
    for t in (test_classification, test_retries, test_no_churn, test_snapback,
              test_pinned, test_skip_hosts,
              test_bs11_trap, test_subchannels, test_ranking, test_dead_hosts,
              test_park_roundtrip, test_circuit_breaker, test_scan_pacing,
              test_lint):
        A.check, A._probe = REAL_CHECK, REAL_PROBE   # isolate: undo prior stubbing
        A.reset_breakers()
        try:
            t()
        except Exception as exc:                      # noqa: BLE001
            print(f"  ERROR in {t.__name__}: {exc}")
            FAILURES.append(t.__name__)
    A.check, A._probe = REAL_CHECK, REAL_PROBE

    print("\n" + ("-" * 50))
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): " + ", ".join(FAILURES))
        return 1
    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
