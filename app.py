"""
Blast Radius - web front end (Streamlit).

A thin layer over the same core code the CLI uses. It only clones and READS
the analysed project; it never runs that project's code.

SERVICE CONTRACT
Create blast/core/service.py with these functions (cli.py can call them too).
For errors, raise an exception that has a `.code` attribute
(INVALID_INPUT, LIBRARY_NOT_FOUND, ...).

  init() -> None
      Create collections, validators and indexes. Safe to call repeatedly.

  ingest_repo(repo_path: str, repo_name: str) -> dict
      Ingest HEAD of a local git checkout.
      {"status": "accepted" | "rejected_duplicate" | "rejected_invalid" | "failed",
       "code": str | None, "message": str, "commitSHA": str, "snapshotId": str}

  list_packages(repo_name: str) -> list[dict]
      From the latest snapshot: [{"name", "pinnedVersion", "rule", "parseError"}]

  simulate_bump(repo_name: str, library: str, target_version: str) -> dict
      The same data as `blast bump --json`:
      {"runId", "verdict": "SAFE TO BUMP" | "BUMP WITH CARE" | "DO NOT BUMP YET",
       "library", "fromVersion", "toVersion", "jump", "apiStatus": "ok" | "unavailable",
       "ruleNote": str | None, "advisory": str | None, "pastWarning": str | None,
       "apiChanges": {"removed": [str],
                      "signatureChanged": [{"name", "old", "new"}],
                      "added": [str]},
       "affected": [{"path", "depth", "label", "reasons": [str], "importedNames": [str],
                     "fanIn": int, "hasTest": bool, "via": str | None}],
       "order": [{"files": [str], "task": str}]}
      "via" = the affected file this one imports on its way to the library
      (None for files at depth 1). It is used to draw the blast graph.

  record_outcome(run_id: str, outcome: "ok" | "broke", files: list[str], note: str) -> None

  get_history(repo_name: str, library: str) -> list[dict]
      [{"runId", "createdAt", "fromVersion", "toVersion", "verdict",
        "outcome", "brokeFiles", "note"}]

  get_events(repo_name: str) -> list[dict]
      [{"receivedAt", "commitSHA", "status", "reason"}]

Until that module exists (or when BLAST_DEMO=1), the page runs on built-in
demo data, so it can be deployed right away.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Blast Radius", page_icon="💥", layout="wide")

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Streamlit secrets -> environment variables, so core/db.py can use os.environ.
try:
    if st.secrets.load_if_toml_exists():
        for _key in ("MONGODB_URI", "MONGODB_DB", "BLAST_DEMO"):
            if _key in st.secrets and _key not in os.environ:
                os.environ[_key] = str(st.secrets[_key])
except Exception:
    pass  # No secrets file (local run): .env or real environment variables are used.

LABEL_RANK = {"LIKELY SAFE": 0, "CHECK THIS": 1, "LIKELY BREAKS": 2}
LABEL_ICON = {"LIKELY SAFE": "🟢", "CHECK THIS": "🟡", "LIKELY BREAKS": "🔴"}
LABEL_COLOR = {"LIKELY SAFE": "#2e8b57", "CHECK THIS": "#c99700", "LIKELY BREAKS": "#c0392b"}
GITHUB_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(\.git)?/?$")
OTHER = "Other (type a name)"


class BlastError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def verdict_for(affected):
    labels = {a["label"] for a in affected}
    if "LIKELY BREAKS" in labels:
        return "DO NOT BUMP YET"
    if "CHECK THIS" in labels:
        return "BUMP WITH CARE"
    return "SAFE TO BUMP"


# --------------------------------------------------------------------------
# Demo service: fake data in the exact shape of the contract above.
# --------------------------------------------------------------------------
class DemoService:
    def __init__(self):
        self.events = []
        self.snapshots = set()
        self.runs = []

    def init(self):
        return None

    def ingest_repo(self, repo_path, repo_name):
        if not repo_name:
            raise BlastError("INVALID_INPUT", "Repository name is missing.")
        sha = hashlib.sha1(repo_name.encode()).hexdigest()[:12]
        if (repo_name, sha) in self.snapshots:
            status, code = "rejected_duplicate", "DUPLICATE_COMMIT"
            msg = f"{repo_name}@{sha} was already ingested, so nothing was stored twice."
        else:
            self.snapshots.add((repo_name, sha))
            status, code = "accepted", None
            msg = f"Stored the import graph of {repo_name}@{sha}."
        self.events.append({"receivedAt": now(), "commitSHA": sha,
                            "status": status, "reason": code or ""})
        return {"status": status, "code": code, "message": msg,
                "commitSHA": sha, "snapshotId": f"{repo_name}@{sha}"}

    def list_packages(self, repo_name):
        return [
            {"name": "fakehttp", "pinnedVersion": "1.0", "rule": "==1.0", "parseError": None},
            {"name": "pyyaml", "pinnedVersion": "5.3", "rule": "==5.3", "parseError": None},
            {"name": "brokenlib", "pinnedVersion": None, "rule": None,
             "parseError": "Line 7 of requirements.txt could not be read: 'brokenlib=>2'"},
        ]

    def simulate_bump(self, repo_name, library, target_version):
        pinned = {p["name"]: p for p in self.list_packages(repo_name) if p["pinnedVersion"]}
        if library not in pinned:
            raise BlastError("LIBRARY_NOT_FOUND", f"{library} is not pinned in this repo's requirements.")
        if not target_version.strip():
            raise BlastError("INVALID_INPUT", "Enter a target version, for example 2.0.")

        if library == "fakehttp":
            report = self._fakehttp_report()
        else:
            report = {
                "jump": "patch", "apiStatus": "ok", "ruleNote": None, "pastWarning": None,
                "advisory": "The current version 5.3 has a known issue, fixed in 5.4.",
                "apiChanges": {"removed": [], "signatureChanged": [], "added": []},
                "affected": [
                    {"path": "config/loader.py", "depth": 1, "label": "LIKELY SAFE",
                     "reasons": ["uses only unchanged names"], "importedNames": ["safe_load"],
                     "fanIn": 3, "hasTest": True, "via": None},
                ],
                "order": [{"files": ["config/loader.py"], "task": "No code change needed; run tests/test_config.py."}],
            }
        report.update({"runId": uuid.uuid4().hex[:8], "library": library,
                       "fromVersion": pinned[library]["pinnedVersion"], "toVersion": target_version.strip()})

        past = [r for r in self.runs
                if r["repo"] == repo_name and r["library"] == library and r["outcome"] == "broke"]
        if past:
            last = past[-1]
            report["pastWarning"] = (f"Last time ({last['createdAt']}, {last['fromVersion']} → {last['toVersion']}) "
                                     f"{len(last['brokeFiles'])} file(s) broke: {', '.join(last['brokeFiles'])}.")
            for item in report["affected"]:
                if item["path"] in last["brokeFiles"]:
                    raised = min(LABEL_RANK[item["label"]] + 1, 2)
                    item["label"] = next(k for k, v in LABEL_RANK.items() if v == raised)
                    item["reasons"].append("broke last time")
        report["verdict"] = verdict_for(report["affected"])

        self.runs.append({"runId": report["runId"], "repo": repo_name, "library": library,
                          "createdAt": now(), "fromVersion": report["fromVersion"],
                          "toVersion": report["toVersion"], "verdict": report["verdict"],
                          "outcome": "unknown", "brokeFiles": [], "note": ""})
        return report

    def _fakehttp_report(self):
        def f(path, depth, label, reason, names, fan_in, has_test, via=None):
            return {"path": path, "depth": depth, "label": label, "reasons": [reason],
                    "importedNames": names, "fanIn": fan_in, "hasTest": has_test, "via": via}

        return {
            "jump": "major", "apiStatus": "ok", "advisory": None, "pastWarning": None,
            "ruleNote": "The pin ==1.0 must change in requirements.txt too.",
            "apiChanges": {
                "removed": ["fetch_all"],
                "signatureChanged": [
                    {"name": "get", "old": "get(url, timeout=10)", "new": "get(url, timeout_s=10)"},
                    {"name": "post", "old": "post(url, data)", "new": "post(url, data, headers)"},
                ],
                "added": ["retry: new optional parameter backoff", "stream"],
            },
            "affected": [
                f("services/sync.py", 1, "LIKELY BREAKS", "imports fetch_all, removed in 2.0 (line 3)", ["fetch_all"], 2, False),
                f("services/orders.py", 1, "LIKELY BREAKS", "line 42: post(url, data) but 2.0 needs 3 arguments", ["post"], 4, True),
                f("clients/catalog.py", 1, "LIKELY BREAKS", "line 17: keyword timeout no longer exists in get()", ["get"], 2, True),
                f("clients/proxy.py", 1, "CHECK THIS", "post(*args): argument count cannot be known", ["post"], 1, False),
                f("services/payments.py", 1, "LIKELY SAFE", "post(url, data, headers) already passes 3 arguments", ["post"], 3, True),
                f("utils/retrying.py", 1, "LIKELY SAFE", "retry(fn, 3): the new parameter has a default", ["retry"], 2, True),
                f("api/routes.py", 2, "CHECK THIS", "reached through services/orders.py", [], 5, True, "services/orders.py"),
                f("jobs/nightly.py", 2, "CHECK THIS", "reached through services/sync.py", [], 1, False, "services/sync.py"),
                f("main.py", 3, "CHECK THIS", "reached through api/routes.py", [], 0, False, "api/routes.py"),
            ],
            "order": [
                {"files": ["clients/catalog.py"], "task": "Rename timeout to timeout_s in get(); run tests/test_catalog.py."},
                {"files": ["services/orders.py"], "task": "Add headers to post() at line 42; run tests/test_orders.py."},
                {"files": ["services/sync.py"], "task": "Replace fetch_all; no test imports this file, so test it by hand."},
                {"files": ["clients/proxy.py"], "task": "Check what *args holds when post() is called."},
                {"files": ["api/routes.py", "jobs/nightly.py"], "task": "Re-run after the fixes above; no direct change expected."},
                {"files": ["main.py"], "task": "Smoke-test the app start."},
            ],
        }

    def record_outcome(self, run_id, outcome, files, note):
        for r in self.runs:
            if r["runId"] == run_id:
                r.update({"outcome": outcome, "brokeFiles": list(files), "note": note})
                return None
        raise BlastError("RUN_NOT_FOUND", f"No bump run with id {run_id}.")

    def get_history(self, repo_name, library):
        rows = [r for r in self.runs if r["repo"] == repo_name and r["library"] == library]
        return list(reversed(rows))

    def get_events(self, repo_name):
        return list(reversed(self.events))


# --------------------------------------------------------------------------
# Loading the real core (or falling back to demo data)
# --------------------------------------------------------------------------
@st.cache_resource
def load_service():
    if os.environ.get("BLAST_DEMO") == "1":
        return DemoService(), "demo", "BLAST_DEMO=1 is set."
    for name in ("blast.core.service", "core.service"):
        try:
            module = __import__(name, fromlist=["service"])
        except ModuleNotFoundError as e:
            if e.name in ("blast", "blast.core", "blast.core.service", "core", "core.service"):
                continue  # this layout does not exist; try the next one
            return DemoService(), "demo", traceback.format_exc()
        except Exception:
            return DemoService(), "demo", traceback.format_exc()
        try:
            module.init()
        except Exception:
            return DemoService(), "demo", traceback.format_exc()
        return module, "live", None
    return DemoService(), "demo", "blast/core/service.py does not exist yet."


@st.cache_resource
def build_sample_repo():
    """Generate the sample repo (with real git commits) into a temp folder.
    A folder with its own .git cannot be pushed inside your GitHub repo,
    so it is created fresh on the server instead."""
    script = ROOT / "scripts" / "generate_sample_repo.py"
    if not script.exists():
        raise BlastError("SAMPLE_MISSING", "scripts/generate_sample_repo.py was not found.")
    out = Path(tempfile.mkdtemp(prefix="blast_sample_")) / "sample_repo"
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "Blast Demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
           "GIT_COMMITTER_NAME": "Blast Demo", "GIT_COMMITTER_EMAIL": "demo@example.com"}
    try:
        subprocess.run([sys.executable, str(script), str(out)], check=True, timeout=120,
                       capture_output=True, text=True, env=env, cwd=ROOT)
    except subprocess.CalledProcessError as e:
        raise BlastError("SAMPLE_FAILED", (e.stderr or str(e)).strip()[-500:])
    except subprocess.TimeoutExpired:
        raise BlastError("SAMPLE_FAILED", "Generating the sample repo took longer than 2 minutes.")
    return str(out)


def repo_name_from_url(url):
    path = url.strip().rstrip("/").removeprefix("https://github.com/")
    return path.removesuffix(".git")


def clone_public_repo(url):
    url = url.strip()
    if not GITHUB_URL.match(url):
        raise BlastError("INVALID_INPUT", "Paste a public GitHub URL like https://github.com/owner/name")
    dest = Path(tempfile.mkdtemp(prefix="blast_clone_")) / "repo"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # private repos fail fast instead of hanging
    try:
        subprocess.run(["git", "clone", "--depth", "2", "--quiet", url, str(dest)],
                       check=True, timeout=90, capture_output=True, text=True, env=env)
    except subprocess.CalledProcessError:
        raise BlastError("CLONE_FAILED", "Could not clone the repo. Check that it is public and the URL is right.")
    except subprocess.TimeoutExpired:
        raise BlastError("CLONE_FAILED", "Cloning took longer than 90 seconds. Try a smaller repo.")
    return str(dest)


def call(fn, *args):
    """Run a service call; show errors in one consistent shape."""
    try:
        return True, fn(*args)
    except Exception as e:
        code = getattr(e, "code", type(e).__name__)
        st.error(f"**{code}**: {e}")
        return False, None


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def esc(text):
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def blast_graph(report):
    lines = [
        "digraph G {", "rankdir=LR;", 'bgcolor="transparent";',
        'node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11, fontcolor="white"];',
        'edge [color="#8a8a8a"];',
        f'"__lib" [label="{esc(report["library"])} {esc(report["toVersion"])}", fillcolor="#4a4a4a"];',
    ]
    for a in report["affected"]:
        lines.append(f'"{esc(a["path"])}" [fillcolor="{LABEL_COLOR.get(a["label"], "#777777")}"];')
        source = esc(a["via"]) if a.get("via") else "__lib"
        lines.append(f'"{source}" -> "{esc(a["path"])}";')
    lines.append("}")
    return "\n".join(lines)


def render_report(svc, report):
    affected = report["affected"]
    counts = {k: sum(1 for a in affected if a["label"] == k) for k in LABEL_RANK}
    summary = (f"{len(affected)} affected: {counts['LIKELY BREAKS']} likely break, "
               f"{counts['CHECK THIS']} to check, {counts['LIKELY SAFE']} likely safe")
    verdict = report["verdict"]
    banner = f"### {verdict}\n{summary}"
    if verdict == "SAFE TO BUMP":
        st.success(banner, icon="✅")
    elif verdict == "BUMP WITH CARE":
        st.warning(banner, icon="⚠️")
    else:
        st.error(banner, icon="⛔")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Version change", f"{report['fromVersion']} → {report['toVersion']}")
    c2.metric("Jump", report["jump"])
    c3.metric("API comparison", report["apiStatus"])
    c4.metric("Run ID", report["runId"])

    if report.get("pastWarning"):
        st.warning(report["pastWarning"], icon="🕘")
    if report.get("advisory"):
        st.info(report["advisory"], icon="🛡️")
    if report.get("ruleNote"):
        st.info(report["ruleNote"], icon="📌")
    if report["apiStatus"] != "ok":
        st.info("API comparison unavailable, so labels use the version jump only.", icon="ℹ️")

    st.subheader("What changed in the library")
    changes = report["apiChanges"]
    a1, a2, a3 = st.columns(3)
    with a1:
        st.markdown("**Removed**")
        st.markdown("\n".join(f"- `{n}`" for n in changes["removed"]) or "None")
    with a2:
        st.markdown("**Signature changed**")
        st.markdown("\n".join(f"- `{c['old']}` became `{c['new']}`" for c in changes["signatureChanged"]) or "None")
    with a3:
        st.markdown("**Added**")
        st.markdown("\n".join(f"- {n}" for n in changes["added"]) or "None")

    st.subheader("Blast radius")
    st.caption("Arrows go from the library to the files that import it, and onward to the files that import those.")
    if affected:
        st.graphviz_chart(blast_graph(report), use_container_width=True)

    rows = sorted(affected, key=lambda a: (-LABEL_RANK.get(a["label"], 0), a["depth"], a["path"]))
    st.dataframe(
        [{"": LABEL_ICON.get(a["label"], ""), "File": a["path"], "Depth": a["depth"],
          "Label": a["label"], "Why": "; ".join(a["reasons"]),
          "Names used": ", ".join(a["importedNames"]), "Fan-in": a["fanIn"],
          "A test imports it": "yes" if a["hasTest"] else "no"} for a in rows],
        use_container_width=True, hide_index=True,
    )

    st.subheader("Fix in this order")
    for i, step in enumerate(report["order"], 1):
        files = ", ".join(f"`{p}`" for p in step["files"])
        together = " (fix these together)" if len(step["files"]) > 1 else ""
        st.markdown(f"{i}. {files}{together}: {step['task']}")

    st.download_button("Download report as JSON", json.dumps(report, indent=2),
                       file_name=f"blast_{report['library']}_{report['runId']}.json",
                       mime="application/json")

    st.subheader("Record what really happened")
    st.caption("Next time someone bumps this library, the result is shown as a warning.")
    with st.form(f"outcome_{report['runId']}"):
        outcome = st.radio("Outcome", ["broke", "ok"], horizontal=True)
        files = st.multiselect("Files that broke", [a["path"] for a in affected])
        note = st.text_input("Note (optional)")
        if st.form_submit_button("Save outcome"):
            ok, _ = call(svc.record_outcome, report["runId"], outcome,
                         files if outcome == "broke" else [], note)
            if ok:
                st.success("Outcome saved. Run the same bump again to see the warning.")


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
svc, mode, mode_note = load_service()

st.title("💥 Blast Radius")
st.write("If I bump this library to that version, what in my code breaks, "
         "in what order do I fix it, and did this go wrong last time?")

if mode == "demo":
    st.info("Demo mode: the results below are built-in sample data.", icon="🧪")
    if mode_note and "Traceback" in mode_note:
        with st.expander("The real analysis engine failed to load. Details"):
            st.code(mode_note)

with st.sidebar:
    st.header("Choose a repository")
    source = st.radio("Source", ["Sample repo with planted problems", "Public GitHub repo"])
    use_sample = source.startswith("Sample")
    url = ""
    if not use_sample:
        url = st.text_input("Repository URL", placeholder="https://github.com/owner/name")
        st.caption("Python projects with a requirements.txt. The code is read, never run.")

    if st.button("Ingest repository", type="primary", use_container_width=True):
        with st.spinner("Reading the repository…"):
            try:
                if use_sample:
                    repo_name = "sample_repo"
                    path = "" if mode == "demo" else build_sample_repo()
                else:
                    path = clone_public_repo(url)
                    repo_name = repo_name_from_url(url)
                ok, result = call(svc.ingest_repo, path, repo_name)
            except Exception as e:
                st.error(f"**{getattr(e, 'code', type(e).__name__)}**: {e}")
                ok = False
        if ok:
            st.session_state.last_ingest = result
            if result["status"] in ("accepted", "rejected_duplicate"):
                st.session_state.repo_name = repo_name
                st.session_state.pop("report", None)

    last = st.session_state.get("last_ingest")
    if last:
        if last["status"] == "accepted":
            st.success(last["message"])
        elif last["status"] == "rejected_duplicate":
            st.warning(f"**DUPLICATE_COMMIT**: {last['message']} "
                       "The database's unique index blocked the second copy.")
        else:
            st.error(f"**{last.get('code') or last['status']}**: {last['message']}")

    if "repo_name" in st.session_state:
        st.markdown(f"Current repository: `{st.session_state.repo_name}`")

repo = st.session_state.get("repo_name")
if not repo:
    st.markdown("Pick a repository in the sidebar and select **Ingest repository** to start. "
                "The sample repo has a library upgrade with planted breaking changes.")
    st.stop()

tab_bump, tab_history, tab_log = st.tabs(["Simulate a bump", "Bump history", "Ingest log"])

ok, packages = call(svc.list_packages, repo)
packages = packages or []
pinned = {p["name"]: p.get("pinnedVersion") for p in packages if not p.get("parseError")}
names = list(pinned)

with tab_bump:
    bad = [p for p in packages if p.get("parseError")]
    if bad:
        with st.expander(f"{len(bad)} requirement line(s) could not be read and were skipped"):
            for p in bad:
                st.markdown(f"- {p['parseError']}")

    c1, c2 = st.columns([2, 1])
    choice = c1.selectbox("Library", names + [OTHER],
                          format_func=lambda n: n if n == OTHER else f"{n} (pinned {pinned.get(n) or 'by a range'})")
    library = c1.text_input("Library name") if choice == OTHER else choice
    target = c2.text_input("Target version", placeholder="2.0")

    if st.button("Simulate bump", type="primary"):
        with st.spinner("Comparing versions and walking the import graph…"):
            ok, report = call(svc.simulate_bump, repo, (library or "").strip(), target)
        if ok:
            st.session_state.report = report

    if st.session_state.get("report"):
        st.divider()
        render_report(svc, st.session_state.report)

with tab_history:
    lib = st.selectbox("Library", names, key="history_lib")
    if lib:
        ok, runs = call(svc.get_history, repo, lib)
        if ok and runs:
            st.dataframe(runs, use_container_width=True, hide_index=True)
        elif ok:
            st.write("No bumps of this library yet. Simulate one in the first tab.")

with tab_log:
    st.caption("Every ingest attempt, including rejected duplicates and invalid input.")
    ok, events = call(svc.get_events, repo)
    if ok and events:
        st.dataframe(events, use_container_width=True, hide_index=True)
    elif ok:
        st.write("No ingest attempts recorded yet.")
