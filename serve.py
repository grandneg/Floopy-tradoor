"""Serves the Fruit Fly TV page and answers a few small JSON endpoints:

  /api/twitch?channels=a,b,c   -> which Twitch streamers are live (title, viewers)
  /api/hls?channel=<login>     -> the channel's stream playlists (for the 3D TV)
  /api/token?mint=<address>    -> live stats for a Solana token (DexScreener)
  /api/chart, /api/trades      -> one-minute candles and recent trades (GeckoTerminal)
  /api/brain, /api/neuron      -> connectome data (Virtual Fly Brain), cached on disk

The browser cannot read those sites itself (cross-origin), so this little server does it.
Only the coin, streamers and neurons configured in fruit-fly-tv.html are served (plus anything in the
ALLOWED_COINS / ALLOWED_CHANNELS environment variables), so the server can't be used as an open relay.
Every upstream result is cached and fetched once per expiry no matter how many viewers ask, and responses
carry Cache-Control headers a CDN such as Cloudflare can use to serve viewers from its edge.
Run:  python serve.py   then open http://localhost:8765/
"""
import json
import os
import random
import re
import sys
import threading
import urllib.error
import time
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "fruit-fly-tv.html")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SOL_MINT = "So11111111111111111111111111111111111111112"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- shared cache: one upstream fetch per expiry
_cache = {}          # key -> (timestamp, result)
_inflight = {}       # key -> threading.Event while one thread is fetching it
_cache_lock = threading.Lock()


def cached_call(key, ttl, fn, arg):
    """Return (result, from_cache). The first request past expiry fetches; everyone else arriving meanwhile
    waits for that one result instead of hitting upstream too. If upstream fails and an old copy exists,
    the old copy is served and the next retry is held back for 5 seconds."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1], True
        ev = _inflight.get(key)
        leader = ev is None
        if leader:
            ev = _inflight[key] = threading.Event()
    if not leader:
        ev.wait(timeout=25)
        with _cache_lock:
            hit = _cache.get(key)
        if hit:
            return hit[1], True
        raise RuntimeError("upstream fetch failed")
    try:
        result = fn(arg)
        with _cache_lock:
            _cache[key] = (time.time(), result)
        return result, False
    except Exception:
        with _cache_lock:
            hit = _cache.get(key)
            if hit:
                _cache[key] = (time.time() - ttl + 5, hit[1])
        if hit:
            log(f"{key[0]} {key[1]}: upstream failed, serving the last good copy")
            return hit[1], True
        raise
    finally:
        with _cache_lock:
            _inflight.pop(key, None)
        ev.set()


# ---------------------------------------------------------------- what the page is allowed to ask for
ALLOWED = {"coins": {SOL_MINT}, "channels": set(), "neurons": set()}
_page_mtime = 0.0


def load_allowlist():
    """Read the coin, streamers and neuron ids out of fruit-fly-tv.html (re-read whenever the file changes)."""
    global _page_mtime
    try:
        mtime = os.path.getmtime(PAGE)
    except OSError:
        return
    if mtime == _page_mtime:
        return
    _page_mtime = mtime
    with open(PAGE, encoding="utf-8", errors="replace") as f:
        src = f.read()
    coins = set(re.findall(r"mint:\s*'([1-9A-HJ-NP-Za-km-z]{32,44})'", src))
    coins |= {c.strip() for c in os.environ.get("ALLOWED_COINS", "").split(",") if c.strip()}
    coins.add(SOL_MINT)
    m = re.search(r"const STREAMERS\s*=\s*\[([^\]]*)\]", src)
    channels = {c.lower() for c in re.findall(r"'([A-Za-z0-9_]{1,25})'", m.group(1))} if m else set()
    channels |= {c.strip().lower() for c in os.environ.get("ALLOWED_CHANNELS", "").split(",") if c.strip()}
    neurons = set(re.findall(r"id:\s*'(VFB_[0-9a-z]{8})'", src))
    ALLOWED.update(coins=coins, channels=channels, neurons=neurons)
    log(f"allowlist: {len(coins) - 1} coin(s), {len(channels)} streamer(s), {len(neurons)} neurons")


# ---------------------------------------------------------------- connectome data (via Virtual Fly Brain)
# Neuron skeletons and the template brain surface come from virtualflybrain.org, which hosts the FlyWire
# (FAFB) whole-brain connectome neurons registered to the JRC2018 unisex template. Files are cached on disk.
VFB = "https://www.virtualflybrain.org/data/VFB/i"
TEMPLATE = "VFB_00101567"          # JRC2018Unisex adult brain template
THIN_VERSION = 3                   # bump when thin_swc changes so cached thinned copies are rebuilt
CACHE_DIR = os.environ.get("CACHE_DIR") or os.path.join(HERE, "cache")


def cached_file(name, url):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, name)
    if not os.path.exists(path):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        with open(path, "wb") as f:
            f.write(data)
    with open(path, "rb") as f:
        return f.read()


def thin_swc(text, max_nodes=3500):
    """Reduce an SWC skeleton to roughly max_nodes while keeping its branching structure.
    Root, branch points and tips are always kept; long unbranched runs keep every k-th node."""
    nodes, order = {}, []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        p = line.split()
        if len(p) < 7:
            continue
        nid, parent = int(p[0]), int(p[6])
        nodes[nid] = (p[2], p[3], p[4], parent)
        order.append(nid)
    n = len(order)
    if n <= max_nodes:
        return "\n".join(f"{i} 0 {x} {y} {z} 1 {pr}" for i, (x, y, z, pr) in nodes.items())
    step = -(-n // max_nodes)  # ceil

    def count_children():
        c = {}
        for nid in order:
            pr = nodes[nid][3]
            if pr != -1:
                c[pr] = c.get(pr, 0) + 1
        return c

    # Very branchy neurons keep too many tips/branch points, so first prune short terminal twigs
    # (shorter than ~step nodes), a few passes, until the skeleton is near the budget.
    children = count_children()
    for _ in range(4):
        if len(order) <= max_nodes * 3:
            break
        min_len = max(3, step // 3)
        drop = set()
        for nid in order:
            if children.get(nid, 0) != 0 or nid in drop:
                continue
            chain, cur = [], nid
            while cur != -1 and cur in nodes and children.get(cur, 0) <= 1 and nodes[cur][3] != -1:
                chain.append(cur)
                cur = nodes[cur][3]
                if len(chain) >= min_len:
                    break
            if len(chain) < min_len:
                drop.update(chain)
        if not drop:
            break
        for nid in drop:
            del nodes[nid]
        order = [nid for nid in order if nid not in drop]
        children = count_children()
    n = len(order)
    step = max(1, -(-n // max_nodes))
    keep = {nid for nid in order if nodes[nid][3] == -1 or children.get(nid, 0) != 1}
    run = {}  # distance (in nodes) from the last kept ancestor; SWC lists parents before children
    for nid in order:
        if nid in keep:
            run[nid] = 0
            continue
        d = run.get(nodes[nid][3], 0) + 1
        if d >= step:
            keep.add(nid)
            d = 0
        run[nid] = d
    newid = {nid: i + 1 for i, nid in enumerate(k for k in order if k in keep)}

    def anc(nid):
        cur = nodes[nid][3]
        while cur != -1 and cur in nodes and cur not in keep:
            cur = nodes[cur][3]
        return cur if (cur != -1 and cur in nodes) else -1

    out = []
    for nid in order:
        if nid not in keep:
            continue
        x, y, z, _ = nodes[nid]
        pr = anc(nid)
        out.append(f"{newid[nid]} 0 {x} {y} {z} 1 {newid[pr] if pr != -1 else -1}")
    return "\n".join(out)


def neuron_swc(vfb_id, max_nodes=3500):
    if not re.fullmatch(r"VFB_[0-9a-z]{8}", vfb_id):
        raise ValueError("bad VFB id")
    max_nodes = max(300, min(int(max_nodes), 20000))
    lite = os.path.join(CACHE_DIR, f"{vfb_id}.thin{max_nodes}.v{THIN_VERSION}.swc")   # pruned twigs + subsampled
    if os.path.exists(lite):
        with open(lite, "rb") as f:
            return f.read()
    raw = cached_file(f"{vfb_id}.swc", f"{VFB}/{vfb_id[4:8]}/{vfb_id[8:12]}/{TEMPLATE}/volume.swc")
    data = thin_swc(raw.decode("utf-8", "replace"), max_nodes).encode()
    with open(lite, "wb") as f:
        f.write(data)
    return data


def decimate_obj(text, cell):
    """Vertex-clustering decimation of a triangle OBJ: vertices in the same `cell`-micron grid box merge."""
    verts, faces = [], []
    for line in text.splitlines():
        if line.startswith("v "):
            a = line.split()
            verts.append((float(a[1]), float(a[2]), float(a[3])))
        elif line.startswith("f "):
            a = line.split()
            faces.append([int(x.split("/")[0]) for x in a[1:4]])
    cell_of, acc, vmap = {}, [], [0] * len(verts)
    for i, (x, y, z) in enumerate(verts):
        key = (int(x // cell), int(y // cell), int(z // cell))
        j = cell_of.get(key)
        if j is None:
            j = len(acc)
            cell_of[key] = j
            acc.append([0.0, 0.0, 0.0, 0])
        a = acc[j]
        a[0] += x; a[1] += y; a[2] += z; a[3] += 1
        vmap[i] = j
    out = ["v %.2f %.2f %.2f" % (a[0] / a[3], a[1] / a[3], a[2] / a[3]) for a in acc]
    seen = set()
    for f in faces:
        t = tuple(vmap[i - 1] + 1 for i in f)
        if len(set(t)) < 3:
            continue
        k = tuple(sorted(t))
        if k in seen:
            continue
        seen.add(k)
        out.append("f %d %d %d" % t)
    return "\n".join(out)


def brain_obj(lod="full"):
    full = cached_file("brain_JRC2018U.obj", f"{VFB}/0010/1567/{TEMPLATE}/volume.obj")
    cell = {"low": 5.0, "med": 2.5}.get(lod)
    if not cell:
        return full
    path = os.path.join(CACHE_DIR, f"brain_JRC2018U.{lod}.obj")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    data = decimate_obj(full.decode("utf-8", "replace"), cell).encode()
    with open(path, "wb") as f:
        f.write(data)
    return data


# ---------------------------------------------------------------- Twitch live status + coin data
TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"   # the Twitch website's own public client id
TWITCH_CACHE_SECONDS = 45
TOKEN_CACHE_SECONDS = 10


def fetch_twitch(channels):
    """Which of these Twitch channels are live right now (title, viewers, category)."""
    logins = [c.strip().lower() for c in channels.split(",") if re.fullmatch(r"[A-Za-z0-9_]{1,25}", c.strip())][:20]
    if not logins:
        raise ValueError("no channels")
    query = ("query { users(logins: %s) { login displayName profileImageURL(width: 70) "
             "stream { id title viewersCount createdAt game { displayName } } } }" % json.dumps(logins))
    req = urllib.request.Request(TWITCH_GQL, data=json.dumps([{"query": query}]).encode(), headers={
        "Client-Id": TWITCH_CLIENT_ID, "Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    users = data[0]["data"]["users"]
    streams = []
    for login, u in zip(logins, users):
        if not u:
            streams.append({"login": login, "name": login, "live": False, "title": "", "viewers": 0, "game": ""})
            continue
        s = u.get("stream")
        streams.append({
            "login": u["login"], "name": u["displayName"], "avatar": u.get("profileImageURL"),
            "live": bool(s), "title": s["title"] if s else "", "viewers": s["viewersCount"] if s else 0,
            "game": ((s.get("game") or {}).get("displayName", "") if s else ""), "since": s["createdAt"] if s else None,
        })
    return {"streams": streams, "checkedAt": int(time.time())}


def fetch_hls(channel):
    """The channel's live HLS variants (resolution, bitrate, playlist url), the way third-party players get them:
    an anonymous playback token from Twitch's GQL, then the master playlist from usher. The variant playlists
    and segments themselves allow cross-origin loads, so the browser streams them directly with hls.js."""
    login = channel.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,25}", login):
        raise ValueError("bad channel")
    body = json.dumps({
        "operationName": "PlaybackAccessToken",
        "variables": {"isLive": True, "login": login, "isVod": False, "vodID": "", "playerType": "embed"},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "0828119ded1c13477966434e15800ff57ddacf13ba1911c129dc2200705b0712"}},
    }).encode()
    req = urllib.request.Request(TWITCH_GQL, data=body, headers={
        "Client-Id": TWITCH_CLIENT_ID, "Content-Type": "application/json", "User-Agent": UA,
        "Device-ID": "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=32))})
    with urllib.request.urlopen(req, timeout=20) as r:
        tok = json.loads(r.read().decode("utf-8", "replace"))["data"]["streamPlaybackAccessToken"]
    if not tok:
        return {"login": login, "live": False, "variants": []}
    q = urllib.parse.urlencode({
        "client_id": TWITCH_CLIENT_ID, "token": tok["value"], "sig": tok["signature"], "allow_source": "true",
        "allow_audio_only": "true", "fast_bread": "true", "player_backend": "mediaplayer",
        "playlist_include_framerate": "true", "reassignments_supported": "true", "p": random.randint(1000000, 9999999)})
    req = urllib.request.Request(f"https://usher.ttvnw.net/api/channel/hls/{login}.m3u8?{q}", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            master = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:   # usher answers 404 when the channel is offline
            return {"login": login, "live": False, "variants": []}
        raise
    variants, info = [], None
    for line in master.splitlines():
        if line.startswith("#EXT-X-STREAM-INF"):
            info = line
        elif line.startswith("http") and info is not None:
            res = re.search(r"RESOLUTION=(\d+)x(\d+)", info)
            bw = re.search(r"BANDWIDTH=(\d+)", info)
            fr = re.search(r"FRAME-RATE=([\d.]+)", info)
            if res:   # skip audio-only
                variants.append({"width": int(res.group(1)), "height": int(res.group(2)),
                                 "kbps": int(bw.group(1)) // 1000 if bw else 0, "fps": float(fr.group(1)) if fr else 0, "url": line})
            info = None
    variants.sort(key=lambda v: v["height"])
    return {"login": login, "live": bool(variants), "variants": variants, "checkedAt": int(time.time())}


# A Twitch playback session (the set of variant playlist URLs usher hands out) must be kept for the whole
# viewing: opening a new one restarts the stream for every viewer and begins with a pre-roll slate. So one
# session per channel is held for hours and only reopened when Twitch rejects it.
_sessions = {}      # login -> {"info": ..., "at": time}
_adstate = {}       # login -> epoch seconds until which the stream is showing an ad slate
SESSION_SECONDS = 4 * 3600
_session_lock = threading.Lock()


def hls_session(login, force=False):
    with _session_lock:
        s = _sessions.get(login)
        if s and not force and time.time() - s["at"] < SESSION_SECONDS:
            return s["info"]
        info = fetch_hls(login)
        if info.get("variants"):          # never remember "offline": the next ask should look again
            _sessions[login] = {"info": info, "at": time.time()}
        else:
            _sessions.pop(login, None)
        return info


def note_ads(login, playlist):
    """Remember until when the playlist is in a stitched-ad break (the purple 'preparing your stream' slate)."""
    from datetime import datetime
    until = 0
    for m in re.finditer(r'#EXT-X-DATERANGE:[^\n]*CLASS="twitch-stitched-ad"[^\n]*', playlist):
        line = m.group(0)
        sd = re.search(r'START-DATE="([^"]+)"', line)
        du = re.search(r"DURATION=([\d.]+)", line)
        if sd and du:
            try:
                start = datetime.fromisoformat(sd.group(1).replace("Z", "+00:00")).timestamp()
                until = max(until, start + float(du.group(1)))
            except Exception:
                pass
    _adstate[login] = until


def ad_state(login):
    remaining = max(0, _adstate.get(login, 0) - time.time())
    return {"login": login, "adSecondsLeft": int(remaining), "checkedAt": int(time.time())}


def hls_playlist(key):
    """One variant's live media playlist, fetched by the server. Twitch binds a playback session to the IP that
    opened it, so the viewer's browser can't load this playlist itself when the server lives elsewhere; the
    video segments inside it are plain CDN URLs and are loaded directly by the viewer."""
    login, height = key.split("|")
    want = int(height)
    for attempt in (0, 1):
        info = hls_session(login, force=(attempt == 1))
        variants = info.get("variants") or []
        if not variants:
            raise ValueError("channel is offline")
        v = next((x for x in variants if x["height"] == want), None) or max((x for x in variants if x["height"] <= want), key=lambda x: x["height"], default=variants[0])
        try:
            text = get(v["url"])
        except urllib.error.HTTPError as e:
            if attempt == 0 and e.code in (403, 404, 410):   # Twitch dropped this session: open a fresh one
                log(f"/hls {login}: session expired ({e.code}), reopening")
                continue
            raise
        note_ads(login, text)
        return text


def fetch_token(mint):
    """Live stats for a Solana token from DexScreener (public, no key): best pair by liquidity."""
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint):
        raise ValueError("bad token address")
    data = json.loads(get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}"))
    pairs = data.get("pairs") or []
    if not pairs:
        raise ValueError("no trading pairs found for that token")
    p = max(pairs, key=lambda x: ((x.get("liquidity") or {}).get("usd") or 0))
    g = lambda *ks: (lambda d: [d := (d or {}).get(k) for k in ks][-1])(p)  # nested get
    return {
        "symbol": (p.get("baseToken") or {}).get("symbol", ""), "name": (p.get("baseToken") or {}).get("name", ""),
        "priceUsd": float(p.get("priceUsd") or 0), "priceNative": float(p.get("priceNative") or 0),
        "marketCap": p.get("marketCap") or p.get("fdv") or 0, "liquidityUsd": g("liquidity", "usd") or 0,
        "volume24h": g("volume", "h24") or 0, "volume5m": g("volume", "m5") or 0,
        "buys5m": g("txns", "m5", "buys") or 0, "sells5m": g("txns", "m5", "sells") or 0,
        "change5m": g("priceChange", "m5") or 0, "change1h": g("priceChange", "h1") or 0, "change24h": g("priceChange", "h24") or 0,
        "dex": p.get("dexId", ""), "pair": p.get("pairAddress", ""), "url": p.get("url", ""),
        "checkedAt": int(time.time()),
    }


# ---------------------------------------------------------------- real candles + trades (GeckoTerminal, public API)
GT = "https://api.geckoterminal.com/api/v2/networks/solana"
_pools = {}   # mint -> (timestamp, pool address)


def gt_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def gt_pool(mint):
    """The token's main pool on GeckoTerminal (cached for 10 minutes)."""
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint):
        raise ValueError("bad token address")
    hit = _pools.get(mint)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    pools = gt_get(f"{GT}/tokens/{mint}/pools").get("data") or []
    if not pools:
        raise ValueError("GeckoTerminal has no pool for that token yet")
    pool = pools[0]["attributes"]["address"]
    _pools[mint] = (time.time(), pool)
    return pool


def fetch_chart(mint):
    """Last 120 one-minute candles (USD): [timestamp, open, high, low, close, volume_usd], oldest first."""
    pool = gt_pool(mint)
    d = gt_get(f"{GT}/pools/{pool}/ohlcv/minute?aggregate=1&limit=120")
    lst = d["data"]["attributes"]["ohlcv_list"]
    candles = sorted(([int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] for c in lst), key=lambda c: c[0])
    meta = d.get("meta", {})
    return {"pool": pool, "base": (meta.get("base") or {}).get("symbol"), "candles": candles, "checkedAt": int(time.time())}


def fetch_trades(mint):
    """The most recent trades in the pool: side, USD size, token price, token amount, wallet."""
    from datetime import datetime
    pool = gt_pool(mint)
    d = gt_get(f"{GT}/pools/{pool}/trades")
    out = []
    for t in (d.get("data") or [])[:80]:
        a = t["attributes"]
        sell = a.get("kind") == "sell"
        try:
            ts = int(datetime.fromisoformat(a["block_timestamp"].replace("Z", "+00:00")).timestamp())
        except Exception:
            ts = int(time.time())
        out.append({
            "ts": ts, "side": "sell" if sell else "buy",
            "usd": float(a.get("volume_in_usd") or 0),
            "price": float((a.get("price_from_in_usd") if sell else a.get("price_to_in_usd")) or 0),
            "tokens": float((a.get("from_token_amount") if sell else a.get("to_token_amount")) or 0),
            "wallet": a.get("tx_from_address") or "", "tx": a.get("tx_hash") or t.get("id"),
        })
    out.sort(key=lambda x: x["ts"], reverse=True)
    return {"pool": pool, "trades": out, "checkedAt": int(time.time())}


def log(msg):
    try:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}")
    except Exception:
        pass


API = {   # path -> (fetcher, query parameter, cache seconds, allowlist bucket)
    "/api/twitch": (fetch_twitch, "channels", TWITCH_CACHE_SECONDS, "channels"),
    "/api/hls": (hls_session, "channel", 20, "channels"),
    "/api/adstate": (ad_state, "channel", 2, "channels"),
    "/api/token": (fetch_token, "mint", TOKEN_CACHE_SECONDS, "coins"),
    "/api/chart": (fetch_chart, "mint", 15, "coins"),
    "/api/trades": (fetch_trades, "mint", 5, "coins"),
}


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive: viewers poll every few seconds, no need for a new connection each time

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        load_allowlist()
        # Only the page itself is public; the folder (server code, cache) is never listed or served.
        if parsed.path in ("/", "/index.html", "/fruit-fly-tv.html"):
            return self._file(PAGE, "text/html; charset=utf-8", "public, max-age=0, s-maxage=120")
        if parsed.path == "/healthz":
            return self._text(b"ok")
        # site icon + social preview images, from the logo/ folder next to this file
        asset = {"/favicon.ico": "favicon-32.png", "/favicon.png": "favicon-32.png", "/apple-touch-icon.png": "apple-touch-icon.png",
                 "/og.png": "og-1200x630.png", "/logo.png": "fly-logo-256.png", "/logo.svg": "fly-logo.svg",
                 "/banner.png": "floopytradoor-banner-1500x500.png", "/pfp.png": "floopytradoor-pfp-1024.png"}.get(parsed.path)
        if asset:
            ctype = "image/svg+xml" if asset.endswith(".svg") else "image/png"
            return self._file(os.path.join(HERE, "logo", asset), ctype, "public, max-age=86400, s-maxage=604800")
        m = re.fullmatch(r"/hls/([a-z0-9_]{1,25})/(\d{2,4})\.m3u8", parsed.path)
        if m:   # the live playlist, relayed for the viewer (see hls_playlist)
            login, height = m.group(1), m.group(2)
            if login not in ALLOWED["channels"]:
                return self._json({"error": "not one of the streamers configured in fruit-fly-tv.html"}, 403)
            try:
                text, _ = cached_call(("/hls", f"{login}|{height}"), 1, hls_playlist, f"{login}|{height}")
            except Exception as exc:
                log(f"/hls {login}: ERROR {type(exc).__name__}: {exc}")
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 502)
            return self._text(text.encode(), "application/vnd.apple.mpegurl", 200, "public, max-age=0, s-maxage=1")
        q = urllib.parse.parse_qs(parsed.query)
        if parsed.path in ("/api/brain", "/api/neuron"):
            try:
                if parsed.path == "/api/brain":
                    data = brain_obj(q.get("lod", ["full"])[0])
                else:
                    vid = q.get("id", [""])[0].strip()
                    if vid not in ALLOWED["neurons"]:
                        return self._json({"error": "that neuron is not part of this page"}, 403)
                    data = neuron_swc(vid, q.get("max", ["3500"])[0])
            except Exception as exc:
                log(f"{parsed.path}: ERROR {type(exc).__name__}: {exc}")
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 502)
            return self._text(data, "text/plain; charset=utf-8", 200, "public, max-age=86400, s-maxage=604800")
        if parsed.path in API:
            fn, param, ttl, bucket = API[parsed.path]
            arg = q.get(param, [""])[0].strip()
            if not arg:
                return self._json({"error": f"missing {param}"}, 400)
            wanted = {c.strip().lower() for c in arg.split(",") if c.strip()} if bucket == "channels" else {arg}
            if not wanted or not wanted <= ALLOWED[bucket]:
                return self._json({"error": "not one of the coins/streamers configured in fruit-fly-tv.html"}, 403)
            try:
                result, from_cache = cached_call((parsed.path, arg), ttl, fn, arg)
            except Exception as exc:
                log(f"{parsed.path} {arg}: ERROR {type(exc).__name__}: {exc}")
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 502)
            if parsed.path == "/api/twitch" and not from_cache:
                log("twitch: " + ", ".join(f"{s['login']}={'LIVE ' + str(s['viewers']) if s['live'] else 'off'}" for s in result["streams"]))
            return self._json(result, 200, f"public, max-age=0, s-maxage={ttl}, stale-while-revalidate=30")
        return self._json({"error": "not found"}, 404)

    def _json(self, obj, status=200, cache="no-store"):
        self._text(json.dumps(obj).encode(), "application/json", status, cache if status == 200 else "no-store")

    def _text(self, body, ctype="text/plain; charset=utf-8", status=200, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype, cache="no-cache"):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"error": "file missing"}, 404)
        self._text(body, ctype, 200, cache)

    def log_message(self, fmt, *args):  # keep the console quiet: only report errors on static files
        text = " ".join(str(a) for a in args)
        if "/api/" in text or "/hls/" in text or "favicon.ico" in text:
            return
        if "404" in text or "code" in fmt:
            super().log_message(fmt, *args)


if __name__ == "__main__":
    # stream titles often contain emoji; don't let the Windows console encoding crash a request
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    # Port: command-line argument > PORT env var (set by Render/Railway/Fly/Docker) > 8765.
    # Host: 127.0.0.1 when run by hand on your own machine; all interfaces when a host sets PORT.
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", PORT))
    host = os.environ.get("HOST") or ("0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
    load_allowlist()
    print(f"Fruit Fly TV  ->  http://localhost:{port}/   (Ctrl+C to stop)")
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.serve_forever()
