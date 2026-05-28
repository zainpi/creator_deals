# Amazon Creators Deal Finder

**Status**: Standalone, ready for integration

A private web-based deal discovery platform using Amazon Creators API, Keepa validation, AI scoring, and Discord alerts.

## Quick Start

```bash
cd creators_deal_finder
pip install -r requirements.txt
python app.py
```

Visit `http://localhost:5000`

## Architecture

- **Creators API**: SearchItems for deal discovery
- **Keepa API**: Historical validation & drop calculation
- **OpenAI**: Deal scoring (1-10)
- **Discord Webhook**: Alert posting
- **SQLite**: Product persistence
- **Flask**: Web dashboard

## Integration Path

### Step 1: Replace Creators API Calls
Replace placeholder in `creators_search.py` with your existing `creators_fetch` from `dealsbrowser.py`

```python
from creators_fetch import fetch_items
```

### Step 2: Reuse Keepa Logic
Move Keepa parsers from:
- `dealsBrowser.py` → `keepa_service.py`
- `productTrackerV2.py` → `keepa_service.py`

### Step 3: Share Configuration
Use your existing `config.yaml` structure:
- Keepa API key
- OpenAI API key
- Discord webhook
- Marketplace settings

### Step 4: Merge Statistics
Link to your existing `testing_stats.py` or metrics system

## File Structure

```
creators_deal_finder/
├── app.py                 # Flask server
├── scheduler.py           # Main discovery loop
├── creators_search.py     # Creators API wrapper
├── keepa_service.py       # Keepa validation
├── ai_scoring.py          # OpenAI scoring
├── discord_alerts.py      # Discord posting
├── filters.py             # Filtering logic
├── database.py            # SQLite persistence
├── testing_stats.py       # Statistics tracking
├── config.yml             # Configuration
├── templates/index.html   # Dashboard
├── static/app.js          # Frontend logic
├── static/style.css       # Styling
└── data/                  # Database storage
```

## Configuration (config.yml)

```yaml
amazon:
  marketplace: DE           # or GB, IT, FR, ES
  min_saving_percent: 50   # Minimum discount
  max_price: 450           # Maximum price €
  pages_to_scan: 5         # Pages per scan

scanner:
  scan_interval_seconds: 15

filters:
  blocked_categories:
    - Books
    - Gift Cards

ai:
  enabled: true
  minimum_score: 7
  model: gpt-4-mini

keepa:
  api_key: YOUR_KEY

discord:
  webhook_url: YOUR_WEBHOOK
```

## API Endpoints

- `GET /api/stats` — Discovery statistics
- `GET /api/products` — Recent discoveries
- `POST /api/start` — Start scanner
- `POST /api/stop` — Stop scanner
- `POST /api/reset` — Reset stats
- `GET|POST /api/config` — Get/update config

## Key Features

✅ **Real-time Discovery** — Scans every 15 seconds  
✅ **Smart Filtering** — Category, seller, price validation  
✅ **Keepa Validation** — 90-day drop calculation  
✅ **AI Scoring** — Gpt-4-mini powered 1-10 rating  
✅ **Discord Alerts** — Automatic embed posting  
✅ **Web Dashboard** — Live stats & product feed  
✅ **Persistence** — SQLite database  

## Next Steps for Production

1. **ASIN Velocity Tracking** — Track first seen, disappear patterns
2. **Keepa Caching** — Cache stats for 6-24 hours
3. **Duplicate Suppression** — Same ASIN/parent ASIN handling
4. **Fast Lane** — Page 1 every 10s, pages 2-5 every 2min
5. **False Positive Reduction** — Add seller reputation, review count
6. **Real-time Dashboard** — Live feed with pass/fail reasons

## Integration with Existing System

This is designed to work alongside your:
- `dealsBrowser.py` — Complement, not replace
- `productTrackerV2.py` — Independent discovery source
- `deal_platforms.py` — Separate pipeline
- `main.py` — Add as optional command `/discover_deals_creators`

## Notes

- All timestamps in ISO format (UTC)
- Keepa cached locally to avoid spam
- AI scoring optional, degraded gracefully
- SQLite easily migrated to PostgreSQL
- Flask easily wrapped in async for integration
