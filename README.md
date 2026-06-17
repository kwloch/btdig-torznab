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

## License

MIT
