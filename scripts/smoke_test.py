"""End-to-end check of the whole engine against your MongoDB.

Usage:  python scripts/smoke_test.py
Uses a separate database (blast_smoke), which it wipes first, so your demo data is safe.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["MONGODB_DB"] = "blast_smoke"

from blast.core import service  # noqa: E402
from blast.core.db import get_db  # noqa: E402
from blast.core.errors import BlastError  # noqa: E402

failures = 0


def check(cond, msg):
    global failures
    print(("PASS " if cond else "FAIL ") + msg)
    failures += 0 if cond else 1


try:
    get_db().client.admin.command("ping")
except BlastError as e:
    sys.exit(f"Cannot start: {e}")
except Exception as e:
    sys.exit("Cannot connect to MongoDB. Check three things in Atlas:\n"
             "  1. Network Access has 0.0.0.0/0 (or your current IP)\n"
             "  2. The password in MONGODB_URI is right (no < > around it)\n"
             "  3. The cluster is running (not paused)\n"
             f"Details: {type(e).__name__}: {str(e)[:200]}")

get_db().client.drop_database("blast_smoke")
service.init()
service.init()  # must be safe to run twice

repo_dir = Path(tempfile.mkdtemp()) / "sample_repo"
subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_sample_repo.py"), str(repo_dir)], check=True)

r = service.ingest_repo(str(repo_dir), "sample_repo")
check(r["status"] == "accepted", f"first ingest accepted: {r['message']}")
r = service.ingest_repo(str(repo_dir), "sample_repo")
check(r["status"] == "rejected_duplicate", "second ingest rejected as DUPLICATE_COMMIT")
r = service.ingest_repo("/no/such/folder", "sample_repo")
check(r["status"] == "rejected_invalid", "missing folder rejected as INVALID_INPUT")
events = service.get_events("sample_repo")
check(len(events) == 3, f"ingest log has all 3 attempts: {[e['status'] for e in events]}")

pk = {p["name"]: p for p in service.list_packages("sample_repo")}
check(pk["fakehttp"]["pinnedVersion"] == "1.0", "fakehttp pinned at 1.0")
check(any(p["parseError"] for p in pk.values()), "broken requirements line recorded, not fatal")

rep = service.simulate_bump("sample_repo", "fakehttp", "2.0")
labels = {a["path"]: a["label"] for a in rep["affected"]}
expected = {
    "shop/services/orders.py": "LIKELY BREAKS", "shop/services/sync.py": "LIKELY BREAKS",
    "shop/clients/catalog.py": "LIKELY BREAKS", "shop/clients/proxy.py": "CHECK THIS",
    "shop/services/payments.py": "LIKELY SAFE", "shop/utils/retrying.py": "LIKELY SAFE",
    "shop/health.py": "LIKELY SAFE", "shop/api/routes.py": "CHECK THIS",
    "shop/services/inventory.py": "CHECK THIS", "shop/jobs/nightly.py": "CHECK THIS",
    "shop/main.py": "CHECK THIS",
}
check(rep["verdict"] == "DO NOT BUMP YET", f"verdict: {rep['verdict']}")
check(labels == expected, "labels match the hand-worked answer")
for p, want in expected.items():
    if labels.get(p) != want:
        print(f"     {p}: got {labels.get(p)}, expected {want}")
check(any(len(s["files"]) > 1 for s in rep["order"]), "import cycle shown as 'fix together'")
for a in rep["affected"]:
    print(f"     {a['label']:<14} d{a['depth']} {a['path']:<28} {a['reasons'][0]}")

service.record_outcome(rep["runId"], "broke", ["shop/services/payments.py"], "payments failed in staging")
rep2 = service.simulate_bump("sample_repo", "fakehttp", "2.0")
check(bool(rep2["pastWarning"]), f"memory warning: {rep2['pastWarning']}")
pay = next(a for a in rep2["affected"] if a["path"] == "shop/services/payments.py")
check(pay["label"] == "CHECK THIS", "payments.py raised one level because it broke last time")
check(len(service.get_history("sample_repo", "fakehttp")) == 2, "history has both runs")

try:
    service.simulate_bump("sample_repo", "notalib", "1.0")
    check(False, "unknown library rejected")
except BlastError as e:
    check(e.code == "LIBRARY_NOT_FOUND", "unknown library gives LIBRARY_NOT_FOUND")

rep3 = service.simulate_bump("sample_repo", "pyyaml", "5.4")
print(f"     pyyaml 5.3 -> 5.4: {rep3['verdict']}, API {rep3['apiStatus']}, advisory: {rep3['advisory']}")
check(rep3["advisory"] is not None, "advisory shown for the vulnerable pinned version")

print("\nALL PASSED" if failures == 0 else f"\n{failures} CHECK(S) FAILED")
sys.exit(1 if failures else 0)
