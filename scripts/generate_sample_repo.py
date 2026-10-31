"""Create the sample repo with planted problems and real git commits.

Usage:  python scripts/generate_sample_repo.py [out_dir]     (default: ./sample_repo)

Commits on main:
  1. Baseline (fakehttp==1.0)
  2. Adds an import cycle: services/orders.py <-> services/inventory.py
Branch upgrade-fakehttp (one commit on top): requirements change to fakehttp==2.0.
HEAD is left on main, so ingesting it gives fakehttp 1.0 for the bump demo.

Expected result of "bump fakehttp to 2.0" on main (verdict DO NOT BUMP YET):
  LIKELY BREAKS  shop/services/orders.py     post() call misses headers
  LIKELY BREAKS  shop/services/sync.py       uses fetch_all, which 2.0 removes
  LIKELY BREAKS  shop/clients/catalog.py     get() called with keyword timeout
  CHECK THIS     shop/clients/proxy.py       post(*args), cannot count arguments
  LIKELY SAFE    shop/services/payments.py   post() already passes headers
  LIKELY SAFE    shop/utils/retrying.py      retry() new parameter has a default
  LIKELY SAFE    shop/health.py              head() unchanged
  CHECK THIS     shop/api/routes.py, shop/services/inventory.py,
                 shop/jobs/nightly.py, shop/main.py   (reached through the above)
"""
import shutil
import subprocess
import sys
from pathlib import Path

BASELINE = {
    "requirements.txt": """\
fakehttp==1.0
pyyaml==5.3
networkx>=2.0,<4.0
brokenlib=>2
""",
    "README.md": "# shop\nA small sample shop backend used to demo Blast Radius.\n",
    "shop/__init__.py": "",
    "shop/main.py": """\
from shop.api import routes
from shop.jobs import nightly
from shop import config


def start():
    settings = config.load("settings.yaml")
    return routes, nightly, settings
""",
    "shop/config.py": """\
import yaml


def load(path):
    with open(path) as f:
        return yaml.safe_load(f)
""",
    "shop/health.py": """\
from fakehttp import head


def ping():
    return head("https://api.example.com/health")
""",
    "shop/legacy.py": """\
def old_report(:
    pass
""",
    "shop/api/__init__.py": "",
    "shop/api/routes.py": """\
from shop.services import orders, payments
from shop.clients.catalog import list_products
from shop import health


def checkout(item, qty, amount):
    payments.charge(amount)
    return orders.place_order(item, qty)


def products():
    return list_products()


def status():
    return health.ping()
""",
    "shop/services/__init__.py": "",
    "shop/services/orders.py": """\
\"\"\"Order handling.\"\"\"
from fakehttp import post

ORDERS_URL = "https://api.example.com/orders"


def place_order(item, qty):
    data = {"item": item, "qty": qty}
    return post(ORDERS_URL, data)
""",
    "shop/services/payments.py": """\
from fakehttp import post


def charge(amount):
    headers = {"Idempotency-Key": "demo"}
    return post("https://api.example.com/pay", {"amount": amount}, headers)
""",
    "shop/services/sync.py": """\
from fakehttp import fetch_all


def sync_all(urls):
    return fetch_all(urls)
""",
    "shop/clients/__init__.py": "",
    "shop/clients/catalog.py": """\
import fakehttp


def list_products():
    return fakehttp.get("https://api.example.com/products", timeout=5)
""",
    "shop/clients/proxy.py": """\
from fakehttp import post


def forward(*args):
    return post(*args)
""",
    "shop/utils/__init__.py": "",
    "shop/utils/retrying.py": """\
from fakehttp import retry


def fetch_with_retry(fn):
    return retry(fn, 3)
""",
    "shop/jobs/__init__.py": "",
    "shop/jobs/nightly.py": """\
from shop.services.sync import sync_all


def run():
    return sync_all(["https://api.example.com/a", "https://api.example.com/b"])
""",
    "tests/test_orders.py": """\
from shop.services.orders import place_order


def test_place_order():
    assert place_order("pen", 2)
""",
    "tests/test_payments.py": """\
from shop.services.payments import charge


def test_charge():
    assert charge(10)
""",
    "tests/test_catalog.py": """\
from shop.clients.catalog import list_products


def test_list_products():
    assert list_products()
""",
    "tests/test_retrying.py": """\
from shop.utils.retrying import fetch_with_retry


def test_retry():
    assert fetch_with_retry(lambda: 1) == 1
""",
}

CYCLE = {
    "shop/services/orders.py": """\
\"\"\"Order handling.\"\"\"
from fakehttp import post
from shop.services import inventory

ORDERS_URL = "https://api.example.com/orders"


def place_order(item, qty):
    inventory.reserve(item, qty)
    data = {"item": item, "qty": qty}
    return post(ORDERS_URL, data)
""",
    "shop/services/inventory.py": """\
from shop.services import orders


def reserve(item, qty):
    return {"item": item, "qty": qty}


def reorder(item):
    return orders.place_order(item, 10)
""",
}


def write(root: Path, files: dict):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def git(root: Path, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_repo").resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    git(out, "init", "-q")
    git(out, "symbolic-ref", "HEAD", "refs/heads/main")
    git(out, "config", "user.name", "Blast Demo")
    git(out, "config", "user.email", "demo@example.com")
    git(out, "config", "commit.gpgsign", "false")

    write(out, BASELINE)
    git(out, "add", "-A")
    git(out, "commit", "-q", "-m", "Baseline shop app on fakehttp 1.0")

    write(out, CYCLE)
    git(out, "add", "-A")
    git(out, "commit", "-q", "-m", "Reserve stock when placing orders (adds an import cycle)")

    git(out, "checkout", "-q", "-b", "upgrade-fakehttp")
    req = (out / "requirements.txt").read_text().replace("fakehttp==1.0", "fakehttp==2.0")
    (out / "requirements.txt").write_text(req)
    git(out, "add", "-A")
    git(out, "commit", "-q", "-m", "Upgrade fakehttp to 2.0")
    git(out, "checkout", "-q", "main")

    print(f"Sample repo created at {out} (HEAD on main, fakehttp 1.0)")


if __name__ == "__main__":
    main()
