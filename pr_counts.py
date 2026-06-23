#!/usr/bin/env python3
"""Snapshot open PR counts across the Home Assistant GitHub org into SQLite."""

import argparse
import os
import sqlite3
import sys
import time
from datetime import date

import requests

# -- Repo config ---------------------------------------------------------------

REPOS = [
    ("addons", "Addons"),
    ("android", "Android"),
    ("brands", "Brands"),
    ("core", "Core"),
    ("developers.home-assistant", "Developer docs"),
    ("home-assistant.io", "End-user docs"),
    ("frontend", "Frontend"),
    ("iOS", "iOS"),
    ("operating-system", "Operating system"),
    ("supervisor", "Supervisor"),
]

DEFAULT_THRESHOLDS = {
    "Android": 20,
    "Core": 500,
    "Frontend": 50,
}

DEFAULT_DB_PATH = "/home/scripts/pr_counts.db"

# -- Token loading (matches board_analytics.py pattern) -------------------------


def load_token() -> str | None:
    """Load GitHub token from file or environment.

    Checks in order:
      1. github_token.txt in the same directory as this script
      2. GITHUB_TOKEN environment variable
    """
    token_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "github_token.txt"
    )
    if os.path.isfile(token_file):
        with open(token_file, "r") as f:
            token = f.read().strip()
            if token:
                return token

    return os.environ.get("GITHUB_TOKEN")


# -- GitHub API helpers ---------------------------------------------------------

API_BASE = "https://api.github.com"


def make_session(token: str) -> requests.Session:
    """Create a requests session with auth and accept headers."""
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }
    )
    return session


def respect_rate_limit(response: requests.Response) -> None:
    """Sleep briefly if rate limit is getting low."""
    remaining = response.headers.get("X-RateLimit-Remaining")
    if remaining is not None and int(remaining) < 10:
        reset_at = int(response.headers.get("X-RateLimit-Reset", 0))
        wait = max(reset_at - int(time.time()), 1)
        print(f"  Rate limit low ({remaining} remaining), sleeping {wait}s...")
        time.sleep(wait)


def count_open_prs(session: requests.Session, slug: str) -> int:
    """Count open PRs for a single repo using pagination."""
    url = f"{API_BASE}/repos/home-assistant/{slug}/pulls"
    params = {"state": "open", "per_page": 100, "page": 1}
    total = 0

    while True:
        resp = session.get(url, params=params)
        resp.raise_for_status()
        items = resp.json()
        total += len(items)
        respect_rate_limit(resp)

        if len(items) < 100:
            break
        params["page"] += 1
        time.sleep(0.5)

    return total


def count_org_open_prs(session: requests.Session) -> int:
    """Count all open PRs across the home-assistant org via search API."""
    url = f"{API_BASE}/search/issues"
    params = {"q": "org:home-assistant is:pr is:open", "per_page": 1}

    resp = session.get(url, params=params)
    resp.raise_for_status()
    respect_rate_limit(resp)

    return resp.json()["total_count"]


# -- SQLite helpers -------------------------------------------------------------


def init_db(db_path: str) -> sqlite3.Connection:
    """Create tables if they don't exist and seed default thresholds."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pr_snapshots (
            snapshot_date TEXT NOT NULL,
            repo          TEXT NOT NULL,
            open_count    INTEGER NOT NULL,
            PRIMARY KEY (snapshot_date, repo)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS repo_thresholds (
            repo      TEXT PRIMARY KEY,
            threshold INTEGER NOT NULL
        )
        """
    )

    for repo_name, threshold in DEFAULT_THRESHOLDS.items():
        conn.execute(
            "INSERT OR IGNORE INTO repo_thresholds (repo, threshold) VALUES (?, ?)",
            (repo_name, threshold),
        )

    conn.commit()
    return conn


def write_snapshot(conn: sqlite3.Connection, snapshot_date: str, repo: str, count: int) -> None:
    """Upsert a single snapshot row."""
    conn.execute(
        "INSERT OR REPLACE INTO pr_snapshots (snapshot_date, repo, open_count) VALUES (?, ?, ?)",
        (snapshot_date, repo, count),
    )


def get_previous_counts(conn: sqlite3.Connection, snapshot_date: str) -> dict[str, int]:
    """Get the most recent snapshot before the given date, keyed by repo name."""
    rows = conn.execute(
        """
        SELECT repo, open_count
        FROM pr_snapshots
        WHERE snapshot_date = (
            SELECT MAX(snapshot_date)
            FROM pr_snapshots
            WHERE snapshot_date < ?
        )
        """,
        (snapshot_date,),
    ).fetchall()

    return {row[0]: row[1] for row in rows}


def get_thresholds(conn: sqlite3.Connection) -> dict[str, int]:
    """Load all thresholds, keyed by repo name."""
    rows = conn.execute("SELECT repo, threshold FROM repo_thresholds").fetchall()
    return {row[0]: row[1] for row in rows}


# -- Output --------------------------------------------------------------------


def print_summary(
    results: list[tuple[str, int]],
    previous: dict[str, int],
    thresholds: dict[str, int],
) -> None:
    """Print a readable plain-text summary table."""
    # Column widths
    name_w = max(len(r[0]) for r in results)
    name_w = max(name_w, len("Repository"))

    header = (
        f"{'Repository':<{name_w}}  {'Current':>8}  {'Previous':>8}  {'Diff':>8}  {'Threshold':>9}"
    )
    separator = "-" * len(header)

    print()
    print(header)
    print(separator)

    for repo_name, count in results:
        prev = previous.get(repo_name)
        thresh = thresholds.get(repo_name)

        prev_str = str(prev) if prev is not None else ""
        if prev is not None:
            diff = count - prev
            diff_str = f"+{diff}" if diff >= 0 else str(diff)
        else:
            diff_str = ""

        thresh_str = str(thresh) if thresh is not None else ""

        print(
            f"{repo_name:<{name_w}}  {count:>8}  {prev_str:>8}  {diff_str:>8}  {thresh_str:>9}"
        )

    print(separator)
    print()


# -- Main -----------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Snapshot open PR counts across the Home Assistant GitHub org"
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("DB_PATH", DEFAULT_DB_PATH),
        help=f"Path to SQLite database (default: DB_PATH env or {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Snapshot date in YYYY-MM-DD format (default: today)",
    )
    args = parser.parse_args()

    # Load token
    token = load_token()
    if not token:
        print(
            "Error: No GitHub token found.\n"
            "Either create github_token.txt in the same folder as this script,\n"
            "or set the GITHUB_TOKEN environment variable.\n\n"
            "The token needs the repo scope (classic) or pull-request read access (fine-grained).",
            file=sys.stderr,
        )
        sys.exit(1)

    session = make_session(token)
    snapshot_date = args.date

    print(f"PR count snapshot for {snapshot_date}")
    print()

    # Collect counts
    results: list[tuple[str, int]] = []

    for slug, display_name in REPOS:
        try:
            count = count_open_prs(session, slug)
            print(f"  {display_name}: {count} open PRs")
            results.append((display_name, count))
        except Exception as e:
            print(f"  {display_name}: ERROR - {e}", file=sys.stderr)

        time.sleep(0.5)

    # Org-wide count
    try:
        org_count = count_org_open_prs(session)
        print(f"  Organization: {org_count} open PRs")
        results.append(("Organization", org_count))
    except Exception as e:
        print(f"  Organization: ERROR - {e}", file=sys.stderr)

    if not results:
        print("No data collected. Exiting.", file=sys.stderr)
        sys.exit(1)

    # Write to SQLite
    conn = init_db(args.db)
    try:
        for repo_name, count in results:
            write_snapshot(conn, snapshot_date, repo_name, count)
        conn.commit()
        print(f"\nWrote {len(results)} rows to {args.db}")
    except Exception as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(1)

    # Summary
    previous = get_previous_counts(conn, snapshot_date)
    thresholds = get_thresholds(conn)
    print_summary(results, previous, thresholds)

    conn.close()


if __name__ == "__main__":
    main()
