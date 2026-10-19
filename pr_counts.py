#!/usr/bin/env python3
"""Snapshot open PR counts across the Home Assistant GitHub org into SQLite."""

import argparse
import os
import sqlite3
import sys
import time
from datetime import date, timedelta

import requests

# -- Repo config ---------------------------------------------------------------

REPOS = [
    ("addons", "Addons"),
    ("android", "Android"),
    ("brands", "Brands"),
    ("core", "Core"),
    ("contributors.home-assistant.io", "Contributor docs"),
    ("home-assistant.io", "End-user docs"),
    ("frontend", "Frontend"),
    ("iOS", "iOS"),
    ("operating-system", "Operating system"),
    ("supervisor", "Supervisor"),
]

ORG = "home-assistant"
SECURITY_REPO = "core"
SECURITY_ROW = "Security triage"
MERGED_WINDOW_DAYS = 7

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
    """Sleep if the rate limit is getting low."""
    remaining = response.headers.get("X-RateLimit-Remaining")
    if remaining is not None and int(remaining) < 3:
        reset_at = int(response.headers.get("X-RateLimit-Reset", 0))
        wait = max(reset_at - int(time.time()), 1)
        print(f"  Rate limit low ({remaining} remaining), sleeping {wait}s...")
        time.sleep(wait)


def search_count(session: requests.Session, query: str) -> int:
    """Return the total_count for a GitHub issue/PR search query.

    Uses the same query style as the meeting notes links, so counts match
    what you see when you click through on GitHub.
    """
    resp = session.get(
        f"{API_BASE}/search/issues", params={"q": query, "per_page": 1}
    )
    resp.raise_for_status()
    respect_rate_limit(resp)
    # Search API allows 30 requests a minute, so pace the calls
    time.sleep(1)
    return resp.json()["total_count"]


def count_open_prs(session: requests.Session, slug: str) -> int:
    return search_count(session, f"is:open is:pr archived:false repo:{ORG}/{slug}")


def count_merged_prs(session: requests.Session, slug: str, since: str) -> int:
    return search_count(
        session, f"is:pr is:merged archived:false repo:{ORG}/{slug} merged:>{since}"
    )


def count_org_open_prs(session: requests.Session) -> int:
    return search_count(session, f"org:{ORG} is:pr is:open archived:false")


def count_org_merged_prs(session: requests.Session, since: str) -> int:
    return search_count(
        session, f"org:{ORG} is:pr is:merged archived:false merged:>{since}"
    )


def count_security_triage(session: requests.Session) -> int:
    """Count repository security advisories in the Triage state on core.

    Needs the security_events scope (included in the classic repo scope)
    and admin or security manager access on the repo.
    """
    url = f"{API_BASE}/repos/{ORG}/{SECURITY_REPO}/security-advisories"
    params = {"state": "triage", "per_page": 100}
    total = 0

    while url:
        resp = session.get(url, params=params)
        resp.raise_for_status()
        respect_rate_limit(resp)
        total += len(resp.json())
        # Follow the Link header; the next URL already carries the params
        url = resp.links.get("next", {}).get("url")
        params = None

    return total


# -- SQLite helpers -------------------------------------------------------------


def init_db(db_path: str) -> sqlite3.Connection:
    """Create tables if needed, add newer columns, seed default thresholds."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pr_snapshots (
            snapshot_date TEXT NOT NULL,
            repo          TEXT NOT NULL,
            open_count    INTEGER NOT NULL,
            merged_count  INTEGER,
            PRIMARY KEY (snapshot_date, repo)
        )
        """
    )

    # Existing databases were created before merged_count existed
    columns = [row[1] for row in conn.execute("PRAGMA table_info(pr_snapshots)")]
    if "merged_count" not in columns:
        conn.execute("ALTER TABLE pr_snapshots ADD COLUMN merged_count INTEGER")

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


def write_snapshot(
    conn: sqlite3.Connection,
    snapshot_date: str,
    repo: str,
    open_count: int,
    merged_count: int | None,
) -> None:
    """Upsert a single snapshot row."""
    conn.execute(
        """
        INSERT OR REPLACE INTO pr_snapshots
            (snapshot_date, repo, open_count, merged_count)
        VALUES (?, ?, ?, ?)
        """,
        (snapshot_date, repo, open_count, merged_count),
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
    results: list[tuple[str, int, int | None]],
    previous: dict[str, int],
    thresholds: dict[str, int],
) -> None:
    """Print a readable plain-text summary table."""
    name_w = max(len("Repository"), *(len(r[0]) for r in results))

    header = (
        f"{'Repository':<{name_w}}  {'Previous':>8}  {'Current':>8}  "
        f"{'Diff':>6}  {'Merged':>6}  {'Threshold':>9}"
    )
    separator = "-" * len(header)

    print()
    print(header)
    print(separator)

    for repo_name, count, merged in results:
        prev = previous.get(repo_name)
        thresh = thresholds.get(repo_name)

        prev_str = str(prev) if prev is not None else ""
        if prev is not None:
            diff = count - prev
            diff_str = f"+{diff}" if diff >= 0 else str(diff)
        else:
            diff_str = ""
        merged_str = str(merged) if merged is not None else ""
        thresh_str = str(thresh) if thresh is not None else ""

        print(
            f"{repo_name:<{name_w}}  {prev_str:>8}  {count:>8}  "
            f"{diff_str:>6}  {merged_str:>6}  {thresh_str:>9}"
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

    token = load_token()
    if not token:
        print(
            "Error: No GitHub token found.\n"
            "Either create github_token.txt in the same folder as this script,\n"
            "or set the GITHUB_TOKEN environment variable.\n\n"
            "The token needs the repo scope (classic), which includes security_events.",
            file=sys.stderr,
        )
        sys.exit(1)

    session = make_session(token)
    snapshot_date = args.date
    # Merged count covers the 7 days up to and including the snapshot date
    since = (
        date.fromisoformat(snapshot_date) - timedelta(days=MERGED_WINDOW_DAYS)
    ).isoformat()

    print(f"PR count snapshot for {snapshot_date} (merged after {since})")
    print()

    # Each result is (display name, open count, merged count or None)
    results: list[tuple[str, int, int | None]] = []

    def collect(name: str, open_fn, merged_fn) -> None:
        try:
            open_count = open_fn()
        except Exception as e:
            print(f"  {name}: ERROR - {e}", file=sys.stderr)
            return

        merged_count = None
        if merged_fn is not None:
            try:
                merged_count = merged_fn()
            except Exception as e:
                print(f"  {name}: merged count ERROR - {e}", file=sys.stderr)

        merged_note = f", {merged_count} merged" if merged_count is not None else ""
        print(f"  {name}: {open_count} open{merged_note}")
        results.append((name, open_count, merged_count))

    for slug, display_name in REPOS:
        collect(
            display_name,
            lambda s=slug: count_open_prs(session, s),
            lambda s=slug: count_merged_prs(session, s, since),
        )

    collect(
        "Organization",
        lambda: count_org_open_prs(session),
        lambda: count_org_merged_prs(session, since),
    )

    collect(SECURITY_ROW, lambda: count_security_triage(session), None)

    if not results:
        print("No data collected. Exiting.", file=sys.stderr)
        sys.exit(1)

    conn = init_db(args.db)
    try:
        for repo_name, open_count, merged_count in results:
            write_snapshot(conn, snapshot_date, repo_name, open_count, merged_count)
        conn.commit()
        print(f"\nWrote {len(results)} rows to {args.db}")
    except Exception as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(1)

    previous = get_previous_counts(conn, snapshot_date)
    thresholds = get_thresholds(conn)
    print_summary(results, previous, thresholds)

    conn.close()


if __name__ == "__main__":
    main()