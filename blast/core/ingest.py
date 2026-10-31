"""Store one commit of a repo: files (nodes), imports (edges), libraries (packages).

Every attempt is logged in ingest_events. The database itself refuses a second
copy of the same commit (unique index on ingest_events.claim), so there is no
"check, then insert" race.
"""
from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
from pymongo.errors import DuplicateKeyError

from .parser import parse_repo


def now():
    return datetime.now(timezone.utc)


def git_ids(path: Path):
    def rev(ref):
        r = subprocess.run(["git", "-C", str(path), "rev-parse", ref],
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else None

    try:
        sha = rev("HEAD")
        parent = rev("HEAD~1") if sha else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, None
    return sha, parent


def content_id(path: Path) -> str:
    """Stand-in commit ID for folders that are not git repos."""
    h = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        if p.is_file() and (p.suffix == ".py" or p.name == "requirements.txt") and ".git" not in p.parts:
            h.update(p.relative_to(path).as_posix().encode())
            h.update(p.read_bytes())
    return "content-" + h.hexdigest()[:12]


def find_cycles(internal_edges) -> list[str]:
    g = nx.DiGraph((e["from"], e["to"]) for e in internal_edges)
    cycles = []
    for comp in nx.strongly_connected_components(g):
        if len(comp) > 1:
            loop = nx.find_cycle(g.subgraph(comp))
            cycles.append(" → ".join([u for u, _ in loop] + [loop[0][0]]))
    return sorted(cycles)


def remove_snapshot_rows(db, snapshot_id):
    for coll in ("nodes", "edges", "packages", "snapshots"):
        key = "_id" if coll == "snapshots" else "snapshotId"
        db[coll].delete_many({key: snapshot_id})


def diff_snapshots(db, old_id, new_id) -> dict:
    """Files added/removed/modified (by content hash) and library versions changed."""
    rows = db.nodes.aggregate([
        {"$match": {"snapshotId": {"$in": [old_id, new_id]}}},
        {"$group": {"_id": "$path", "versions": {"$push": {"s": "$snapshotId", "h": "$contentHash"}}}},
        {"$sort": {"_id": 1}},
    ])
    added, removed, modified = [], [], []
    for r in rows:
        v = {x["s"]: x["h"] for x in r["versions"]}
        if old_id not in v:
            added.append(r["_id"])
        elif new_id not in v:
            removed.append(r["_id"])
        elif v[old_id] != v[new_id]:
            modified.append(r["_id"])

    def pins(sid):
        return {p["name"]: p.get("pinnedVersion") or p.get("rule")
                for p in db.packages.find({"snapshotId": sid, "parseError": None})}

    old_p, new_p = pins(old_id), pins(new_id)
    packages = [{"name": n, "from": old_p.get(n), "to": new_p.get(n)}
                for n in sorted(set(old_p) | set(new_p)) if old_p.get(n) != new_p.get(n)]
    return {"added": added, "removed": removed, "modified": modified, "packagesChanged": packages}


def ingest_repo(db, repo_path, repo_name) -> dict:
    received = now()
    repo = (repo_name or "").strip()
    path = Path(repo_path) if repo_path else None

    if not repo or path is None or not path.is_dir():
        reason = "Repository name is missing." if not repo else f"Folder not found: {repo_path!r}."
        db.ingest_events.insert_one({"repo": repo or "(missing)", "commitSHA": "(none)",
                                     "receivedAt": received, "status": "rejected_invalid",
                                     "reason": reason, "payload": {"path": str(repo_path)}})
        return {"status": "rejected_invalid", "code": "INVALID_INPUT", "message": reason,
                "commitSHA": None, "snapshotId": None}

    sha, parent = git_ids(path)
    note = None
    if not sha:
        sha, parent = content_id(path), None
        note = "Not a git repository, so a hash of the file contents is used as the commit ID."
    key = f"{repo}@{sha}"

    try:
        event_id = db.ingest_events.insert_one({
            "claim": key, "repo": repo, "commitSHA": sha, "parentSHA": parent,
            "receivedAt": received, "status": "processing", "payload": {"path": str(path)},
        }).inserted_id
    except DuplicateKeyError:
        db.ingest_events.insert_one({"repo": repo, "commitSHA": sha, "receivedAt": received,
                                     "status": "rejected_duplicate", "reason": "DUPLICATE_COMMIT",
                                     "payload": {"path": str(path)}})
        return {"status": "rejected_duplicate", "code": "DUPLICATE_COMMIT",
                "message": f"{key} was already ingested, so nothing was stored twice.",
                "commitSHA": sha, "snapshotId": key}

    def finish(status, reason=None, keep_claim=False):
        update = {"$set": {"status": status, "reason": reason, "finishedAt": now()}}
        if not keep_claim:
            update["$unset"] = {"claim": ""}
        db.ingest_events.update_one({"_id": event_id}, update)

    try:
        parsed = parse_repo(path)
        if not parsed["files"]:
            finish("rejected_invalid", "No Python files found.")
            return {"status": "rejected_invalid", "code": "INVALID_INPUT",
                    "message": "No Python files were found in this repository.",
                    "commitSHA": sha, "snapshotId": None}

        edges = parsed["internalEdges"] + parsed["externalEdges"]
        db.nodes.insert_many([{**f, "snapshotId": key} for f in parsed["files"]])
        if edges:
            db.edges.insert_many([{**e, "snapshotId": key} for e in edges])
        if parsed["packages"]:
            db.packages.insert_many([{**p, "snapshotId": key} for p in parsed["packages"]])

        snapshot = {
            "_id": key, "repo": repo, "commitSHA": sha, "parentSHA": parent, "createdAt": now(),
            "nodeCount": len(parsed["files"]), "edgeCount": len(edges),
            "cycles": find_cycles(parsed["internalEdges"]),
            "parseErrors": [f["path"] for f in parsed["files"] if f["parseError"]],
            "changedFiles": None, "packagesChanged": None,
        }
        parent_id = f"{repo}@{parent}" if parent else None
        if parent_id and db.snapshots.find_one({"_id": parent_id}, {"_id": 1}):
            d = diff_snapshots(db, parent_id, key)
            snapshot["changedFiles"] = {k: d[k] for k in ("added", "removed", "modified")}
            snapshot["packagesChanged"] = d["packagesChanged"]
        db.snapshots.insert_one(snapshot)  # written last: a snapshot only exists when complete
        finish("accepted", note, keep_claim=True)
    except Exception as e:
        remove_snapshot_rows(db, key)  # never leave a half-written graph behind
        finish("failed", f"{type(e).__name__}: {e}")
        return {"status": "failed", "code": "INGEST_FAILED",
                "message": f"Ingest failed and was rolled back: {e}", "commitSHA": sha, "snapshotId": None}

    parts = [f"Stored {snapshot['nodeCount']} files and {snapshot['edgeCount']} imports of {key}."]
    if snapshot["parseErrors"]:
        parts.append(f"{len(snapshot['parseErrors'])} file(s) could not be parsed and were skipped.")
    if snapshot["cycles"]:
        parts.append(f"{len(snapshot['cycles'])} import cycle(s) found.")
    if note:
        parts.append(note)
    return {"status": "accepted", "code": None, "message": " ".join(parts),
            "commitSHA": sha, "snapshotId": key}
