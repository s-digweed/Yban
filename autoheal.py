#!/usr/bin/env python3
"""
autoheal.py — self-healing links for the JP-IPTV playlists.

A working link is LEFT ALONE. Each run it checks only the channel's current URL;
if that URL is confirmed dead (HTTP 404/410, refused connection, or DNS failure)
it swaps in the first alive replacement, preferring naori-test > haru > 58.x >
anything else. Ambiguous responses (403, 429 rate-limit, 5xx, timeouts) are NOT
treated as dead, so a healthy stream is never churned just because the host was
briefly gating or rate-limiting the checker.

Replacements, when a link IS dead, come from:
  1. the naori <-> 58.x pair, generated from the channel's zhongying id
     (naori pxx.php?shk_cid=<id>  <->  58.82.168.138:5002/<id>.m3u8?...)
  2. any other URL for that channel found in JP_Backup.m3u and JP_Auto.m3u
  3. per-channel EXTRA_CANDIDATES
sorted by preferred host, first CONFIRMED-alive one wins.
Dead hosts (akariko / primehome / converse / livingjtv) are never used.

Runs on YOUR machine (only it can reach naori/haru to test them), like Propho's.
Stream-only: never rewrites tvg-id / tvg-logo / EPG, just the URL line.

    python autoheal.py                 check + heal local files (no push)
    python autoheal.py --check         dry run: report what would change
    python autoheal.py --push          heal AND push to GitFlic if anything changed
    python autoheal.py --loop 15 --push  keep running: re-check + push every 15 min
"""

import argparse
import json
import re
import subprocess
import sys
import socket
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

# --- Configuration -------------------------------------------------------

REPO = Path(__file__).resolve().parent

TARGETS = [
    "YB.m3u", "YB_Categories.m3u",
    "YB_Safe.m3u", "YB_Safe_Categories.m3u",
    "YB_NOVPN.m3u", "YB_NOVPN_Categories.m3u",
]
# extra places to look for a channel's alternate URLs (matched by name)
ALT_SOURCES = ["YB_Backup.m3u", "YB_Auto.m3u"]

# Hosts we deliberately do NOT probe. A channel on one of these is left exactly
# as it is: no request is made, the result is 'unknown', and nothing is healed,
# upgraded or parked.
#
# This is the OPPOSITE of DEAD_HOSTS, and the distinction matters:
#   SKIP = "the host is fine, we just can't see it" -> leave its channels alone
#   DEAD = "the host is gone"                       -> migrate every channel off
# Skipping a dead host strands its channels; declaring a live-but-unreachable
# host dead migrates 90+ channels onto worse sources for nothing.
#
# akariko-bck1, re-added 2026-08-08. The host is HEALTHY — a paced manual probe
# from a normal desktop shows every slug alive except the 8 in
# AKARIKO_BCK_DEAD_SLUGS, and viewers reach it fine. The machine running this
# checker cannot, and the reason is on our side, not the host's: its TLS trust
# store is broken, so every https request to it fails verification before a
# request is even sent. curl reports
#     error setting certificate verify locations:
#     CAfile: /usr/local/Python38/sys/etc/certs/ca-bundle.crt
# and urllib (which this script uses) fails the same way, returning 'unknown'.
#
# Skipping is the correct response and DEAD_HOSTS would be actively wrong: dead
# migrates every channel off a host that is actually the best source we have,
# and it carries 92 of the 160. Skipped, those channels are left exactly as the
# playlists define them and everything else still heals normally.
#
# Remove this line once the CA bundle is fixed — the block is ours to clear, and
# until it is we are running the healer half-blind.
# akariko-bck1 skip removed 2026-08-14: it was there for a local CA-bundle
# problem on the original author's machine. CI has a working CA bundle, and
# the host is now confirmed dead (connection failure), so we want it probed,
# healed and parked like any other.
SKIP_HOSTS = set()

DEAD_HOSTS = [
    "akariko.netgenx.site",
    # Confirmed dead again 2026-08-14: connection failure, not a 404. Most of the
    # "alternates" harvested from JP_Backup/JP_Auto also point here, so it must be
    # excluded by host or autoheal will just swap one dead link for another.
    "akariko-bck1.sankuria.sbs",
    "converse.nathcreqtives.com", "live.livingjtv.live",
    # akariko-bck1 was listed here 2026-08-05 -> 2026-08-08. It died in stages:
    # 8 slugs 404'd first, then the whole host. It came back on 2026-08-08,
    # verified by a paced manual probe — everything alive except the same 8,
    # which are still 404. Those 8 are handled by removing them from
    # AKARIKO_BCK_SLUG, not by declaring the whole host dead.
]
# (cdns.jp-primehome.com is BACK as of Jul 2026 — used as a fallback, see PRIMEHOME_CID)

# Hosts that WORK but that we refuse to publish. Not a health judgement — a
# policy one — so they get their own list rather than a lie in DEAD_HOSTS.
# Treated identically to dead: never offered as a candidate, and any channel
# already sitting on one is migrated off (or parked) on the next pass.
#
# Onyx, added 2026-08-08. We evaluated it as a fallback in Aug 2026 and decided
# against it: the streams were unstable under deep probing, and its URLs embed a
# subscription's username/password in the path, which we will not publish.
#
# Dropping it from EXTRA_CANDIDATES was NOT enough. autoheal harvests candidate
# URLs per tvg-id out of ALT_SOURCES, and Propho's JP_Auto.m3u carries Onyx links
# of its own — a second, independent supply line. On 2026-08-08 that route
# unparked TOKYO MX2 and KBS onto Onyx and pushed the credentials to a public
# repo. Blocking by HOST is the only fix that closes every route at once.
BANNED_HOSTS = [
    "onyx.trustissues.life",
    "live.forjoytv.com",
]

# Everything we will not heal onto, for whatever reason.
EXCLUDED_HOSTS = DEAD_HOSTS + BANNED_HOSTS

# Extra fallback URLs (keyed by tvg-id) for channels that would otherwise have
# no source when haru/naori are down. Found via iptv-org. Add more here as you
# discover independent links for the premium haru-only channels.
EXTRA_CANDIDATES = {
    # Weather News — official Akamai feed
    "rch_45": ["https://rch01e-alive-hls.akamaized.net/38fb45b25cdb05a1/out/v1/4e907bfabc684a1dae10df8431a84d21/index.m3u8"],
    # KBS World — vtvprime restream (can be intermittent)
    "CH.656": ["https://liveh12.vtvprime.vn/hls/KBS/03.m3u8"],

    # --- durable extra fallbacks (from the BaheulaTV list, token-free/playable) ---
    # Tokyo terrestrials — MOV3 restreams (+ i9.ee alt for NHK G)
    "NHK東京・総合_jp": ["https://nhk4.mov3.co/hls/nhk.m3u8",
                          "http://tsb-mega.i9.ee/stream/nhkg_avc_1080p"],
    "日本テレビ_jp":     ["https://ntv5.mov3.co/hls/ntv.m3u8"],
    "TBS_jp":            ["https://tbs5.mov3.co/hls/tbs.m3u8"],
    "フジテレビ_jp":     ["https://fujitv4.mov3.co/hls/fujitv.m3u8"],
    "テレビ朝日_jp":     ["https://tvasahi.mov3.co/hls/tvasahi.m3u8"],
    # SunTV — jpnettv (has a token in the URL but the server doesn't enforce it)
    "サンテレビ_jp": ["https://cdn.th11.jpnettv.live/watch/live.m3u8?video=kansi_sun_tv_720&sid=6UfUvVezZZ4Xhiq5QTkP7e7iprNrhEIGYuSDN6bL&secret=3fa0200ac6b80c134f320b9b06edeb80&time=1721464163"],
    # NHK World-Japan — official (main + Tokyo backup)
    "NHKWorldJapan.jp": ["https://media-osa.hls.nhkworld.jp/hls/w/live/master.m3u8",
                          "https://media-tyo.hls.nhkworld.jp/hls/w/live/master.m3u8"],
    # Shop Channel — official (primary + backup)
    "ショップチャンネル_jp": ["http://stream1.shopch.jp/HLS/out1/prog_index.m3u8",
                              "https://stream3.shopch.jp/HLS/master.m3u8"],
    # STOCK VOICE — videog.jp (fills the gap; this channel has no naori/58 source)
    "rch_83": ["https://p14080-live02.videog.jp/stockvoice/abr-streamnt/playlist_dvr.m3u8"],
    # QVC — official Rakuten cloudfront variant
    "rch_102": ["https://d1flvb4iqlercm.cloudfront.net/live/live_1080p_2nd.m3u8"],
    # GSTV — MediaKind CDN
    "rch_112": ["https://japaneast.av.mk.io/mediakindcdn-mediakind/ca01a143-f823-4432-b670-c22ff9643ce4/index.qfm/manifest(format=m3u8-cmaf)"],
}

# primehome fallback (cdns.jp-primehome.com, back online) — tvg-id -> zhongying cid.
# A third live source layer, from MrKagesan's list. Used only when the primary
# host is dead (ranked below naori/haru/58.x). Does NOT cover the haru-only
# premiums (KBS World/KNTV/Mnet/AXN/Mystery/etc. have no cid).
PRIMEHOME_CID = {
    "NHK東京・総合_jp": "hdgd01", "NHK東京・教育_jp": "hdgd02", "日本テレビ_jp": "hdgd03",
    "テレビ朝日_jp": "hdgd06", "TBS_jp": "hdgd04", "テレ東_jp": "hdgd07",
    "フジテレビ_jp": "hdgd05", "TOKYO・MX_jp": "hdgd08", "NHK大阪・総合_jp": "gx06",
    "毎日テレビ_jp": "gx01", "ABCテレビ_jp": "gx02", "関西テレビ_jp": "gx03",
    "読売テレビ_jp": "gx04", "テレビ大阪_jp": "gx05", "サンテレビ_jp": "gx07",
    "CH.572": "cs11", "CH.566": "cs16", "CH.565": "cs15", "NHK・BS_jp": "bs11",
    "BS日テレ_jp": "bs02", "BS朝日_jp": "bs03", "BS-TBS_jp": "bs04", "BSテレ東_jp": "bs05",
    "BSフジ_jp": "bs06", "J-COM-BS_jp": "bs31", "NHKBSP4K.jp": "bs01", "CH.621": "bs12",
    "CH.622": "bs20", "CH.623": "bs07", "CH.625": "bs08", "CH.603": "bs18",
    "CH.604": "bs19", "CH.606": "bs21", "CH.605": "bs22", "CH.608": "cs02",
    "CH.688": "bs14", "CH.629": "cs27", "CH.634": "bs23", "CH.632": "cs14",
    "CH.628": "cs22", "CH.662": "cs04", "CH.661": "cs05", "CH.664": "cs29",
    "CH.654": "cs19", "CH.660": "cs20", "CH.613": "cs26", "CH.670": "bs15",
    "CH.668": "cs25", "CH.669": "cs07", "CH.620": "bs24", "CH.672": "cs23",
    "CH.640": "cs18", "CH.639": "cs06", "TAKARAZUKASKYSTAGE.jp": "cs28",
    "CH.675": "cs10", "CH.676": "cs08", "CH.677": "cs24", "CH.544": "cs12",
    "CH.659": "cs21",
}

# akariko's OFFICIAL BACKUP host (posted by haru on their Telegram, Jul 2026).
# Same operator/slug scheme as haru; carries the premium channels haru gated, so
# it rescues most of the parked ones. tvg-id -> slug. Ranked just below haru.
# Slugs akariko-bck1 serves a 404 for. The host came back on 2026-08-08 after
# its Aug 5 outage, but these eight did not — they were also the first to go
# when it started failing, and a paced re-probe on the 8th still shows 404.
#
# Kept as an exclusion set rather than deleted from the map below, because a 404
# here has historically meant "wrong slug", not "channel absent" — akariko was
# recorded for weeks as having no NHK sub-channels purely because we were asking
# for haru's spelling. If a working slug turns up, delete the entry here and the
# channel heals on the next pass with no other change.
AKARIKO_BCK_DEAD_SLUGS = {
    "shogi_channel", "tetsudo_channel", "mystery_channel", "music_air",
    "action_channel", "kntv", "mnet", "dlife",
}

AKARIKO_BCK_SLUG = {
    "ABCテレビ_jp": "abc", "BS-TBS_jp": "bs_tbs", "BS10_jp": "bs10",
    "BS11-イレブン_jp": "bs11", "BS12トゥエルビ_jp": "bs_12",
    "BSよしもと_jp": "bs_yoshimoto", "BSテレ東_jp": "bs_tv_tokyo",
    "BSフジ_jp": "bs_fuji", "BS日テレ_jp": "bs_ntv", "BS朝日_jp": "bs_asahi",
    "CH.544": "tabi_channel", "CH.565": "bbc_news", "CH.566": "cnnj",
    "CH.571": "ntv_news24", "CH.572": "tbs_news", "CH.580": "sport_live_plus",
    "CH.600": "fighting_tv_samurai", "CH.603": "jsport_1", "CH.604": "jsport_2",
    "CH.605": "jsport_4", "CH.606": "jsport_3", "CH.608": "nittele_g+",
    "CH.611": "tv_asahi_channel_1", "CH.612": "tv_asahi_channel_2",
    "CH.613": "fuji_tv_next", "CH.614": "fuji_tv_one", "CH.615": "fuji_tv_two",
    "CH.616": "tbs_channel_1", "CH.617": "tbs_channel_2", "CH.619": "nittele_plus",
    "CH.620": "disney_channel", "CH.621": "wowow_prime", "CH.622": "wowow_live",
    "CH.623": "wowow_cinema", "CH.625": "bs_10_premium", "CH.628": "eisei_gekijo",
    "CH.629": "toei_channel", "CH.630": "wowow_plus", "CH.631": "the_cinema",
    "CH.632": "movie_plus", "CH.634": "nihon_eiga_senmon", "CH.639": "music_japan_tv",
    "CH.640": "mtv", "CH.641": "music_on_tv", "CH.649": "mystery_channel",
    "CH.650": "action_channel", "CH.654": "lala_tv", "CH.656": "kbs_world",
    "CH.657": "kntv", "CH.658": "mnet", "CH.659": "mondo_tv",
    "CH.660": "family_gekijyo", "CH.661": "home_drama_channel",
    "CH.662": "jidaigeki_senmon", "CH.664": "channel_ginga", "CH.667": "at-x",
    "CH.668": "cartoon_network", "CH.670": "animax", "CH.672": "disney_junior",
    "CH.675": "national_geographic_japan", "CH.676": "discovery_channel",
    "CH.677": "animal_planet", "CH.688": "green_channel",
    "Dlife(ディーライフ)_jp": "dlife", "J-COM-BS_jp": "jcom_bs", "KBS京都_jp": "kbs",
    "NHKBSP4K.jp": "nhk_bs4k", "NHK・BS_jp": "nhk_bs", "NHK大阪・総合_jp": "nhk_g_osaka",
    "NHK東京・教育_jp": "nhk_e", "NHK東京・総合_jp": "nhk_g",
    "TAKARAZUKASKYSTAGE.jp": "takarazuka_sky_stage", "TBS_jp": "tbs",
    "TOKYO・MX2_jp": "tokyo_mx2", "TOKYO・MX_jp": "tokyo_mx1",
    # added 2026-07-23 — these exist on the host but the name-match missed them
    # when the map was generated (found after a viewer reported Neco failing)
    "rch_59": "neco_ch", "CH.635": "v_paradise_nsfw", "rch_41": "pigoo_nsfw",
    # added 2026-08-04 — akariko DOES carry the sub-channels after all, using a
    # "_2" suffix rather than haru's "(subch)". Both confirmed working, which
    # takes the last two channels off haru before its access-code gate lands.
    "NHK東京・総合2_jp": "nhk_g_2", "NHK・BS2_jp": "nhk_bs_2",
    "rch_30": "tetsudo_channel", "rch_36": "shogi_channel", "rch_43": "history",
    "rch_45": "weather_news", "rch_46": "fishing_vision", "rch_49": "sky_a",
    "rch_51": "gaora_sports", "rch_75": "golf_network", "rch_85": "kayo_pops",
    "サンテレビ_jp": "sun", "スペースシャワーTV_jp": "space_shower_tv",
    "テレビ大阪_jp": "tv_osaka", "テレビ朝日_jp": "tv_asahi", "テレ東_jp": "tv_tokyo",
    "フジテレビ_jp": "fuji_tv", "ミュージック・エア_jp": "music_air",
    "メ～テレNEXT_jp": "me-tele_next", "日本テレビ_jp": "ntv", "毎日テレビ_jp": "mbs",
    "読売テレビ_jp": "ytv", "関西テレビ_jp": "kansai_tv",
}

# Candidate URLs to NEVER use for a channel, even if a name/alt match suggests
# them. The zhongying "bs11" cid is actually NHK BS, so BS11 (haru-slug-only)
# must never heal onto any bs11-cid source or it'll silently become NHK BS.
# Channels where WE have chosen the source by hand, usually because it looks
# better than whatever the host ranking would pick. autoheal keeps a pinned link
# as long as it's alive and never "upgrades" away from it — without this, a
# manual edit gets reverted on the next pass, because HOST_PREF would rank the
# hand-picked host lower than akariko/naori and snap back to them.
#
# A pinned link that goes DOWN still heals normally, so pinning can't strand a
# channel. tvg-id -> url.
PINNED = {
    # 1080p, noticeably better picture than the akariko feed
    "NHK東京・総合_jp": "http://tsb-mega.i9.ee/stream/nhkg_avc_1080p",
}

WRONG_CANDIDATES = {
    "BS11-イレブン_jp": ["shk_cid=bs11", "cid=bs11", "gid=bs11", "/bs11.m3u8"],
    # Sub-channels must never fall back to their PARENT channel's stream, or they
    # silently become a duplicate of it (gd01 = NHK G, bs11 = NHK BS in the
    # zhongying cid namespace; /nhk_g/ and /nhk_bs/ are the parent slugs).
    "NHK東京・総合2_jp": ["shk_cid=gd01", "cid=gd01", "gid=gd01", "/gd01.m3u8",
                          "shk_cid=hdgd01", "cid=hdgd01", "/stream/jp/nhk_g/"],
    "NHK・BS2_jp":      ["shk_cid=bs11", "cid=bs11", "gid=bs11", "/bs11.m3u8",
                          "/stream/jp/nhk_bs/"],
}

TIMEOUT = 10                      # seconds per health check
USER_AGENT = "VLC/3.0.20 LibVLC/3.0.20"
THROTTLE = 0.4                    # default seconds paused before each check; one at
                                  # a time, so a host never sees a burst (no 429 lockout)
# Per-host pacing. Some hosts cap CONCURRENT streams and count each manifest fetch
# as an active stream for several seconds — so probing faster than that ceiling
# makes the checker compete with itself and get "Channel limit has been reached".
# akariko-bck1 allows ~3 at once; 5s clears it (verified: 1.5s -> every 4th 429,
# 5s -> all 200). Raise a value here if a host starts refusing checks.
HOST_THROTTLE = {
    # akariko-bck1 history: 5s stopped the 429s, 10s gave headroom, 20s after
    # haru died and this became the only source for 100 channels.
    #
    # Aug 2026: it now sits behind Cloudflare, which answers a scan-like burst
    # with a 403 "Attention Required!" page. Per haru's Telegram that block is
    # TEMPORARY and triggered by the scanning itself, so the answer is the same
    # as it always was here — go slower, don't dress the checker up as something
    # else. 60s means ~1 request/minute, well under anything that reads as a
    # scan, at the cost of a much longer sweep (see HOST_SCAN_BATCH).
    "akariko-bck1.sankuria.sbs": 60.0,
}
# How often a host may be scanned at all, in seconds. The loop can run every 30
# min for hosts that don't mind, while a host that objects to frequent automated
# checking is swept far less often. akariko-bck1 returned
# "Automated scanning is not permitted" after ~48 sweeps in a day; Propho runs an
# equivalent checker against the same infrastructure every 4h with no issue, so
# match that. Channels on a host that's inside its cooldown are simply left alone
# (no request made, no change). `--now` overrides and scans everything.
HOST_SCAN_INTERVAL = {
    # Every 5h, so the 20-channel batches roll through all 100 in about a day.
    "akariko-bck1.sankuria.sbs": 5 * 3600,
}
# Check at most N channels per sweep for a host, rotating through the list so
# every channel still gets covered over successive sweeps. Keeps each visit small
# instead of walking all 99 in one go. `--now` ignores this and checks everything.
HOST_SCAN_BATCH = {
    # At 60s apiece, 20 channels is a 20-minute visit. Five sweeps covers all
    # 100, so every channel is seen roughly once a day at ~100 requests/day —
    # about a third of what we were doing before the Cloudflare block appeared.
    "akariko-bck1.sankuria.sbs": 20,
}
STATE_FILE = ".autoheal_state.json"   # remembers last scan time per host
TRIES = 3                         # probes per check; retries ride out transient
                                  # 429/522/timeout flaps on popular channels
HOST_FAIL_LIMIT = 8               # consecutive non-alive replies from ONE host before
                                  # we stop probing it for the rest of the run. Stops a
                                  # rate-limited host from being hammered into a deeper
                                  # block (95 channels x 3 retries = ~285 requests).
CONFIRM = 2                       # a better host (naori/haru) must test alive this
                                  # many checks in a row before a WORKING link is
                                  # migrated back to it — anti-flap safeguard
PARK_OFFLINE = True               # when a channel has NO working source, comment it
                                  # out of the lists (hide it), and restore it the
                                  # moment its source comes back
PARK_MARK = "#PARKED#"            # prefix that comments out a parked channel's lines
GIT_BRANCH = "main"

# -------------------------------------------------------------------------

TVG = re.compile(r'tvg-id="([^"]*)"')


def read(p):
    return Path(p).read_bytes().decode("utf-8", "surrogateescape")


def write(p, t):
    Path(p).write_bytes(t.encode("utf-8", "surrogateescape"))


def norm(s):
    """Normalised channel-name key used to match a channel across playlists.

    IMPORTANT: a sub-channel must NEVER normalise to the same key as its parent
    ("NHK G" vs "NHK G (Sub Ch.)"), or the parent's stream gets matched in as a
    candidate and the sub-channel silently becomes a duplicate of it. So the
    sub-channel marker is folded to one token that SURVIVES normalisation.
    """
    s = unicodedata.normalize("NFKC", s).lower()
    is_sub = bool(re.search(r'\(sub\s*ch\.?\)|\(subch\)|\bsubch\b', s))
    s = re.sub(r'\(fast\)|\bhd\b|720p|1080p|nsfw|\(sub ch\.?\)|\(subch\)|'
              r'staton|station|\(fhd\)|\(sd\)|\(r\)|\(axn\)', '', s)
    s = re.sub(r'[^a-z0-9぀-ヿ一-鿿]', '', s)
    return s + ("subch" if is_sub else "")


def entries(text):
    """Return (lines, [(url_line_index, tvgid, name, url)])."""
    lines = text.split("\n")
    out = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].startswith("#EXTINF"):
            m = TVG.search(lines[i])
            tid = m.group(1) if m else ""
            name = lines[i].rsplit(",", 1)[-1].strip()
            j = i + 1
            while j < n and (not lines[j].strip() or lines[j].strip().startswith("#")):
                j += 1
            if j < n:
                out.append((j, tid, name, lines[j].strip()))
            i = j
        else:
            i += 1
    return lines, out


def zhongying_id(url):
    m = (re.search(r'shk_cid=([a-z0-9]+)', url)
         or re.search(r'/([a-z0-9]+)\.m3u8\?token=guoziyun', url))
    return m.group(1) if m else None


def naori_url(i):
    return f"https://naori-test.netgenx.site/pxx.php?shk_cid={i}"


def x58_url(i):
    return f"http://58.82.168.138:5002/{i}.m3u8?token=guoziyun&gid={i}&channel=zhongying"


def primehome_url(cid):
    return f"http://cdns.jp-primehome.com:8000/zhongying/live/playlist.m3u8?cid={cid}"


def akariko_bck_url(slug):
    return f"https://akariko-bck1.sankuria.sbs/stream/jp/{slug}/stream-output.m3u8?mode=hls"


# Preferred host order (better video quality first). A channel is swapped to the
# highest-ranked host that's alive, so when naori/haru recover it snaps back off
# the 58.x fallback. Unknown/unique links rank last (kept only if nothing better).
HOST_PREF = [
    "akariko-bck1.sankuria.sbs", # 0 - PREFERRED: loads instantly, https, and
                                 #     carries the premiums haru gates
    "naori-test.netgenx.site",   # 1 - good quality but slower to start
    "58.82.168.138",             # 2 - fallback
    "cdns.jp-primehome.com",     # 3 - primehome (MrKagesan's host, cid-based)
    # haru.charandom.blog was rank 2 until Aug 2026, when it locked every stream
    # behind an access code nobody has. Removed rather than reordered, so it now
    # ranks last (same as any unknown host): autoheal will never *upgrade* a
    # working channel onto it, but can still fall back to it if it's genuinely
    # the only thing alive. Put it back above 58.x if codes ever circulate.
]


def rank(url):
    for i, host in enumerate(HOST_PREF):
        if host in url:
            return i
    return len(HOST_PREF)         # unique/other link - lowest preference


def build_alt_sources():
    """normalized name -> [alternate urls] gathered from ALT_SOURCES."""
    alt = {}
    for f in ALT_SOURCES:
        p = REPO / f
        if not p.exists():
            continue
        _, es = entries(read(p))
        for _, _, name, url in es:
            alt.setdefault(norm(name), []).append(url)
    return alt


def candidates(tid, name, url, altsrc):
    """Ordered, de-duplicated candidate URLs for a channel (current first)."""
    cands = [url]
    z = zhongying_id(url)
    if z:
        cands += [naori_url(z), x58_url(z)]
    cands += altsrc.get(norm(name), [])
    cands += EXTRA_CANDIDATES.get(tid, [])
    if tid in AKARIKO_BCK_SLUG and AKARIKO_BCK_SLUG[tid] not in AKARIKO_BCK_DEAD_SLUGS:
        cands.append(akariko_bck_url(AKARIKO_BCK_SLUG[tid]))
    if tid in PRIMEHOME_CID:
        cands.append(primehome_url(PRIMEHOME_CID[tid]))
    wrong = WRONG_CANDIDATES.get(tid, [])
    seen, out = set(), []
    for u in cands:
        if not u or u in seen:
            continue
        if any(d in u for d in EXCLUDED_HOSTS):
            continue
        if any(w in u for w in wrong):           # never heal onto a mismatched source
            continue
        seen.add(u)
        out.append(u)
    out.sort(key=rank)            # preferred host first (stable: ties keep order)
    return out


_host_fails = {}      # host -> consecutive non-alive replies (circuit breaker)
_last_code = None     # HTTP status of the most recent probe, or None


def host_of(url):
    m = re.match(r'https?://([^/?]+)', url or "")
    return m.group(1) if m else ""


def reset_breakers():
    """Clear the per-host circuit breakers. Called at the start of every run."""
    _host_fails.clear()


def throttle_for(url):
    """Seconds to wait before probing this url — per-host, see HOST_THROTTLE."""
    h = host_of(url)
    for key, secs in HOST_THROTTLE.items():
        if key in h:
            return secs
    return THROTTLE


_last_scan = {}          # host -> unix time of last scan (persisted)
_batch_allow = set()     # urls cleared for this sweep on batched hosts
_scan_now = set()        # hosts cleared to be scanned this run
FORCE_SCAN = False       # set by --now


def load_state():
    global _last_scan
    try:
        _last_scan = json.loads((REPO / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        _last_scan = {}


def save_state():
    try:
        (REPO / STATE_FILE).write_text(json.dumps(_last_scan), encoding="utf-8")
    except Exception:
        pass


def may_scan(url):
    """Is this host inside its scan cooldown? Hosts without an interval are always
    scannable. Marks the host as scanned the first time it's cleared in a run."""
    h = host_of(url)
    interval = next((s for k, s in HOST_SCAN_INTERVAL.items() if k in h), 0)
    if not interval or FORCE_SCAN:
        return True
    if h in _scan_now:
        return True
    if time.time() - _last_scan.get(h, 0) >= interval:
        _scan_now.add(h)
        _last_scan[h] = time.time()
        return True
    return False


def batch_slice(host_key, urls):
    """The rotating subset of `urls` to check this sweep for a batched host.
    Remembers where it stopped, so successive sweeps cover the whole list."""
    n = next((v for k, v in HOST_SCAN_BATCH.items() if k in host_key), 0)
    if not n or FORCE_SCAN or n >= len(urls):
        return set(urls)
    start = int(_last_scan.get("batch:" + host_key, 0)) % len(urls)
    picked = [urls[(start + i) % len(urls)] for i in range(n)]
    _last_scan["batch:" + host_key] = (start + n) % len(urls)
    return set(picked)


def _probe(url):
    """One health probe -> 'alive' | 'dead' | 'unknown'.

    Only a HARD failure counts as dead: HTTP 404/410, a refused connection, or a
    DNS failure (the host/path is really gone), or a 403 whose body says the
    channel is pulled/gated (haru "Missing params"). Ambiguous responses -- 429
    (busy), other 403s, 5xx (e.g. Cloudflare 522), or a timeout -- return
    'unknown', because those happen constantly on healthy streams under load.
    """
    global _last_code
    _last_code = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            _last_code = getattr(r, "status", 200)
            if _last_code == 200 and len(r.read(1024)) > 0:
                return "alive"
            return "unknown"
    except urllib.error.HTTPError as e:
        _last_code = e.code
        if e.code in (404, 410):
            return "dead"
        # Some hosts return a 403 with an error body when a channel is pulled or
        # gated (haru: "Missing params"). Treat that as dead so it heals/flags;
        # keep 429 "Channel limit" and generic 403s as transient/unknown.
        try:
            body = e.read(300).decode("utf-8", "ignore").lower()
        except Exception:
            body = ""
        if any(s in body for s in ("missing params", "no stream", "not available")):
            return "dead"
        return "unknown"
    except urllib.error.URLError as e:
        if isinstance(e.reason, (ConnectionRefusedError, socket.gaierror)):
            return "dead"
        return "unknown"          # timeout and other transient errors
    except Exception:
        return "unknown"


def check(url):
    """Health-check with retries so a transient hiccup (429 busy, Cloudflare 522,
    a timeout) doesn't read a healthy stream as down. Returns 'alive' the instant
    any probe succeeds; 'dead' only if a probe hard-fails and none ever succeed;
    otherwise 'unknown'. Popular haru channels flap 200<->522/429, so a single
    shot is unreliable -- this gives them a few chances.

    Also guards against hammering a host that has started refusing us: after
    HOST_FAIL_LIMIT consecutive non-alive replies from one host, further checks
    against it are skipped for the rest of the run (returning 'unknown', which is
    a no-op). Without this, a rate-limited host gets 3x the traffic and the block
    deepens — that's how akariko-bck1 ended up 403ing all 95 of its channels.
    """
    h = host_of(url)
    if any(s in h for s in SKIP_HOSTS):
        return "unknown"                  # deliberately not probed — see SKIP_HOSTS
    if not may_scan(url):
        return "unknown"                  # host is inside its scan cooldown
    if _batch_allow and any(k in h for k in HOST_SCAN_BATCH) and url not in _batch_allow:
        return "unknown"                  # not in this sweep's rotating subset
    if _host_fails.get(h, 0) >= HOST_FAIL_LIMIT:
        return "unknown"                  # circuit open — stop poking this host

    pace = throttle_for(url)
    result = "unknown"
    for attempt in range(TRIES):
        time.sleep(pace)              # per-host pacing — never burst a host
        r = _probe(url)
        if r == "alive":
            _host_fails[h] = 0            # host is answering again
            return "alive"
        if r == "dead":
            result = "dead"
            break                         # a 404 won't become a 200 in 0.4s
        if _last_code == 403:
            break                         # a block won't clear on retry either
    if result != "dead":
        _host_fails[h] = _host_fails.get(h, 0) + 1
    return result


def decide_url(tid, name, url, altsrc, state):
    """Decide the url to use for a channel. Returns (chosen_url_or_None, kind,
    current_link_status):

      * 'heal'     — current link was confirmed dead and replaced (or None if no
                     replacement is alive)
      * 'snapback' — current link works but a BETTER host (naori > haru > 58.x)
                     has tested alive CONFIRM checks in a row, so migrate to it
      * None       — no change (current stays)

    Rate-limit / gating noise (403/429/timeouts) is 'unknown', never dead, so it
    can't churn a working link. `state` holds per-channel snap-back streak counts.
    """
    cands = candidates(tid, name, url, altsrc)   # sorted best-host-first

    # A hand-picked source wins over host ranking for as long as it works, so a
    # deliberate choice isn't undone by the next snap-back pass.
    pin = PINNED.get(tid)
    if pin:
        pin_st = st = check(pin) if pin != url else check(url)
        if pin_st == "alive":
            state.pop(tid, None)
            return pin, (None if pin == url else "pin"), st
        # pinned link is down — fall through and heal like any other channel

    # A link on a host we've declared dead is dead by definition — don't probe
    # it. This matters when the checker cannot reach that host at all: the probe
    # would come back 'unknown', which is not 'dead', and the channel would sit
    # broken forever waiting for a confirmation that can never arrive.
    # BANNED hosts ride the same path on purpose: a host we refuse to publish
    # must be evacuated exactly like one that died, even though it answers.
    if any(d in url for d in EXCLUDED_HOSTS):
        st = "dead"
    else:
        st = check(url)                          # health of the current link

    if st == "dead":
        state.pop(tid, None)
        for u in cands:
            if u != url and check(u) == "alive":
                return u, "heal", st
        return None, "heal", st                   # dead, nothing alive to swap in

    # current link works — is a strictly-better host alive right now?
    cur_rank = rank(url)
    target = None
    for u in cands:
        if rank(u) >= cur_rank:                    # nothing better remains
            break
        if u != url and check(u) == "alive":
            target = u
            break

    if target is None:
        state.pop(tid, None)
        return url, None, st

    # anti-flap: only migrate after the better host is alive CONFIRM runs running
    s = state.get(tid)
    n = s["n"] + 1 if (s and s.get("cand") == target) else 1
    if n >= CONFIRM:
        state.pop(tid, None)
        return target, "snapback", st
    state[tid] = {"cand": target, "n": n}
    return url, None, st


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True)


def push(changed_files, msg):
    git("add", *changed_files)
    if git("status", "--porcelain").stdout.strip():
        git("commit", "-m", msg)
    # integrate any remote changes first, else the push gets rejected (fetch first)
    pr = git("pull", "--rebase", "origin", GIT_BRANCH)
    if pr.returncode != 0:
        git("rebase", "--abort")
        return False, "pull --rebase failed: " + (pr.stdout + pr.stderr).strip()
    # push if we have anything unpushed (also flushes a previously-stuck commit)
    ahead = git("rev-list", "--count", f"origin/{GIT_BRANCH}..{GIT_BRANCH}").stdout.strip()
    if ahead in ("", "0"):
        return True, "nothing to push"
    p = git("push", "origin", GIT_BRANCH)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def parked_urls(lines):
    """URLs currently parked (commented out) in a list of lines."""
    out = set()
    for l in lines:
        if l.startswith(PARK_MARK):
            inner = l[len(PARK_MARK):].strip()
            if inner.startswith("http"):
                out.add(inner)
    return out


def parked_meta(lines):
    """[(parked_url, tvg-id, display name)] for each parked channel block."""
    out, n = [], len(lines)
    i = 0
    while i < n:
        inner = lines[i][len(PARK_MARK):] if lines[i].startswith(PARK_MARK) else ""
        if inner.startswith("#EXTINF"):
            m = TVG.search(inner)
            tid = m.group(1) if m else ""
            name = inner.rsplit(",", 1)[-1].strip()
            j, url = i + 1, None
            while j < n and lines[j].startswith(PARK_MARK):
                s = lines[j][len(PARK_MARK):].strip()
                if s.startswith("http"):
                    url = s
                j += 1
            if url:
                out.append((url, tid, name))
            i = j
        else:
            i += 1
    return out


def park_unpark(lines, park_urls, unpark_urls):
    """In `lines` (mutated in place): comment out active channel blocks whose url
    is in park_urls, and un-comment parked blocks whose url is in unpark_urls.
    A block is commented by prefixing each of its non-blank lines with PARK_MARK,
    which hides it from players while keeping it exactly in place. Returns
    (n_parked, n_unparked)."""
    n = len(lines)
    np = nu = 0

    # unpark: strip the marker from parked blocks whose url has recovered
    i = 0
    while i < n:
        if lines[i].startswith(PARK_MARK) and lines[i][len(PARK_MARK):].startswith("#EXTINF"):
            j, url = i, None
            while j < n and lines[j].startswith(PARK_MARK):
                inner = lines[j][len(PARK_MARK):].strip()
                if inner.startswith("http"):
                    url = inner
                j += 1
            if url and url in unpark_urls:
                repl = unpark_urls[url]           # may be a different (new) host
                for k in range(i, j):
                    lines[k] = lines[k][len(PARK_MARK):]
                    if lines[k].strip() == url and repl != url:
                        trail = "\r" if lines[k].endswith("\r") else ""
                        lines[k] = repl + trail
                nu += 1
            i = j
        else:
            i += 1

    # park: comment out active blocks whose url is dead-with-no-source
    i = 0
    while i < n:
        if lines[i].startswith("#EXTINF"):
            j = i + 1
            while j < n and (not lines[j].strip() or lines[j].strip().startswith("#")):
                j += 1
            if j < n and lines[j].strip() in park_urls:
                for k in range(i, j + 1):
                    if lines[k].strip():
                        lines[k] = PARK_MARK + lines[k]
                np += 1
            i = j + 1
        else:
            i += 1

    return np, nu


def lint_playlists():
    """Structural sanity checks — no network, runs on every pass. Catches the
    kinds of silent corruption that don't show up as a dead link:

      1. duplicate tvg-id inside a file  (autoheal keys by tvg-id, so a shared id
         means one channel's URL can be applied to another — this is how the NHK
         sub-channels once became duplicates of the main channels)
      2. two channels pointing at the SAME stream URL (duplicate content)
      3. a flat list and its _Categories twin drifting out of sync

    Returns a list of warning strings (empty == clean)."""
    warn = []

    def chans(path):
        out = []
        for l in read(path).split("\n"):
            s = l[len(PARK_MARK):] if l.startswith(PARK_MARK) else l
            if s.startswith("#EXTINF"):
                m = TVG.search(s)
                out.append([m.group(1) if m else "", s.rsplit(",", 1)[-1].strip(), None])
            elif out and out[-1][2] is None:
                u = s[len(PARK_MARK):].strip() if l.startswith(PARK_MARK) else s.strip()
                if u.startswith("http"):
                    out[-1][2] = u
        return out

    sets = {}
    for f in TARGETS:
        p = REPO / f
        if not p.exists():
            continue
        cs = chans(p)
        sets[f] = {(t, n) for t, n, _ in cs}

        seen = {}
        for tid, name, _ in cs:
            if tid:
                seen.setdefault(tid, []).append(name)
        for tid, names in seen.items():
            if len(names) > 1:
                warn.append(f"{f}: duplicate tvg-id {tid!r} on {', '.join(names)}")

        byurl = {}
        for tid, name, url in cs:
            if url:
                byurl.setdefault(url, []).append(name)
        for url, names in byurl.items():
            if len(names) > 1:
                warn.append(f"{f}: same stream on {', '.join(names)} — {url[:60]}")

    for flat, cat in (("YB.m3u", "YB_Categories.m3u"),
                      ("YB_Safe.m3u", "YB_Safe_Categories.m3u"),
                      ("YB_NOVPN.m3u", "YB_NOVPN_Categories.m3u")):
        if flat in sets and cat in sets:
            for miss in sets[flat] - sets[cat]:
                warn.append(f"{cat}: missing {miss[1]!r} (present in {flat})")
            for extra in sets[cat] - sets[flat]:
                warn.append(f"{cat}: has {extra[1]!r} but {flat} doesn't")
    return warn


def audit_backups():
    """Proactively health-check every defined fallback (EXTRA_CANDIDATES +
    primehome), so a backup dying is caught BEFORE a channel needs it. Read-only
    — prints a report, changes nothing. Meant to run once a day."""
    names = {}                                    # tvg-id -> display name
    for l in read(REPO / "YB.m3u").split("\n"):
        s = l[len(PARK_MARK):] if l.startswith(PARK_MARK) else l
        if s.startswith("#EXTINF"):
            m = re.search(r'tvg-id="([^"]*)"', s)
            if m:
                names.setdefault(m.group(1), s.rsplit(",", 1)[-1].strip())

    items = []                                    # (tvg-id, kind, url)
    for tid, urls in EXTRA_CANDIDATES.items():
        for u in urls:
            items.append((tid, "extra", u))
    for tid, cid in PRIMEHOME_CID.items():
        items.append((tid, "primehome", primehome_url(cid)))

    print(f"--- backup audit {time.strftime('%Y-%m-%d %H:%M')} — {len(items)} links ---")
    down = [(tid, kind, u) for tid, kind, u in items if check(u) != "alive"]
    if not down:
        print(f"All {len(items)} backups alive.")
    else:
        print(f"{len(down)} backup(s) DOWN:")
        for tid, kind, u in down:
            print(f"  [{kind}] {names.get(tid, tid)} — {u}")
    return down


def sweep_all(verbose=False):
    """Health-check EVERY known candidate for EVERY channel, not just the link
    each channel is currently using. Read-only — reports, changes nothing.

    A normal run only probes a channel's current url, and only looks at the
    alternatives when that url is dead or a better host might be available. So
    a fallback can quietly rot for weeks and you don't find out until the day
    you need it. --audit covers EXTRA_CANDIDATES and PRIMEHOME_CID; this covers
    everything, including the akariko-bck1 slug map and the alternates scraped
    out of JP_Backup / JP_Auto.

    Per-host pacing still applies, so this takes a while (akariko-bck1 alone is
    ~99 urls at 10s). Scan cooldowns and per-sweep batch limits are bypassed,
    since the whole point is total coverage.
    """
    global FORCE_SCAN
    was_forced = FORCE_SCAN
    FORCE_SCAN = True                     # ignore cooldowns/batching for this run
    reset_breakers()
    _batch_allow.clear()

    altsrc = build_alt_sources()
    lines = read(REPO / "YB.m3u").split("\n")
    _, master = entries("\n".join(lines))

    plan = []                             # (tvg-id, name, [candidates], parked?)
    for _, tid, name, url in master:
        plan.append((tid, name, candidates(tid, name, url, altsrc), False))
    for url, tid, name in parked_meta(lines):
        plan.append((tid, name, candidates(tid, name, url, altsrc), True))

    total = sum(len(c) for _, _, c, _ in plan)
    print(f"--- full sweep {time.strftime('%Y-%m-%d %H:%M')} — "
          f"{len(plan)} channels, {total} candidate links ---")

    results = {}                          # url -> status (probe each url once)
    no_source, per_channel = [], []
    for tid, name, cands, is_parked in plan:
        stats = []
        for u in cands:
            if u not in results:
                results[u] = check(u)
            stats.append((u, results[u]))
        alive = [u for u, s in stats if s == "alive"]
        per_channel.append((tid, name, stats, alive, is_parked))
        if not alive:
            no_source.append((tid, name, is_parked))
        if verbose:
            tag = "PARKED " if is_parked else ""
            print(f"  {tag}{name} — {len(alive)}/{len(stats)} alive")
            for u, s in stats:
                print(f"      [{s:7s}] {u}")

    alive_n = sum(1 for s in results.values() if s == "alive")
    dead_n = sum(1 for s in results.values() if s == "dead")
    unk_n = len(results) - alive_n - dead_n
    print(f"\nprobed {len(results)} unique links: "
          f"{alive_n} alive, {dead_n} dead, {unk_n} unknown")

    thin = [(n, len(a)) for _, n, s, a, _ in per_channel if 0 < len(a) <= 1]
    if thin:
        print(f"\n{len(thin)} channel(s) down to a SINGLE working link "
              "(no fallback left if it dies):")
        for n, _ in sorted(thin):
            print(f"  {n}")

    if no_source:
        print(f"\n{len(no_source)} channel(s) with NO working link:")
        for tid, n, is_parked in sorted(no_source, key=lambda x: x[1]):
            print(f"  {n}{'  (already parked)' if is_parked else ''}")

    dead_urls = sorted(u for u, s in results.items() if s == "dead")
    if dead_urls:
        from collections import Counter
        by_host = Counter(host_of(u) for u in dead_urls)
        print(f"\n{len(dead_urls)} dead link(s), by host:")
        for h, n in by_host.most_common():
            print(f"  {n:4d}  {h}")

    tripped = [h for h, n in _host_fails.items() if n >= HOST_FAIL_LIMIT]
    if tripped:
        print("\n!! host stopped answering partway through, results for it are "
              "incomplete: " + ", ".join(tripped))

    FORCE_SCAN = was_forced
    return no_source


def jp_lines_preview():
    """URL lines of JP.m3u — used only to report how many channels a SKIP_HOSTS
    entry is covering, before the real pass starts."""
    try:
        return [l.strip() for l in read(REPO / "YB.m3u").split("\n")
                if l.strip().startswith("http")]
    except Exception:                                  # noqa: BLE001
        return []


def run_once(args, state):
    reset_breakers()
    _scan_now.clear()
    load_state()
    skipped = [h for h in HOST_SCAN_INTERVAL
               if time.time() - _last_scan.get(h, 0) < HOST_SCAN_INTERVAL[h]
               and not FORCE_SCAN]
    for h in skipped:
        due = (HOST_SCAN_INTERVAL[h] - (time.time() - _last_scan.get(h, 0))) / 3600
        print(f"  (skipping scan of {h} — next in {due:.1f}h)")

    if SKIP_HOSTS:
        skipped_n = sum(1 for l in jp_lines_preview()
                        if any(s in l for s in SKIP_HOSTS))
        print(f"  (not probing {', '.join(sorted(SKIP_HOSTS))} — "
              f"{skipped_n} channel(s) left untouched)")

    for w in lint_playlists():                # structural problems, checked free
        print(f"  !! LINT: {w}")

    altsrc = build_alt_sources()
    jp_lines = read(REPO / "YB.m3u").split("\n")

    # rotating subset for batched hosts, so each sweep stays small
    _batch_allow.clear()
    all_urls = [u.strip() for u in
                (l[len(PARK_MARK):] if l.startswith(PARK_MARK) else l for l in jp_lines)
                if u.strip().startswith("http")]
    for hk in HOST_SCAN_BATCH:
        if hk in skipped:
            continue
        urls = sorted({u for u in all_urls if hk in host_of(u)})
        if urls:
            picked = batch_slice(hk, urls)
            _batch_allow.update(picked)
            if len(picked) < len(urls):
                print(f"  (checking {len(picked)} of {len(urls)} on {hk} this sweep)")

    # Restore any parked channel that has a working source again — checking ALL
    # its candidates, not just the url it was parked on, so a channel also comes
    # back when a NEW fallback host starts carrying it. unpark_urls maps the
    # parked url -> the url to restore it with (may be a different host).
    unpark_urls = {}
    if PARK_OFFLINE:
        parked = parked_meta(jp_lines)            # [(parked_url, tvg-id, name)]
        for u, tid, name in sorted(parked):
            st = check(u)
            good = u if st == "alive" else None
            if not good:                          # try the channel's other sources
                for c in candidates(tid, name, u, altsrc):
                    if c != u and check(c) == "alive":
                        good = c
                        break
            if args.verbose:
                where = "" if good in (None, u) else f" -> {good}"
                print(f"  parked-check [{'alive' if good else st}] {name}{where}")
            if good:
                unpark_urls[u] = good

    _, master = entries("\n".join(jp_lines))

    # health-check one channel at a time (sequential + throttled in check()),
    # so a host never gets a burst of requests and rate-limits/locks us out
    decisions = {}   # tvgid -> (current_url, chosen_url_or_None, kind)
    for _, tid, name, url in master:
        new, kind, st = decide_url(tid, name, url, altsrc, state)
        decisions[tid] = (url, new, kind)
        if args.verbose:
            tag = kind or ("no-source" if new is None else st)
            print(f"  [{tag}] {name}")

    swaps = [(t, o, n, k) for t, (o, n, k) in decisions.items() if n and n != o]
    heals = [(t, o, n) for t, o, n, k in swaps if k == "heal"]
    snaps = [(t, o, n) for t, o, n, k in swaps if k == "snapback"]
    pins = [(t, o, n) for t, o, n, k in swaps if k == "pin"]
    dead = [t for t, (o, n, k) in decisions.items() if n is None]
    park_urls = {decisions[t][0] for t in dead} if PARK_OFFLINE else set()

    print(f"checked {len(decisions)} active channels: {len(heals)} healed, "
          f"{len(snaps)} upgraded, {len(dead)} with no source"
          + (f"  |  parking {len(park_urls)}, restoring {len(unpark_urls)}"
             if PARK_OFFLINE else ""))
    for tid, o, n in heals:
        print(f"  HEAL {tid}\n     {o}\n  -> {n}")
    for tid, o, n in snaps:
        print(f"  UPGRADE {tid}\n     {o}\n  -> {n}")
    for tid, o, n in pins:
        print(f"  PINNED {tid}\n     {o}\n  -> {n}")
    if dead:
        print("  parking (no working source): " + ", ".join(sorted(dead)))
    save_state()                              # remember which hosts we scanned
    tripped = [h for h, n in _host_fails.items() if n >= HOST_FAIL_LIMIT]
    if tripped:
        print("  !! host refusing us (checks skipped, nothing changed): "
              + ", ".join(tripped))

    if args.check:
        print("(dry run — nothing written)")
        return 0

    changed_files = []
    for f in TARGETS:
        p = REPO / f
        if not p.exists():
            continue
        lines, es = entries(read(p))
        changed = False
        for ui, tid, name, url in es:
            d = decisions.get(tid)
            if d and d[1] and url == d[0] and d[1] != url:
                trail = "\r" if lines[ui].endswith("\r") else ""
                lines[ui] = d[1] + trail
                changed = True
        if PARK_OFFLINE and (park_urls or unpark_urls):
            npark, nunpark = park_unpark(lines, park_urls, unpark_urls)
            if npark or nunpark:
                changed = True
        if changed:
            write(p, "\n".join(lines))
            changed_files.append(f)

    if not changed_files:
        print("Everything alive — no changes.")
        return 0

    print(f"Updated {len(changed_files)} files.")
    if args.push:
        ok, out = push(changed_files,
                       f"autoheal: {len(heals)} healed, {len(snaps)} upgraded, "
                       f"{len(park_urls)} parked, {len(unpark_urls)} restored")
        print(("pushed to GitFlic" if ok else "PUSH FAILED") + (f": {out}" if out else ""))
        return 0 if ok else 2
    return 0


def main():
    ap = argparse.ArgumentParser(description="Self-healing links for JP playlists")
    ap.add_argument("--check", action="store_true", help="dry run; report only")
    ap.add_argument("--push", action="store_true", help="commit + push to GitFlic if changed")
    ap.add_argument("--loop", type=int, metavar="MIN", help="repeat every MIN minutes")
    ap.add_argument("--now", action="store_true",
                    help="upgrade working links to their best host immediately "
                         "(skip the 2-check anti-flap wait, this run only)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print the health status of every channel as it's checked")
    ap.add_argument("--audit", action="store_true",
                    help="scan every defined backup (fallback) link and report any "
                         "that are down; read-only. Meant to run daily.")
    ap.add_argument("--sweep", action="store_true",
                    help="check EVERY candidate link for EVERY channel, not just "
                         "the one in use; read-only. Slower than --audit (~20 min) "
                         "but catches fallbacks that have quietly died. Add -v for "
                         "a per-link breakdown.")
    ap.add_argument("--lint", action="store_true",
                    help="structural check only (duplicate tvg-ids, duplicated "
                         "streams, flat/categories drift); no network, no changes")
    args = ap.parse_args()

    if args.lint:
        problems = lint_playlists()
        if not problems:
            print("Playlists look structurally clean.")
        else:
            print(f"{len(problems)} problem(s):")
            for w in problems:
                print("  !!", w)
        return 0

    if args.sweep:
        load_state()
        sweep_all(verbose=args.verbose)
        return 0                                  # report-only; exit after

    if args.audit:
        return 0 if not audit_backups() else 0    # report-only; exit after

    if args.now:
        global CONFIRM, FORCE_SCAN
        CONFIRM = 1              # snap back on the first confirmed-alive check
        FORCE_SCAN = True        # ...and ignore per-host scan cooldowns

    state = {}   # per-channel snap-back streaks, kept across loop iterations
    if args.loop:
        print(f"autoheal looping every {args.loop} min. Ctrl-C to stop.")
        try:
            while True:
                print("\n--- " + time.strftime("%Y-%m-%d %H:%M:%S") + " ---")
                run_once(args, state)
                time.sleep(args.loop * 60)
        except KeyboardInterrupt:
            print("\nstopped.")
            return 0
    return run_once(args, state)


if __name__ == "__main__":
    sys.exit(main())
