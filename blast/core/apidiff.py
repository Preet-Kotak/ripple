"""Compare the public API of two versions of a library.

Source comes from fakelibs/<library>/<version>/ when present (demo, no internet),
otherwise from PyPI. Only .py files are read with `ast`; nothing is imported or run.
Results are cached in the api_changes collection.
"""
from __future__ import annotations

import ast
import difflib
import io
import json
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .parser import import_names_for

ROOT = Path(__file__).resolve().parents[2]
FAKELIBS = ROOT / "fakelibs"
CACHE = Path(tempfile.gettempdir()) / "blast_cache"
MAX_DOWNLOAD = 50 * 1024 * 1024


class ApiUnavailable(Exception):
    pass


# ------------------------------------------------------------ getting source
def _safe_target(base: Path, name: str):
    target = (base / name).resolve()
    return target if str(target).startswith(str(base.resolve())) else None


def _extract_py(data: bytes, filename: str, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    if filename.endswith((".whl", ".zip")):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if info.filename.endswith(".py") and (t := _safe_target(dest, info.filename)):
                    t.parent.mkdir(parents=True, exist_ok=True)
                    t.write_bytes(z.read(info))
    elif filename.endswith((".tar.gz", ".tgz", ".tar.bz2")):
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            for m in tf.getmembers():
                if m.isfile() and m.name.endswith(".py") and (t := _safe_target(dest, m.name)):
                    t.parent.mkdir(parents=True, exist_ok=True)
                    t.write_bytes(tf.extractfile(m).read())
    else:
        raise ApiUnavailable(f"unsupported file type {filename}")


def _download(library: str, version: str) -> Path:
    dest = CACHE / f"{library}-{version}"
    if dest.is_dir() and any(dest.iterdir()):
        return dest
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{library}/{version}/json", timeout=20) as r:
            meta = json.load(r)
    except Exception as e:
        raise ApiUnavailable(f"could not fetch {library} {version} from PyPI ({type(e).__name__})")

    def rank(f):
        name = f["filename"]
        if f["packagetype"] == "bdist_wheel" and name.endswith("-none-any.whl"):
            return 0
        if f["packagetype"] == "sdist":
            return 1
        if f["packagetype"] == "bdist_wheel":
            return 2
        return 9

    files = sorted((f for f in meta.get("urls", []) if rank(f) < 9), key=rank)
    if not files:
        raise ApiUnavailable(f"no downloadable files for {library} {version}")
    chosen = files[0]
    if chosen.get("size", 0) > MAX_DOWNLOAD:
        raise ApiUnavailable(f"{chosen['filename']} is too large to analyse")
    try:
        with urllib.request.urlopen(chosen["url"], timeout=60) as r:
            data = r.read(MAX_DOWNLOAD + 1)
    except Exception as e:
        raise ApiUnavailable(f"download of {chosen['filename']} failed ({type(e).__name__})")

    tmp = Path(tempfile.mkdtemp(prefix="blast_dl_"))
    try:
        _extract_py(data, chosen["filename"], tmp)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), str(dest))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


def locate_package(library: str, version: str) -> Path:
    local = FAKELIBS / library / version
    base = local if local.is_dir() else _download(library, version)
    names = import_names_for(library)
    cands = [p.parent for p in base.rglob("__init__.py") if p.parent.name in names]
    cands += [p for n in names for p in base.rglob(f"{n}.py")]
    if not cands:
        raise ApiUnavailable(f"no Python package named {names[0]} inside {library} {version}")
    cands.sort(key=lambda p: (len(p.relative_to(base).parts), -len(str(p))))
    return cands[0]


# --------------------------------------------------------- reading the API
def _public(name: str) -> bool:
    return not name.startswith("_")


def _unparse(node):
    return ast.unparse(node) if node is not None else None


def params_of(args: ast.arguments, method: bool = False) -> list[dict]:
    positional = list(args.posonlyargs) + list(args.args)
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    params = []
    for i, (a, d) in enumerate(zip(positional, defaults)):
        params.append({"name": a.arg, "kind": "posonly" if i < len(args.posonlyargs) else "pos",
                       "hasDefault": d is not None, "default": _unparse(d)})
    if args.vararg:
        params.append({"name": args.vararg.arg, "kind": "varargs", "hasDefault": True, "default": None})
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        params.append({"name": a.arg, "kind": "kwonly", "hasDefault": d is not None, "default": _unparse(d)})
    if args.kwarg:
        params.append({"name": args.kwarg.arg, "kind": "varkw", "hasDefault": True, "default": None})
    if method and params and params[0]["kind"] in ("posonly", "pos") and params[0]["name"] in ("self", "cls"):
        params = params[1:]
    return params


def sig_text(name: str, params) -> str:
    if params is None:
        return name
    out, star_done = [], False
    for p in params:
        if p["kind"] == "varargs":
            out.append(f"*{p['name']}")
            star_done = True
        elif p["kind"] == "varkw":
            out.append(f"**{p['name']}")
        else:
            if p["kind"] == "kwonly" and not star_done:
                out.append("*")
                star_done = True
            out.append(f"{p['name']}={p['default']}" if p["hasDefault"] else p["name"])
    return f"{name}({', '.join(out)})"


def _resolve_inner(node: ast.ImportFrom, file: Path, pkg_dir: Path, pkg_name: str):
    if node.level == 0:
        if not node.module or node.module.split(".")[0] != pkg_name:
            return None
        base, parts = pkg_dir, node.module.split(".")[1:]
    else:
        base = file.parent
        for _ in range(node.level - 1):
            base = base.parent
        parts = node.module.split(".") if node.module else []
    if not parts:
        return base / "__init__.py" if (base / "__init__.py").is_file() else None
    cand = base.joinpath(*parts)
    if cand.with_suffix(".py").is_file():
        return cand.with_suffix(".py")
    if (cand / "__init__.py").is_file():
        return cand / "__init__.py"
    return None


def module_api(file: Path, pkg_dir: Path, pkg_name: str, seen=frozenset(), depth=0) -> dict:
    api: dict = {}
    if file in seen or depth > 4 or not file.is_file():
        return api
    seen = seen | {file}
    try:
        tree = ast.parse(file.read_text(errors="replace"))
    except (SyntaxError, ValueError):
        return api
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _public(node.name):
            api[node.name] = params_of(node.args)
        elif isinstance(node, ast.ClassDef) and _public(node.name):
            funcs = [b for b in node.body if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))]
            init = next((b for b in funcs if b.name == "__init__"), None)
            api[node.name] = params_of(init.args, method=True) if init else None
            for b in funcs:
                if _public(b.name):
                    api[f"{node.name}.{b.name}"] = params_of(b.args, method=True)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and _public(t.id):
                    api.setdefault(t.id, None)
        elif isinstance(node, ast.ImportFrom):
            target = _resolve_inner(node, file, pkg_dir, pkg_name)
            if target is None:
                continue
            sub = module_api(target, pkg_dir, pkg_name, seen, depth + 1)
            for alias in node.names:
                if alias.name == "*":
                    api.update(sub)
                elif alias.name in sub and _public(alias.asname or alias.name):
                    local = alias.asname or alias.name
                    api[local] = sub[alias.name]
                    for k, v in sub.items():
                        if k.startswith(alias.name + "."):
                            api[local + k[len(alias.name):]] = v
    return api


def extract_api(path: Path) -> dict:
    if path.is_file():
        return module_api(path, path.parent, path.stem)
    return module_api(path / "__init__.py", path, path.name)


# ------------------------------------------------------------- comparing
def compare_params(old: list, new: list):
    def named(ps):
        return {p["name"]: p for p in ps if p["kind"] not in ("varargs", "varkw")}

    on, wn = named(old), named(new)
    new_varkw = any(p["kind"] == "varkw" for p in new)
    reasons = []
    for name, p in on.items():
        if name not in wn and not (new_varkw and p["kind"] != "posonly"):
            reasons.append(f"parameter {name} was removed or renamed")
        elif name in wn and p["hasDefault"] and not wn[name]["hasDefault"]:
            reasons.append(f"parameter {name} is now required")
    for name, p in wn.items():
        if name not in on and not p["hasDefault"]:
            reasons.append(f"new required parameter {name}")
    optional_new = [n for n, p in wn.items() if n not in on and p["hasDefault"]]
    return reasons, optional_new


def diff_api(old: dict, new: dict) -> dict:
    removed = sorted(n for n in old if n not in new)
    new_names = sorted(n for n in new if n not in old)
    added = list(new_names)
    changed, new_params = [], []
    for n in sorted(set(old) & set(new)):
        o, w = old[n], new[n]
        if o is None or w is None or o == w:
            continue
        reasons, optional_new = compare_params(o, w)
        if reasons:
            changed.append({"name": n, "old": sig_text(n, o), "new": sig_text(n, w), "why": reasons})
            new_params.append({"name": n, "params": w})
        elif optional_new:
            added.append(f"{n}: new optional parameter {', '.join(optional_new)}")
    hints = []  # a list, not a dict: names like "Session.close" contain dots
    for r in removed:
        match = difflib.get_close_matches(r, new_names, n=1, cutoff=0.6)
        if match:
            hints.append({"name": r, "hint": match[0]})
    return {"removed": removed, "signatureChanged": changed, "added": added,
            "renameHints": hints, "newParams": new_params}


def compare(db, library: str, old_version: str, new_version: str) -> dict:
    key = f"{library}:{old_version}->{new_version}"
    cached = db.api_changes.find_one({"_id": key})
    if cached and cached.get("status") == "ok":
        return cached
    try:
        old_api = extract_api(locate_package(library, old_version))
        new_api = extract_api(locate_package(library, new_version))
        if not old_api and not new_api:
            raise ApiUnavailable("no public names could be read (compiled-only package?)")
    except ApiUnavailable as e:
        return {"_id": key, "status": "unavailable", "reason": str(e), "removed": [],
                "signatureChanged": [], "added": [], "renameHints": [], "newParams": []}
    result = {"_id": key, "library": library, "fromVersion": old_version, "toVersion": new_version,
              "status": "ok", "computedAt": datetime.now(timezone.utc), **diff_api(old_api, new_api)}
    db.api_changes.replace_one({"_id": key}, result, upsert=True)
    return result
