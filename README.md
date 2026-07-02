# Release Download Analytics

A small personal analytics tool for tracking GitHub release asset downloads over time:

`scheduled Python collector → SQLite database → Streamlit dashboard`

GitHub exposes only the **current cumulative** download count for each release asset. This project stores each polling run as a snapshot so you can build historical trends, compare release adoption, and annotate external events such as release posts, docs launches, or Reddit posts.

## Project structure

- `collector.py` fetches GitHub release data and stores snapshots.
- `db.py` creates the SQLite schema and provides metric helpers.
- `dashboard.py` launches the Streamlit dashboard.
- `import_history.py` imports manually pasted historical snapshots.
- `tests/` covers historical parsing and download delta calculations.
- `.env.example` shows configuration.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Configure

Edit `.env`:

```env
REPOS=home-assistant/frontend,owner/another-repo
GITHUB_TOKEN=
DB_PATH=release_analytics.sqlite3
LOG_LEVEL=INFO
```

Public repositories work without authentication. Set `GITHUB_TOKEN` if you want higher GitHub REST API rate limits.

## Collect data

Run for repositories from `.env`:

```bash
python collector.py
```

Or pass repositories explicitly:

```bash
python collector.py --repo owner/repo --repo another-owner/another-repo
```

By default draft releases are skipped. Prereleases are stored and can be filtered in the dashboard.

## Import historical snapshots

Paste text into stdin. The importer uses the `Current UTC time` line as `collected_at` for all following asset counts:

```bash
python import_history.py --repo owner/repo <<'EOF'
Current UTC time: 2026-07-02T05:11:27Z
v4.7.0  2026-06-27T17:58:49Z    skylight-calendar-card.js=188
EOF
```

The importer creates release and asset metadata if it is missing, then inserts a snapshot without overwriting existing snapshots at other collection times.

## Launch the dashboard

```bash
streamlit run dashboard.py
```

Dashboard controls:

- **Repo selector** filters to one repository.
- **Asset selector** defaults to `skylight-calendar-card.js` when present.
- **Prerelease toggle** includes or excludes prereleases.
- **Date range filter** limits snapshots used for charts.

Dashboard sections:

- **KPI cards** show latest stable release downloads, previous stable release downloads, current-vs-previous adoption percentage, and downloads added over the last 24 hours / 7 days.
- **Total downloads by release** compares latest cumulative asset downloads per release.
- **Cumulative downloads by release age** overlays versions by days since publication, useful for adoption curves.
- **Daily downloads by version** shows downloads added per day from snapshot deltas.
- **Release comparison milestones** reports 24h, 72h, 7d, and 14d totals when snapshots exist before those cutoffs.
- **Events / annotations** displays rows from the `events` table for notes such as blog posts, docs launches, Reddit posts, or social announcements.

## Scheduling

### Cron

Run every 6 hours:

```cron
0 */6 * * * cd /path/to/release-analytics && /path/to/release-analytics/.venv/bin/python collector.py >> collector.log 2>&1
```

### GitHub Actions

Create `.github/workflows/collect.yml` in your own deployment repo and persist the SQLite file as appropriate for your setup:

```yaml
name: collect-release-downloads
on:
  schedule:
    - cron: '0 */6 * * *'
  workflow_dispatch:
jobs:
  collect:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install -r requirements.txt
      - run: python collector.py
        env:
          REPOS: owner/repo
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### Docker or manual loop

A container can run cron, or a simple repeated command:

```bash
while true; do python collector.py; sleep 21600; done
```

### Local manual run

```bash
python collector.py
```

## SQLite schema

The database contains `repos`, `releases`, `assets`, `download_snapshots`, and `events`. Metadata rows are upserted to avoid duplicates, while each collector run inserts a new snapshot keyed by asset and UTC collection timestamp.

Indexes cover repository/tag lookup, GitHub asset IDs, snapshot collection time, and release publication time.

## Known limitation

GitHub's releases API exposes cumulative **current** asset download counts only. Historical trend accuracy depends on how often you poll and store snapshots.

## Run as a Docker web asset

The project can run as a single container that continuously polls GitHub on a schedule, stores SQLite data in `/data`, and exposes the Streamlit dashboard as a web-viewable asset on port `8501`.

Build and run with Docker:

```bash
docker build -t release-analytics .
docker run --rm \
  --env-file .env \
  -e DB_PATH=/data/release_analytics.sqlite3 \
  -e RUN_INTERVAL_SECONDS=21600 \
  -p 8501:8501 \
  -v release_analytics_data:/data \
  release-analytics
```

Or use Compose:

```bash
docker compose up --build
```

Then open <http://localhost:8501>. Set `RUN_INTERVAL_SECONDS` to change the polling cadence. The default is `21600` seconds, or every 6 hours. Set `COLLECT_ON_START=false` if you want the container to wait one full interval before the first collection.

One-shot container commands are also available:

```bash
docker compose run --rm release-analytics collect --repo owner/repo
docker compose run --rm release-analytics import-history --repo owner/repo < history.txt
```
