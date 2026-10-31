# Blast Radius

A developer tool that answers one question: "If I bump this library to that version,
what in my code breaks, in what order do I fix it, and did this go wrong last time?"
Python repos only. Web page (Streamlit) on top of an analysis engine that stores
everything in MongoDB Atlas.

## Hackathon rules (hard constraints, never break these)

- Event: UNCHARTED 2026 (NEXUS x SVNIT Surat, with MongoDB), Track 3: Developer Tools.
- Build window ends **31 Oct 2026, 9:00 PM IST**. After that there is a code freeze:
  no commits, no pushes, no redeploys. Warn me if I ask for a change after 9 PM.
- A live deployed URL is compulsory. The app is on Streamlit Community Cloud, which
  redeploys on every push to `main`, so a broken push breaks the live demo.
- Never commit, print or log secrets: `.env`, `.streamlit/secrets.toml`, `MONGODB_URI`.
- Judges will ask us to explain the architecture and data model. Keep code plain and
  explainable. The engine deliberately uses no LLM: every warning traces to a line of code.

## Commands

- Setup (Windows): `setup.bat`. Mac/Linux: `bash setup.sh`. Needs `.env` with `MONGODB_URI`.
- Full end-to-end test: `python scripts/smoke_test.py` (uses database `blast_smoke`, wipes it first).
  Must end with `ALL PASSED`. Run it after every change to `blast/core/`.
- Run the web page: `streamlit run app.py`
- Make the sample repo: `python scripts/generate_sample_repo.py <out_dir>`

## Architecture

`app.py` (Streamlit page) calls only `blast/core/service.py`, which calls:

- `parser.py`: reads .py files with `ast` and `requirements.txt` with `packaging`.
  Finds internal imports, library imports, imported names and call sites. No DB access.
- `ingest.py`: stores one commit (nodes, edges, packages, snapshot). Duplicate commits are
  blocked by a unique partial index on `ingest_events.claim`. Rolls back on failure.
- `apidiff.py`: compares the public API of two library versions (removed names, breaking
  signature changes, added names). Source from `fakelibs/` if present, else PyPI. Cached in `api_changes`.
- `bump.py`: impact walk with `$graphLookup`, per-call signature check, labels
  (LIKELY BREAKS / CHECK THIS / LIKELY SAFE), verdict, fix order (networkx topological sort,
  cycles grouped), past-outcome warning.
- `history.py`: bump runs and their recorded real outcomes.
- `db.py`: connection, `$jsonSchema` validators, indexes, advisory seed data. `init_db()` is idempotent.
- `errors.py`: `BlastError(code, message)`; codes: INVALID_INPUT, DUPLICATE_COMMIT,
  LIBRARY_NOT_FOUND, NO_SNAPSHOT, RUN_NOT_FOUND, NO_DATABASE.

## MongoDB collections

ingest_events, snapshots, nodes, edges, packages, advisories, api_changes, bump_runs.
Snapshot id is `"{repo}@{commitSHA}"`; nodes/edges/packages carry `snapshotId`.
Edge `from -> to` means "from imports to"; `kind` is `internal` (a file) or `external` (a library).

## Conventions

- `blast/core/` never imports Streamlit. `app.py` holds no analysis logic.
- The return shapes of `service.py` are a contract documented at the top of `app.py`.
  If you change a shape, update that docstring and `DemoService` in `app.py` too.
- Raise `BlastError` with a code for expected errors; never let a bad input crash the page.
- Only use dependencies already in `requirements.txt`. Ask before adding one.
- Prefer small, focused changes over refactors. Explain what you changed in plain words.

## The sample repo is the test oracle

`scripts/generate_sample_repo.py` plants specific problems, and `scripts/smoke_test.py`
checks the exact expected labels (bump fakehttp 1.0 -> 2.0: verdict DO NOT BUMP YET;
orders.py, sync.py, catalog.py break; proxy.py check; payments.py, retrying.py, health.py safe).
If you change the sample repo or `fakelibs/`, update the expected answers in both files.

## Known limits (state them honestly, don't hide them)

Static analysis only: names and parameters, not behaviour. Only simple direct calls are
checked (not calls via variables, wrappers or getattr). Only root `requirements.txt` is read.
Compiled-only packages give "API comparison unavailable" and fall back to the version jump.

## Status and next tasks (in priority order)

1. Engine and web page are done; smoke test passes. Confirm it passes on this machine.
2. Confirm the live Streamlit URL works end to end with real data.
3. Write README.md for judges: pitch, architecture diagram, data model, commands,
   "Why MongoDB" (unique indexes, $graphLookup, aggregation, schema validation,
   history that changes the next answer), honest limits.
4. Optional, only if time allows: small `blast/cli.py` (typer + rich) calling `service.py`;
   reading `pyproject.toml` dependencies.
Stop adding features by 6 PM IST on 31 Oct; the rest of the time is for the demo video and PPT.
