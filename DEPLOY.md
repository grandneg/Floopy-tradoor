# Deploying Fruit Fly TV

The site is one page (`fruit-fly-tv.html`) plus a small Python server (`serve.py`) that checks whether the
channel is live, lists its uploads and fetches the connectome data. The server must run somewhere; a static
host (GitHub Pages, Netlify) is not enough.

Files in this folder:

| File | Used by |
|---|---|
| `Dockerfile`, `.dockerignore` | Render, Railway, Fly.io, any Docker host |
| `render.yaml` | Render (Blueprint) |
| `Procfile` | Railway / Heroku-style hosts without Docker |
| `fly.toml` | Fly.io |
| `start-fly-tv.bat` | running it on your own Windows PC |

## Render (simplest)

1. Put this folder in a GitHub repository.
2. In Render: **New → Blueprint**, choose the repo. It reads `render.yaml` and builds the Dockerfile.
3. When the deploy finishes you get `https://fruit-fly-tv.onrender.com` (name may differ).

The free plan sleeps after 15 minutes without visitors; the first visit afterwards takes ~30 s to wake up
and re-download the brain data (about 10 MB).

## Railway

1. **New Project → Deploy from GitHub repo**, choose the repo. Railway detects the Dockerfile.
2. **Settings → Networking → Generate Domain**.

## Fly.io

```bash
fly launch --copy-config --yes
fly deploy
```

## Any Docker host / your own server

```bash
docker build -t fruit-fly-tv .
docker run -d -p 80:8765 --name fruit-fly-tv fruit-fly-tv
```

## Going public: put Cloudflare in front (free)

The server caches everything and fetches each upstream value once per expiry, but with many viewers the
sheer number of polls (about 20 small requests per viewer per minute) is what overloads a small host.
Cloudflare's free plan answers those from its edge, using the `Cache-Control: s-maxage` headers the
server sends, so the host only sees cache refreshes.

You need a domain you own (a few dollars a year from any registrar; Cloudflare also sells them).

1. **Cloudflare**: sign up at cloudflare.com, *Add a site*, enter your domain, pick the Free plan, and
   change your domain's nameservers at the registrar to the two Cloudflare gives you.
2. **Render**: open your service, *Settings → Custom Domains → Add*, enter `fly.yourdomain.com` (or the
   bare domain). Render shows a CNAME target like `fruit-fly-tv.onrender.com`.
3. **Cloudflare DNS**: add a `CNAME` record, name `fly` (or `@`), target that `…onrender.com` address,
   proxy status **Proxied** (orange cloud). Under *SSL/TLS* set the mode to **Full**.
4. **Cache the polls**: *Caching → Cache Rules → Create rule*. Match `URI Path starts with /api/`
   **or** `URI Path starts with /hls/`, set *Cache eligibility* to **Eligible for cache** and *Edge TTL* to
   **Use cache-control header from origin**. That makes Cloudflare honour the `s-maxage` values (1 s for
   the stream playlist, 5–45 s for data, a week for brain files). Optionally a second rule for the page
   itself (`URI Path equals /`) with the same settings.
5. Wait for the nameserver change (minutes to a day), then open `https://fly.yourdomain.com`.

After editing the page or the coin, purge Cloudflare's cache (*Caching → Configuration → Purge
Everything*) so viewers get the new version right away rather than after the cache expires.

## Allowlist

The server only answers for the coin, streamers and neurons named in `fruit-fly-tv.html`, so nobody can
use it as a relay to the data providers. To allow extra coins or channels without editing the page (for
example to use `?coin=` links), set the environment variables `ALLOWED_COINS=addr1,addr2` and
`ALLOWED_CHANNELS=login1,login2` on the host. The page is re-read whenever it changes, no restart needed.

## Things to know

- **HTTPS**: Render, Railway and Fly provide it. The YouTube embed works on plain http too, but browsers
  are stricter about autoplay and sound on http.
- **Sound** still starts muted for every visitor until they tap once. Browsers require that.
- **How the TV plays Twitch**: the server fetches each channel's stream playlist the way third-party
  players do (an anonymous playback token from Twitch, then the playlist) and relays the small, constantly
  refreshing playlist file to viewers (Twitch ties a playback session to the server's IP, so the browser
  can't fetch it itself). The video segments named in that playlist are loaded by each viewer's browser
  straight from Twitch's CDN with hls.js and drawn onto the 3D screen, so video bandwidth does not pass
  through the server. If Twitch changes that internal API the live check still works but the TV will show
  the standby screen; the fix would be in `fetch_hls` / `hls_playlist` in `serve.py`.
- **Environment variables** (all optional): `PORT` (set by the host), `HOST` (bind address), `CACHE_DIR`
  (where the downloaded brain files go; a persistent disk avoids re-downloading after restarts).
- **Changing the streamers or the coin**: edit the `STREAMERS` list and the `COIN` line near the top of
  `fruit-fly-tv.html` and redeploy, or add `?coin=<token address>` to the URL for one visit. A real coin
  gets its price, market cap and volume from DexScreener and its one-minute candles and live trades from
  GeckoTerminal; both are free public APIs with no key. GeckoTerminal allows about 30 calls a minute, the
  server uses about 13 and shares them between all viewers, and serves its last good copy if it is ever
  rate-limited.
