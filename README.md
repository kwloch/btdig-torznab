# btdig-torznab

**Torznab bridge for btdig.com** — makes [BTDig](https://btdig.com/) available as a Torznab indexer for Sonarr, Radarr, Lidarr, etc.

BTDig is a DHT search engine that indexes active torrents from the BitTorrent DHT network. Rich in Polish-language content that other trackers miss. No registration, no API key, no rate limiting for residential IPs.

## Why this exists

Jackett doesn't support btdig.com — no native indexer, and Cardigann definitions fail to parse the HTML (nested `display:table` layout confuses the HTML Agility Pack parser). This bridge does one job: take a Torznab search request, scrape btdig.com, and return standard Torznab XML.

## Architecture

```
Sonarr/Radarr → http://192.168.1.67:5555/torznab/api?t=search&q=...
                     ↓
            btdig-torznab (Python)
                     ↓
            https://btdig.com/search?q=...
                     ↓
            Parse HTML → Torznab XML
```

- **Zero external dependencies** — Python 3 stdlib only (`http.server`, `urllib`, `html.parser`)
- **Single-file** — one `btdig_torznab.py` script, one systemd unit
- **Configurable via env vars** — port, bind, cache TTL
- **Runs as `btdig-torznab` user** — least privilege

## Torznab API

| Endpoint | Method | Params |
|----------|--------|--------|
| `/torznab/api` | GET | `t=search&q=<query>` |
| `/torznab/api` | GET | `t=tvsearch&q=<query>&season=&ep=` |
| `/torznab/api` | GET | `t=movie&q=<query>` |
| `/torznab/api` | GET | `t=caps` (capabilities) |

Returns standard Torznab RSS+XML with magnet links, sizes, seeders, and upload date.

## Fields extracted from btdig

| Field | Source |
|-------|--------|
| Title | `div.torrent_name a` (text) |
| Magnet link | `div.torrent_magnet a[href^='magnet:']` |
| Size | `span.torrent_size` |
| Age | `span.torrent_age` (parsed as relative date) |
| btdig detail link | `div.torrent_name a[href]` |
| Seeders | 0 (btdig doesn't show live seeder counts) |

## Deployment

```
# 1. Copy files to LXC 100 (dockbox, 192.168.1.67)
# 2. Install systemd service
# 3. Start + enable
# 4. Add as Torznab indexer in Sonarr/Radarr
```

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `BTDIG_HOST` | `0.0.0.0` | Bind address |
| `BTDIG_PORT` | `5555` | Listen port |
| `BTDIG_CACHE_TTL` | `300` | Cache TTL in seconds |
| `BTDIG_URL` | `https://btdig.com` | BTDig base URL |

## ext.to Torznab bridge

This repository also includes **ext_to_torznab.py** — a separate Torznab bridge
for [EXT Torrents](https://ext.to), one of the most active general-purpose
torrent indexers in 2026 (carrying much of the former TPG user base).

### Differences from btdig bridge

| Aspect | btdig-torznab | ext-to-torznab |
|--------|---------------|----------------|
| Dependencies | None (stdlib) | `requests`, `beautifulsoup4` |
| Cloudflare | No protection | Managed challenge (requires FlareSolverr) |
| Magnet links | In HTML | HMAC-signed API |
| Seeders/Peers | Not shown | Available |
| Categories | Generic | Rich (Movies, TV, Music, Books, Games, etc.) |
| Port | 5555 | 5556 |
| On-demand magnet | No | Yes (`t=download`) |

### Architecture

```
Sonarr/Radarr → http://host:5556/torznab/api?t=search&q=...
                     ↓
            ext_to_torznab.py (Python)
                     ↓
            FlareSolverr (headless Chrome)
                     ↓
            ext.to (Cloudflare-protected)
                     ↓
            Parse HTML → Resolve magnets via HMAC API → Torznab XML
```

### Quick start (Docker Compose)

```yaml
# docker-compose.yml
services:
  flaresolverr:
    image: ghcr.io/flaresolverr/flaresolverr:latest
    container_name: flaresolverr
    restart: unless-stopped
    ports:
      - "8191:8191"
    environment:
      - LOG_LEVEL=info

  ext-to-torznab:
    image: python:3.11-slim
    container_name: ext-to-torznab
    restart: unless-stopped
    ports:
      - "5556:5556"
    volumes:
      - ./ext_to_torznab.py:/app/ext_to_torznab.py:ro
    working_dir: /app
    command: >
      sh -c "pip install --quiet requests beautifulsoup4 &&
             python3 ext_to_torznab.py"
    environment:
      - FLARESOLVERR_URL=http://flaresolverr:8191
      - EXT_TO_PORT=5556
      - EXT_TO_URL=https://ext.to
      - INCLUDE_ADULT=true
    depends_on:
      - flaresolverr
```

### Manual setup

```bash
# Install dependencies
pip install requests beautifulsoup4

# Run (ensure FlareSolverr is running on port 8191)
python3 ext_to_torznab.py
```

### Configuration (ext.to)

| Env var | Default | Description |
|---------|---------|-------------|
| `EXT_TO_HOST` | `0.0.0.0` | Bind address |
| `EXT_TO_PORT` | `5556` | Listen port |
| `EXT_TO_URL` | `https://ext.to` | ext.to base URL |
| `FLARESOLVERR_URL` | `http://localhost:8191` | FlareSolverr endpoint |
| `FLARESOLVERR_TIMEOUT` | `60000` | FlareSolverr timeout (ms) |
| `INCLUDE_ADULT` | `true` | Include adult (XXX) content |
| `LOG_LEVEL` | `INFO` | Log level (DEBUG, INFO, WARNING) |
| `EXT_TO_CACHE_TTL` | `300` | Cache TTL in seconds |

### Torznab API (ext.to)

The same Torznab protocol as btdig, plus:

| Feature | Details |
|---------|---------|
| `t=download` | On-demand magnet resolution for a GUID |
| `t=search` | Free-text search |
| `t=tvsearch` | TV search with `season`, `ep` params |
| `t=movie` | Movie search |
| `t=music`, `t=book` | Music and book search |
| Categories | Full Torznab category tree (42 subcategories) |
| Seeders/Peers | Live counts from ext.to (unlike btdig) |

### Magnet resolution

ext.to requires a two-step process for magnet links:

1. **Search** returns metadata (title, size, seeders, category) with empty magnet
2. **HMAC-signed API call** to `/ajax/getSearchMagnet.php` extracts the infohash
3. **On-demand (`t=download`)** — when the initial search produces no magnet,
   the *arr app calls back with the GUID and the bridge resolves the magnet via
   the detail-page API

The HMAC is computed as `SHA256(torrent_id|timestamp|searchPageToken)` —
tokens are extracted from the page HTML and auto-refreshed when they expire.

### Adding to Sonarr as a second indexer

Add both bridges as separate Torznab indexers:

| Indexer | URL |
|---------|-----|
| BTDig | `http://host:5555/torznab/api` |
| ext.to | `http://host:5556/torznab/api` |



## License

MIT
