# Open discussion items — Creators Deal Finder

Parked during the Jul 20 dashboard review so we don't lose them. Neither has a decision yet.

## 1. Add more categories + API budget/token strategy

Right now only 2 of the 26 top-level categories (Appliances, Electronics) have
browse-node data and are enabled in the Method 1/2 round robin. Adding more
isn't just a toggle:

- Method 1 needs real Amazon subcategory `browseNodeId`s researched into `data/categories.json`.
- Method 2 needs each category's `parentBrowseNodeId` filled into `data/topcategories.json` (24 of 26 are currently `null`).
- More enabled nodes dilutes the round robin — `tick_seconds` (110s/node) stays fixed, so the same daily tick budget gets spread across more targets, meaning each node is revisited less often.
- It also uses more of the shared daily budget: Amazon Creators API TPD (currently 8640/day, DE-only), Keepa API tokens, and OpenAI calls.

Options on the table (none chosen yet):

1. **Spread across marketplaces first** — `config.yml` already holds Creators API affiliate tags for DE/FR/IT/ES/GB/UK/NL/BE, each of which likely has its own separate daily TPD budget. `method_test.marketplace` is currently hardcoded to `DE` only; running the same categories across more of these marketplaces would multiply throughput without a new account.
2. **Add more DE categories, same budget** — expand coverage but stay inside the current 8640/day cap. Slower rollout per category, no new cost.
3. **New/upgraded account** — worth knowing going in: Amazon's Creators/PA-API daily quota is normally tied to the Associates account's trailing sales performance, not something a brand-new signup gets more of automatically. Keepa and OpenAI scale by subscription tier instead, which is a more straightforward lever if either of those (not Amazon) turns out to be the real bottleneck.

**Needs:** decide which lever(s) to pull, then (if adding real category coverage) source the missing browse-node IDs for the categories we want to light up.

## 2. Run it 24/7

The dashboard currently shows "worker not running" — the worker process isn't
staying up continuously today. `Procfile` (`web: gunicorn ...`, `worker: python
worker.py`) points at a Heroku-style deploy.

Options on the table (none chosen yet):

1. **Keep `worker.py`, just make it always-on** — smallest change: whatever host this deploys to, make sure the worker process is provisioned to run continuously (e.g. a Heroku worker dyno scaled to 1 and left on) instead of being started/stopped by hand.
2. **Rewrite as a standalone Discord bot** — bigger lift: restructure the engine to run inside a `discord.py` bot process. Ties naturally into the existing Discord alerting, but is a real rewrite, not a config change.

**Needs:** confirm where this actually runs today, then weigh cost/effort/reliability between the two directions (or a third option) before committing.
