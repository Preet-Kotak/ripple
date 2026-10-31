"""Memory of past bumps: every simulation is saved, and its real outcome can be recorded.
The next bump of the same library reads this and changes its answer."""
from __future__ import annotations

from datetime import datetime, timezone

from .errors import BlastError


def save_run(db, repo: str, snapshot_id: str, report: dict):
    db.bump_runs.insert_one({
        "_id": report["runId"], "runId": report["runId"], "repo": repo, "snapshotId": snapshot_id,
        "library": report["library"], "fromVersion": report["fromVersion"],
        "toVersion": report["toVersion"], "jump": report["jump"], "verdict": report["verdict"],
        "affected": [{"path": a["path"], "depth": a["depth"], "label": a["label"]}
                     for a in report["affected"]],
        "createdAt": datetime.now(timezone.utc), "outcome": "unknown", "brokeFiles": [], "note": "",
    })


def last_broke(db, repo: str, library: str):
    return db.bump_runs.find_one({"repo": repo, "library": library, "outcome": "broke"},
                                 sort=[("createdAt", -1)])


def record_outcome(db, run_id: str, outcome: str, files, note: str = ""):
    if outcome not in ("ok", "broke"):
        raise BlastError("INVALID_INPUT", "Outcome must be ok or broke.")
    files = list(files or []) if outcome == "broke" else []
    res = db.bump_runs.update_one(
        {"_id": run_id},
        {"$set": {"outcome": outcome, "brokeFiles": files, "note": note or "",
                  "outcomeAt": datetime.now(timezone.utc)}})
    if res.matched_count == 0:
        raise BlastError("RUN_NOT_FOUND", f"No bump run with id {run_id}.")


def get_history(db, repo: str, library: str, limit: int = 50) -> list[dict]:
    rows = db.bump_runs.find({"repo": repo, "library": library}).sort("createdAt", -1).limit(limit)
    return [{"runId": r["runId"], "createdAt": r["createdAt"].strftime("%Y-%m-%d %H:%M UTC"),
             "fromVersion": r["fromVersion"], "toVersion": r["toVersion"], "verdict": r["verdict"],
             "outcome": r["outcome"], "brokeFiles": ", ".join(r.get("brokeFiles", [])),
             "note": r.get("note", "")} for r in rows]
