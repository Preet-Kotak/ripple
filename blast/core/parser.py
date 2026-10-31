"""Read a Python repo: files, who imports whom, which libraries are used and how.

Nothing here touches the database, so it can be tested on its own:
    parse_repo("sample_repo")
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
from pathlib import Path, PurePosixPath

from packaging.requirements import InvalidRequirement, Requirement

SKIP_DIRS = {".git", "venv", ".venv", "env", "__pycache__", "node_modules", "site-packages",
             "build", "dist", ".tox", ".mypy_cache", ".pytest_cache"}

# Import name -> library name, for libraries whose names differ. Extend when needed.
IMPORT_TO_DIST = {
    "yaml": "pyyaml", "cv2": "opencv-python", "pil": "pillow", "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn", "dateutil": "python-dateutil", "dotenv": "python-dotenv",
    "jwt": "pyjwt", "attr": "attrs", "serial": "pyserial", "docx": "python-docx",
    "pptx": "python-pptx", "fitz": "pymupdf", "magic": "python-magic",
}


def normalize(name: str) -> str:
    """Library names: lowercase, and -, _ and . count as the same."""
    return re.sub(r"[-_.]+", "-", name).lower()


def import_names_for(dist: str) -> list[str]:
    """Library name -> the names it can be imported as (used by apidiff)."""
    names = [k for k, v in IMPORT_TO_DIST.items() if v == dist]
    names += [dist.replace("-", "_"), dist.replace("-", "")]
    return list(dict.fromkeys(names))


def is_test(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1]
    return (any(p in ("tests", "test") for p in parts[:-1]) or name.startswith("test_")
            or name.endswith("_test.py") or name == "conftest.py")


# ---------------------------------------------------------------- requirements
def parse_requirements(root: Path) -> list[dict]:
    req_file = root / "requirements.txt"
    found: dict[str, dict] = {}
    if not req_file.is_file():
        return []
    for n, raw in enumerate(req_file.read_text(errors="replace").splitlines(), 1):
        line = raw.split(" #", 1)[0].split("\t#", 1)[0].strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement:
            name = f"unreadable-line-{n}"
            found[name] = {"name": name, "pinnedVersion": None, "rule": None,
                           "declaredIn": "requirements.txt",
                           "parseError": f"Line {n} of requirements.txt could not be read: {raw.strip()!r}"}
            continue
        specs = list(req.specifier)
        pinned = None
        if len(specs) == 1 and specs[0].operator in ("==", "===") and "*" not in specs[0].version:
            pinned = specs[0].version
        name = normalize(req.name)
        found[name] = {"name": name, "pinnedVersion": pinned, "rule": str(req.specifier) or None,
                       "declaredIn": "requirements.txt", "parseError": None}
    return list(found.values())


# --------------------------------------------------------------------- imports
def module_name(rel: PurePosixPath) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def resolve_from_base(module, level, current_module, is_package):
    """Turn `from ..x import y` into an absolute module name."""
    if level == 0:
        return module
    pkg = current_module.split(".") if current_module else []
    if not is_package:
        pkg = pkg[:-1]
    if level - 1 > len(pkg):
        return None
    base = pkg[:len(pkg) - (level - 1)]
    if module:
        base += module.split(".")
    return ".".join(base)


def scan_file(tree, current_module, is_package, index, lib_of):
    internal: dict[str, dict] = {}
    external: dict[str, dict] = {}
    from_locals: dict[str, tuple[str, str]] = {}   # local name -> (library, original name)
    module_locals: dict[str, str] = {}             # local name -> library (for `import lib`)

    def add_internal(target, line, names):
        e = internal.setdefault(target, {"line": line, "names": set()})
        e["line"] = min(e["line"], line)
        e["names"].update(names)

    def ext(lib, line):
        e = external.setdefault(lib, {"line": line, "names": set(), "nameLines": {}, "calls": []})
        e["line"] = min(e["line"], line)
        return e

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in index:
                    add_internal(index[alias.name], node.lineno, [])
                    continue
                lib = lib_of(alias.name)
                if lib:
                    ext(lib, node.lineno)
                    if alias.asname or "." not in alias.name:
                        module_locals[alias.asname or alias.name] = lib
        elif isinstance(node, ast.ImportFrom):
            base = resolve_from_base(node.module, node.level, current_module, is_package)
            if base is None:
                continue
            subs = {a.name: (f"{base}.{a.name}" if base else a.name) for a in node.names}
            if node.level or base in index or any(s in index for s in subs.values()):
                for alias in node.names:
                    sub = subs[alias.name]
                    if alias.name != "*" and sub in index:
                        add_internal(index[sub], node.lineno, [])
                    elif base in index:
                        add_internal(index[base], node.lineno, [alias.name])
                continue
            lib = lib_of(base)
            if lib:
                e = ext(lib, node.lineno)
                for alias in node.names:
                    e["names"].add(alias.name)
                    e["nameLines"].setdefault(alias.name, node.lineno)
                    if alias.name != "*":
                        from_locals[alias.asname or alias.name] = (lib, alias.name)

    # Second pass: how the library names are used (attributes and calls).
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in module_locals):
            e = external[module_locals[node.value.id]]
            e["names"].add(node.attr)
            e["nameLines"].setdefault(node.attr, node.lineno)
        if isinstance(node, ast.Call):
            lib = name = None
            f = node.func
            if isinstance(f, ast.Name) and f.id in from_locals:
                lib, name = from_locals[f.id]
            elif (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                  and f.value.id in module_locals):
                lib, name = module_locals[f.value.id], f.attr
            if name:
                external[lib]["calls"].append({
                    "name": name,
                    "line": node.lineno,
                    "positionalCount": sum(1 for a in node.args if not isinstance(a, ast.Starred)),
                    "keywordNames": [k.arg for k in node.keywords if k.arg],
                    "hasStarArgs": (any(isinstance(a, ast.Starred) for a in node.args)
                                    or any(k.arg is None for k in node.keywords)),
                })
    return internal, external


# ------------------------------------------------------------------- the repo
def parse_repo(path) -> dict:
    root = Path(path)
    packages = parse_requirements(root)
    req_names = {p["name"] for p in packages if not p["parseError"]}

    def lib_of(dotted: str):
        top = dotted.split(".")[0]
        for cand in (normalize(top), IMPORT_TO_DIST.get(top.lower())):
            if cand and cand in req_names:
                return cand
        return None

    py_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        py_files += [Path(dirpath) / f for f in sorted(filenames) if f.endswith(".py")]

    rel = {p: PurePosixPath(p.relative_to(root).as_posix()) for p in py_files}
    index: dict[str, str] = {}
    for r in rel.values():
        name = module_name(r)
        if name:
            index.setdefault(name, str(r))
        if r.parts[0] == "src" and len(r.parts) > 1:  # src/ layout
            index[module_name(PurePosixPath(*r.parts[1:]))] = str(r)

    files, internal_edges, external_edges = [], [], []
    for p, r in rel.items():
        source = p.read_bytes()
        node = {"path": str(r), "kind": "test" if is_test(str(r)) else "source",
                "contentHash": hashlib.sha256(source).hexdigest(), "parseError": None}
        files.append(node)
        try:
            tree = ast.parse(source, filename=str(r))
        except (SyntaxError, ValueError) as e:
            node["parseError"] = f"line {getattr(e, 'lineno', '?')}: {getattr(e, 'msg', e)}"
            continue
        under_src = r.parts[0] == "src" and len(r.parts) > 1
        current = module_name(PurePosixPath(*r.parts[1:])) if under_src else module_name(r)
        internal, external = scan_file(tree, current, r.name == "__init__.py", index, lib_of)
        for target, e in internal.items():
            if target != str(r):
                internal_edges.append({"from": str(r), "to": target, "kind": "internal",
                                       "names": sorted(e["names"]), "line": e["line"]})
        for lib, e in external.items():
            external_edges.append({"from": str(r), "to": lib, "kind": "external",
                                   "names": sorted(e["names"]), "nameLines": e["nameLines"],
                                   "line": e["line"], "calls": e["calls"]})

    kind = {f["path"]: f["kind"] for f in files}
    fan_in: dict[str, int] = {}
    fan_out: dict[str, int] = {}
    tested = set()
    for e in internal_edges:
        fan_in[e["to"]] = fan_in.get(e["to"], 0) + 1
        fan_out[e["from"]] = fan_out.get(e["from"], 0) + 1
        if kind.get(e["from"]) == "test":
            tested.add(e["to"])
    for f in files:
        f["fanIn"] = fan_in.get(f["path"], 0)
        f["fanOut"] = fan_out.get(f["path"], 0)
        f["hasTest"] = f["path"] in tested

    return {"files": files, "internalEdges": internal_edges,
            "externalEdges": external_edges, "packages": packages}
