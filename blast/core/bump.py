"""Simulate bumping one library: which files are hit, how badly, and in what order to fix them."""
from __future__ import annotations

import uuid

import networkx as nx
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from . import apidiff, history
from .errors import BlastError
from .parser import normalize

SAFE, CHECK, BREAKS = "LIKELY SAFE", "CHECK THIS", "LIKELY BREAKS"
RANK = {SAFE: 0, CHECK: 1, BREAKS: 2}
BY_RANK = {v: k for k, v in RANK.items()}


def classify_jump(current: str, target: str) -> str:
    a, b = Version(current), Version(target)
    if b == a:
        return "same"
    if b < a:
        return "downgrade"
    ra, rb = a.release + (0, 0, 0), b.release + (0, 0, 0)
    if ra[0] != rb[0]:
        return "major"
    if ra[1] != rb[1]:
        return "minor"
    return "patch"


def advisory_note(db, library, current, target):
    notes = []
    for adv in db.advisories.find({"name": library}):
        try:
            spec = SpecifierSet(adv["affectedRange"])
        except InvalidSpecifier:
            continue
        if current and spec.contains(Version(current), prereleases=True):
            notes.append(f"The current version {current} has a known issue ({adv['note']}), "
                         f"fixed in {adv['fixedIn']}.")
            if spec.contains(Version(target), prereleases=True):
                notes.append(f"The target {target} is still affected.")
        elif spec.contains(Version(target), prereleases=True):
            notes.append(f"The target {target} has a known issue ({adv['note']}), fixed in {adv['fixedIn']}.")
    return " ".join(notes) or None


def check_call(call: dict, params: list, lib_label: str):
    """Compare one recorded call with the new signature."""
    name, line = call["name"], call["line"]
    if call["hasStarArgs"]:
        return CHECK, f"line {line}: {name}() is called with *args or **kwargs, so its arguments cannot be counted"
    by_name = {p["name"]: p for p in params}
    has_varkw = any(p["kind"] == "varkw" for p in params)
    has_varargs = any(p["kind"] == "varargs" for p in params)
    for kw in call["keywordNames"]:
        if (kw not in by_name or by_name[kw]["kind"] == "posonly") and not has_varkw:
            return BREAKS, f"line {line}: {name}() is called with keyword {kw}, which {lib_label} no longer accepts"
    positional = [p for p in params if p["kind"] in ("posonly", "pos")]
    if call["positionalCount"] > len(positional) and not has_varargs:
        return BREAKS, (f"line {line}: {name}() gets {call['positionalCount']} positional arguments, "
                        f"but {lib_label} takes at most {len(positional)}")
    covered = {p["name"] for p in positional[:call["positionalCount"]]} | set(call["keywordNames"])
    missing = [p["name"] for p in params
               if p["kind"] in ("posonly", "pos", "kwonly") and not p["hasDefault"] and p["name"] not in covered]
    if missing:
        return BREAKS, f"line {line}: {name}() call does not pass {', '.join(missing)}, which {lib_label} requires"
    return SAFE, f"line {line}: {name}() call already fits the {lib_label} signature"


def label_direct(edges: list, api: dict, lib_label: str, jump: str, has_test: bool):
    """Label a file that imports the library itself. Returns (label, reasons)."""
    if api["status"] != "ok":
        if jump == "major" and not has_test:
            return BREAKS, [f"major version jump, and no test imports this file"]
        if jump in ("major", "downgrade", "unknown"):
            return CHECK, [f"{jump} version change; API comparison unavailable"]
        return SAFE, [f"{jump} version change"]

    removed = set(api["removed"])
    hints = {h["name"]: h["hint"] for h in api.get("renameHints", [])}
    new_params = {c["name"]: c["params"] for c in api.get("newParams", [])}
    results = []
    used = []
    for e in edges:
        for name in e["names"]:
            used.append(name)
            line = e.get("nameLines", {}).get(name, e["line"])
            if name == "*":
                results.append((CHECK, f"line {line}: star import, so the names used cannot be known"))
            elif name in removed:
                hint = f"; possibly renamed to {hints[name]}" if name in hints else ""
                results.append((BREAKS, f"uses {name} (line {line}), which {lib_label} removes{hint}"))
            elif name in new_params:
                calls = [c for c in e["calls"] if c["name"] == name]
                if not calls:
                    results.append((CHECK, f"{name}() changed in {lib_label} but is not called directly here"))
                for c in calls:
                    results.append(check_call(c, new_params[name], lib_label))
    worst = max((RANK[l] for l, _ in results), default=0)
    if worst == 0:
        safe_calls = [r for l, r in results if l == SAFE]
        reason = safe_calls[0] if safe_calls else (
            f"uses only unchanged names: {', '.join(sorted(set(used)))}" if used else "no library names used directly")
        return SAFE, [reason]
    return BY_RANK[worst], [r for l, r in results if RANK[l] == worst]


def find_affected(db, sid: str, library: str):
    """Direct importers, then everything that imports them, via $graphLookup."""
    direct_edges: dict[str, list] = {}
    for e in db.edges.find({"snapshotId": sid, "kind": "external", "to": library}):
        direct_edges.setdefault(e["from"], []).append(e)

    rows = db.edges.aggregate([
        {"$match": {"snapshotId": sid, "kind": "external", "to": library}},
        {"$group": {"_id": "$from"}},
        {"$graphLookup": {
            "from": "edges", "startWith": "$_id",
            "connectFromField": "from", "connectToField": "to",
            "as": "dependents", "depthField": "hops", "maxDepth": 10,
            "restrictSearchWithMatch": {"snapshotId": sid, "kind": "internal"},
        }},
    ])
    depth = {path: 1 for path in direct_edges}
    for row in rows:
        for d in row["dependents"]:
            depth[d["from"]] = min(depth.get(d["from"], 99), d["hops"] + 2)
    return direct_edges, depth


def build_order(paths, internal_edges, labels):
    """Files that others depend on come first; files in a cycle are fixed together."""
    g = nx.DiGraph()
    g.add_nodes_from(paths)
    g.add_edges_from((e["to"], e["from"]) for e in internal_edges)  # dependency -> dependent
    cond = nx.condensation(g)
    members = {n: sorted(cond.nodes[n]["members"]) for n in cond.nodes}

    def key(n):
        worst = max(RANK[labels[p]] for p in members[n])
        return f"{2 - worst}{members[n][0]}"

    return [members[n] for n in nx.lexicographical_topological_sort(cond, key=key)]


def simulate_bump(db, repo: str, library: str, target: str) -> dict:
    library = normalize((library or "").strip())
    target = (target or "").strip()
    if not library:
        raise BlastError("INVALID_INPUT", "Choose a library.")
    snap = db.snapshots.find_one({"repo": repo}, sort=[("createdAt", -1)])
    if not snap:
        raise BlastError("NO_SNAPSHOT", f"Ingest {repo} first.")
    sid = snap["_id"]
    pkg = db.packages.find_one({"snapshotId": sid, "name": library, "parseError": None})
    if not pkg:
        raise BlastError("LIBRARY_NOT_FOUND", f"{library} is not in this repo's requirements.txt.")
    try:
        target_v = Version(target)
    except InvalidVersion:
        raise BlastError("INVALID_INPUT", f"{target!r} is not a valid version number.")

    current = pkg.get("pinnedVersion")
    jump = classify_jump(current, target) if current else "unknown"
    if jump == "same":
        raise BlastError("INVALID_INPUT", f"{library} is already pinned to {current}.")
    lib_label = f"{library} {target}"

    rule_note = None
    if pkg.get("rule"):
        try:
            if not SpecifierSet(pkg["rule"]).contains(target_v, prereleases=True):
                rule_note = (f"The requirement {library}{pkg['rule']} does not allow {target}, "
                             f"so requirements.txt must change too.")
        except InvalidSpecifier:
            pass

    if current:
        api = apidiff.compare(db, library, current, target)
    else:
        api = {"status": "unavailable", "reason": "the current version is a range, not one pinned version",
               "removed": [], "signatureChanged": [], "added": [], "renameHints": [], "newParams": []}

    nodes = {n["path"]: n for n in db.nodes.find({"snapshotId": sid}, {"_id": 0, "path": 1, "kind": 1,
                                                                        "fanIn": 1, "hasTest": 1})}
    direct_edges, depth = find_affected(db, sid, library)
    # Tests that only reach the library through other files are the safety net, not the blast.
    depth = {p: d for p, d in depth.items() if d == 1 or nodes.get(p, {}).get("kind") != "test"}
    paths = sorted(depth, key=lambda p: (depth[p], p))

    internal = list(db.edges.find({"snapshotId": sid, "kind": "internal",
                                   "from": {"$in": paths}, "to": {"$in": paths}}, {"from": 1, "to": 1}))
    imports_of: dict[str, list] = {}
    for e in internal:
        imports_of.setdefault(e["from"], []).append(e["to"])

    labels, reasons, via = {}, {}, {}
    for p in paths:  # sorted by depth, so the file a dependent goes through is labelled first
        node = nodes.get(p, {})
        if depth[p] == 1:
            labels[p], reasons[p] = label_direct(direct_edges[p], api, lib_label, jump, node.get("hasTest", False))
            via[p] = None
        else:
            options = [t for t in imports_of.get(p, []) if depth.get(t) == depth[p] - 1]
            through = max(options, key=lambda t: (RANK[labels[t]], t)) if options else None
            via[p] = through
            labels[p] = CHECK if through and labels[through] != SAFE else SAFE
            reasons[p] = [f"reached through {through}" if through else "reached through other files"]

    past = history.last_broke(db, repo, library)
    past_warning = None
    if past:
        broke = past.get("brokeFiles", [])
        past_warning = (f"Last time ({past['createdAt'].strftime('%Y-%m-%d')}, {past['fromVersion']} → "
                        f"{past['toVersion']}) {len(broke)} file(s) broke: {', '.join(broke) or 'none named'}.")
        for p in paths:
            if p in broke:
                labels[p] = BY_RANK[min(RANK[labels[p]] + 1, 2)]
                reasons[p].append("broke last time")

    tests_for: dict[str, list] = {}
    for e in db.edges.find({"snapshotId": sid, "kind": "internal", "to": {"$in": paths}}, {"from": 1, "to": 1}):
        if nodes.get(e["from"], {}).get("kind") == "test":
            tests_for.setdefault(e["to"], []).append(e["from"])

    def task(p):
        if labels[p] == SAFE:
            first = "No change expected"
        elif depth[p] == 1:
            first = "Fix: " + reasons[p][0]
        else:
            first = "Re-check once the files it imports are fixed"
        tests = sorted(tests_for.get(p, []))
        return f"{first}; " + (f"run {', '.join(tests)}." if tests else "no test imports this file, so check it by hand.")

    def group_task(group):
        if len(group) == 1:
            return task(group[0])
        worst_first = sorted(group, key=lambda p: (-RANK[labels[p]], p))
        return "Import cycle, so fix these together. " + " ".join(f"{p}: {task(p)}" for p in worst_first)

    order = [{"files": group, "task": group_task(group)} for group in build_order(paths, internal, labels)]

    affected = [{"path": p, "depth": depth[p], "label": labels[p], "reasons": reasons[p],
                 "importedNames": sorted({n for e in direct_edges.get(p, []) for n in e["names"]}),
                 "fanIn": nodes.get(p, {}).get("fanIn", 0), "hasTest": nodes.get(p, {}).get("hasTest", False),
                 "via": via[p]} for p in paths]

    all_labels = set(labels.values())
    verdict = ("DO NOT BUMP YET" if BREAKS in all_labels else
               "BUMP WITH CARE" if CHECK in all_labels else "SAFE TO BUMP")

    report = {
        "runId": uuid.uuid4().hex[:8], "verdict": verdict, "library": library,
        "fromVersion": current or f"range {pkg.get('rule')}", "toVersion": target, "jump": jump,
        "apiStatus": api["status"], "apiReason": api.get("reason"),
        "ruleNote": rule_note, "advisory": advisory_note(db, library, current, target),
        "pastWarning": past_warning, "cycles": snap.get("cycles", []),
        "apiChanges": {
            "removed": [f"{n} (possibly renamed to {h['hint']})" if (h := next(
                (x for x in api.get("renameHints", []) if x["name"] == n), None)) else n
                for n in api["removed"]],
            "signatureChanged": [{"name": c["name"], "old": c["old"], "new": c["new"]}
                                 for c in api["signatureChanged"]],
            "added": api["added"],
        },
        "affected": affected, "order": order,
    }
    history.save_run(db, repo, sid, report)
    return report
