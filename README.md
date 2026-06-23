# OHF PR Counts

Snapshots open pull request counts across the Home Assistant GitHub org and stores them in SQLite for Grafana time-series dashboards.

Tracks ten repositories individually plus an org-wide total:

| Repository | Display name |
|---|---|
| addons | Addons |
| android | Android |
| brands | Brands |
| core | Core |
| developers.home-assistant | Developer docs |
| home-assistant.io | End-user docs |
| frontend | Frontend |
| iOS | iOS |
| operating-system | Operating system |
| supervisor | Supervisor |

The org-wide count comes from the GitHub search API and covers all repos in the `home-assistant` org, not just the ten above.

## Requirements

- Python 3.10+
- `requests` (`pip install requests`)
- A GitHub personal access token with `repo` scope (classic) or pull request read access (fine-grained)

## Setup

1. Clone the repo
2. Create a `github_token.txt` file in the repo root containing your PAT (this file is gitignored). Alternatively, set the `GITHUB_TOKEN` environment variable.
3. Run: `python3 pr_counts.py`

## Usage

```
python3 pr_counts.py [--db PATH] [--date YYYY-MM-DD]
```

**--db** sets the SQLite database path. Defaults to the `DB_PATH` environment variable, or `/home/scripts/pr_counts.db` if neither is set.

**--date** overrides the snapshot date. Defaults to today. Useful for backfilling a missed run.

Re-running on the same date performs an upsert, so it won't create duplicate rows.

## Output

Each run prints a summary table to stdout:

```
Repository        Current  Previous      Diff  Threshold
----------------------------------------------------------
Addons                 12        10        +2
Android                18        15        +3         20
Brands                  4         4        +0
Core                  831       829        +2        500
...
Organization         1571      1560       +11
----------------------------------------------------------
```

Previous counts and diffs appear once there are at least two snapshots in the database.

## Database

Two tables:

**pr_snapshots** stores one row per repo per snapshot date. Primary key on `(snapshot_date, repo)`.

**repo_thresholds** stores optional alert thresholds per repo. Seeded on first run with defaults for Android (20), Core (500), and Frontend (50). Manual edits to this table are preserved across runs.

## Cron

Designed to run weekly alongside `board_analytics.py`. Example crontab entry:

```
15 7 * * 1 python3 /opt/OHF-PR-Counts/pr_counts.py --db /var/lib/grafana/pr_counts.db >> /var/log/pr_counts.log 2>&1
```
