# Release Download Analytics

Release Download Analytics is a Docker-first tool for tracking GitHub release asset downloads over time and viewing the results in a Streamlit dashboard.

```text
Docker container
├── scheduled Python collector
├── SQLite database persisted in /data
└── Streamlit dashboard on port 8501
```

GitHub exposes only the **current cumulative** download count for each release asset. This project polls GitHub on a schedule, stores each run as a snapshot, and uses those snapshots to show historical trends, release adoption, daily deltas, and annotated events.

## Docker-first project structure

- `Dockerfile` builds the application image from `python:3.12-slim`.
- `docker-compose.yml` runs the web dashboard, scheduled collector, environment configuration, and persistent SQLite volume.
- `docker-entrypoint.sh` starts the collector loop and Streamlit dashboard, or dispatches one-shot commands.
- `collector.py` fetches GitHub release data and stores download snapshots.
- `dashboard.py` serves the Streamlit analytics UI.
- `db.py` creates the SQLite schema and provides metric helpers.
- `config.py` reads environment-driven runtime configuration.
- `import_history.py` imports manually pasted historical snapshots.
- `tests/` covers historical parsing and download delta calculations.
- `.env.example` shows the runtime configuration expected by Docker and local commands.

## Quick start with Docker Compose

1. Create your environment file:

   ```bash
   cp .env.example .env
   ```

2. Edit `.env` for the repositories you want to track:

   ```env
   REPOS=home-assistant/frontend,owner/another-repo
   GITHUB_TOKEN=
   LOG_LEVEL=INFO
   ```

   Public repositories work without authentication. Set `GITHUB_TOKEN` for higher GitHub REST API rate limits. Compose sets `DB_PATH=/data/release_analytics.sqlite3` so the SQLite database is stored in the Docker volume.

3. Build and start the stack:

   ```bash
   docker compose up --build
   ```

4. Open the dashboard:

   <http://localhost:8501>

The default container mode starts Streamlit and runs the collector in the background. A collection happens immediately on startup, then repeats every `RUN_INTERVAL_SECONDS` seconds. The Compose default is `21600` seconds, or every 6 hours.

## Runtime configuration

Configure the container with `.env` and Compose environment values:

| Variable | Default | Description |
| --- | --- | --- |
| `REPOS` | none | Comma-separated list of `owner/repo` repositories to poll. |
| `GITHUB_TOKEN` | empty | Optional GitHub token for higher API rate limits. |
| `DB_PATH` | `/data/release_analytics.sqlite3` in Docker | SQLite database path. Keep this under `/data` in Docker so it is persisted. |
| `LOG_LEVEL` | `INFO` | Python logging level. |
| `RUN_INTERVAL_SECONDS` | `21600` | Delay between collector runs in web mode. |
| `COLLECT_ON_START` | `true` | Run the collector once before waiting for the first interval. |
| `STREAMLIT_SERVER_ADDRESS` | `0.0.0.0` | Streamlit bind address inside the container. |
| `STREAMLIT_SERVER_PORT` | `8501` | Streamlit port inside the container. |

## Docker commands

### Start or update the application

```bash
docker compose up --build -d
```

### View logs

```bash
docker compose logs -f release-analytics
```

### Stop the application

```bash
docker compose down
```

This stops the container but keeps the named SQLite volume. To remove collected data as well, remove the volume intentionally:

```bash
docker compose down -v
```

### Run a one-shot collection

Use the same image and environment, but run the collector directly:

```bash
docker compose run --rm release-analytics collect
```

You can also pass repositories explicitly:

```bash
docker compose run --rm release-analytics collect --repo owner/repo --repo another-owner/another-repo
```

### Import historical snapshots

Paste or redirect history into the one-shot importer. The importer uses the `Current UTC time` line as `collected_at` for all following asset counts:

```bash
docker compose run --rm -T release-analytics import-history --repo owner/repo <<'EOF'
Current UTC time: 2026-07-02T05:11:27Z
v4.7.0  2026-06-27T17:58:49Z    skylight-calendar-card.js=188
EOF
```

The importer creates release and asset metadata if it is missing, then inserts a snapshot without overwriting existing snapshots at other collection times.

### Build and run without Compose

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

## Dashboard

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

## Local development

Docker is the recommended runtime, but you can still run commands locally for development and tests.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Run the collector locally:

```bash
python collector.py
```

Launch Streamlit locally:

```bash
streamlit run dashboard.py
```

Run tests:

```bash
pytest
```

## SQLite data model

The database contains `repos`, `releases`, `assets`, `download_snapshots`, and `events`. Metadata rows are upserted to avoid duplicates, while each collector run inserts a new snapshot keyed by asset and UTC collection timestamp.

Indexes cover repository/tag lookup, GitHub asset IDs, snapshot collection time, and release publication time.

## Known limitation

GitHub's releases API exposes cumulative **current** asset download counts only. Historical trend accuracy depends on how often you poll and store snapshots.
