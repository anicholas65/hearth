# Remote access with Tailscale

Hearth stays on your LAN at `http://192.168.1.64:8080`. Tailscale adds a private HTTPS address,
`https://hearth.<tailnet>.ts.net`, that only devices signed in to your tailnet can reach. HTTPS is what
lets the browser use the camera, install Hearth as an app (PWA) and receive push.

> **Never enable Funnel for Hearth.** Funnel publishes a service to the whole internet. Hearth holds
> your inventory, documents and finances and is designed for a private network. Both setups below keep
> Funnel off (`AllowFunnel: false` in the sidecar's `serve.json`).

## Option A — Unraid 7 native ("Use Tailscale" on the container)
1. Docker tab → Hearth → **Edit**.
2. Turn on **Use Tailscale**; set the **Tailscale hostname** to `hearth` (must be unique in your tailnet).
3. Choose **Serve** (not Funnel). Unraid takes the port from the container's WebUI setting (8080).
4. Apply, then approve the new machine in the Tailscale admin console if prompted. Enable **MagicDNS**
   and **HTTPS certificates** in the admin console (DNS page) once for your tailnet.
5. Open `https://hearth.<tailnet>.ts.net`. Settings → **Remote access** shows the detected address.

If the toggle fails on this image (it runs as `nobody`), use Option B.

## Option B — sidecar (`docker-compose.tailscale.yml`)
```sh
cp docker-compose.tailscale.yml tailscale/serve.json /mnt/user/appdata/hearth-compose/   # keep them together
cd /mnt/user/appdata/hearth-compose
TS_AUTHKEY=tskey-auth-XXXX HEARTH_PUBLIC_URL=https://hearth.<tailnet>.ts.net \
  docker compose -f docker-compose.tailscale.yml up -d
```
The sidecar joins the tailnet as `hearth`, serves HTTPS on 443 and proxies to `127.0.0.1:8080` inside the
shared network namespace. Port 8080 is still published for LAN use. Stop the plain Hearth container first
(both would claim 8080).

## Sign-in on the tailnet
Requests that arrive through Tailscale Serve carry `Tailscale-User-Login` and `Tailscale-User-Name`.
Hearth treats them as signed-in household members (the first one becomes the owner) and skips the LAN
basic-auth prompt.

**Trust rule:** these headers are honoured **only when the connection comes from loopback**, which is
where Serve connects from in both setups. On any other connection, including LAN clients on
`192.168.1.64:8080`, Hearth strips them before anything else runs, so they can't be forged. For the same
reason Uvicorn runs without `--proxy-headers`: the peer address can't be faked with `X-Forwarded-For`.
Don't put another reverse proxy in front of Hearth on the same host without revisiting this.

The LAN address keeps its existing behaviour (open, or basic auth if `HEARTH_USERNAME`/`HEARTH_PASSWORD`
are set).

## Pairing the Android app
Settings → **Remote access & devices** → **Pair a device** shows a QR code containing
`{"server":"https://hearth.<tailnet>.ts.net","pairCode":"XXXXXXXX"}`. The code works once and expires
after 10 minutes. The app calls `POST /api/v1/pair` and receives a device token; every device is listed
in Settings and can be revoked there. Tokens are stored only as SHA-256 hashes.

If the server address shown is wrong (for example with the sidecar, where Hearth can't see the
Tailscale CLI), set **Server URL for devices** in the same card, or `HEARTH_PUBLIC_URL`.

## Push
Phones subscribe to your ntfy topic (Settings → Remote access → ntfy server/topic, or the ntfy URL in
`APPRISE_URLS`). The app reads it from `GET /api/v1/notify-config`. Use a long, unguessable topic name.
