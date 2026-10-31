"""The functions app.py calls (see the contract at the top of app.py).
A CLI can call exactly the same functions."""
from __future__ import annotations

from . import bump, history, ingest
from .db import get_db, init_db
from .errors import BlastError  # noqa: F401  (re-exported for callers)


def init() -> None:
    init_db(get_db())


def ingest_repo(repo_path: str, repo_name: str) -> dict:
    return ingest.ingest_repo(get_db(), repo_path, repo_name)


def list_packages(repo_name: str) -> list[dict]:
    db = get_db()
    snap = db.snapshots.find_one({"repo": repo_name}, sort=[("createdAt", -1)])
    if not snap:
        return []
    return list(db.packages.find({"snapshotId": snap["_id"]},
                                 {"_id": 0, "name": 1, "pinnedVersion": 1, "rule": 1, "parseError": 1})
                .sort("name", 1))


def simulate_bump(repo_name: str, library: str, target_version: str) -> dict:
    return bump.simulate_bump(get_db(), repo_name, library, target_version)


def record_outcome(run_id: str, outcome: str, files: list[str], note: str) -> None:
    history.record_outcome(get_db(), run_id, outcome, files, note)


def get_history(repo_name: str, library: str) -> list[dict]:
    return history.get_history(get_db(), repo_name, library)


def get_events(repo_name: str) -> list[dict]:
    rows = get_db().ingest_events.find({"repo": repo_name}).sort("receivedAt", -1).limit(100)
    return [{"receivedAt": r["receivedAt"].strftime("%Y-%m-%d %H:%M:%S UTC"),
             "commitSHA": r.get("commitSHA", ""), "status": r["status"],
             "reason": r.get("reason") or ""} for r in rows]
