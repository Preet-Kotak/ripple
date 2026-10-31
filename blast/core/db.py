"""MongoDB connection and setup: collections, validators, indexes, seed data.

init_db() is safe to run again and again.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import CollectionInvalid, OperationFailure

from .errors import BlastError

try:  # local runs read .env; on Streamlit Cloud the app copies secrets into os.environ
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

EVENT_STATUSES = ["processing", "accepted", "rejected_duplicate", "rejected_invalid", "failed"]

# Required fields are checked by MongoDB itself. "payload" stays open on purpose.
VALIDATORS = {
    "ingest_events": {
        "bsonType": "object",
        "required": ["repo", "commitSHA", "receivedAt", "status"],
        "properties": {
            "repo": {"bsonType": "string"},
            "commitSHA": {"bsonType": "string"},
            "receivedAt": {"bsonType": "date"},
            "status": {"enum": EVENT_STATUSES},
            "payload": {"bsonType": "object"},
        },
    },
    "snapshots": {
        "bsonType": "object",
        "required": ["repo", "commitSHA", "createdAt"],
        "properties": {
            "repo": {"bsonType": "string"},
            "commitSHA": {"bsonType": "string"},
            "createdAt": {"bsonType": "date"},
        },
    },
    "packages": {
        "bsonType": "object",
        "required": ["snapshotId", "name"],
        "properties": {"snapshotId": {"bsonType": "string"}, "name": {"bsonType": "string"}},
    },
    "bump_runs": {
        "bsonType": "object",
        "required": ["repo", "library", "createdAt", "outcome"],
        "properties": {
            "createdAt": {"bsonType": "date"},
            "outcome": {"enum": ["unknown", "ok", "broke"]},
        },
    },
}

# A small hand-written list of real advisories, enough for the demo.
ADVISORIES = [
    {"name": "pyyaml", "affectedRange": "<5.4", "fixedIn": "5.4",
     "note": "full_load can run arbitrary code (CVE-2020-14343)"},
    {"name": "requests", "affectedRange": "<2.31.0", "fixedIn": "2.31.0",
     "note": "Proxy-Authorization header can leak on redirect (CVE-2023-32681)"},
    {"name": "urllib3", "affectedRange": "<1.26.18", "fixedIn": "1.26.18",
     "note": "request body kept on 303 redirect (CVE-2023-45803)"},
    {"name": "jinja2", "affectedRange": "<3.1.3", "fixedIn": "3.1.3",
     "note": "xmlattr filter allows attribute injection (CVE-2024-22195)"},
    {"name": "pillow", "affectedRange": "<10.0.1", "fixedIn": "10.0.1",
     "note": "bundled libwebp heap overflow (CVE-2023-4863)"},
    {"name": "flask", "affectedRange": "<2.2.5", "fixedIn": "2.2.5",
     "note": "session cookie can be cached by proxies (CVE-2023-30861)"},
]


@lru_cache(maxsize=1)
def get_client() -> MongoClient:
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        raise BlastError("NO_DATABASE", "MONGODB_URI is not set. Put it in .env locally, "
                                        "or in the app's Secrets on Streamlit Cloud.")
    return MongoClient(uri, serverSelectionTimeoutMS=8000, appname="blast-radius")


def get_db():
    return get_client()[os.environ.get("MONGODB_DB", "blast")]


def init_db(db=None):
    db = db if db is not None else get_db()
    existing = set(db.list_collection_names())

    for name, schema in VALIDATORS.items():
        if name not in existing:
            try:
                db.create_collection(name, validator={"$jsonSchema": schema})
            except CollectionInvalid:
                pass  # created by another process a moment ago
        else:
            try:
                db.command("collMod", name, validator={"$jsonSchema": schema})
            except OperationFailure:
                pass  # some hosted tiers restrict collMod; the original validator stays

    # Duplicate guard: only one event per commit may hold the "claim" field.
    db.ingest_events.create_index(
        [("claim", ASCENDING)], unique=True,
        partialFilterExpression={"claim": {"$exists": True}}, name="one_ingest_per_commit")
    db.ingest_events.create_index([("repo", ASCENDING), ("receivedAt", DESCENDING)])

    db.snapshots.create_index([("repo", ASCENDING), ("commitSHA", ASCENDING)], unique=True)
    db.snapshots.create_index([("repo", ASCENDING), ("createdAt", DESCENDING)])

    db.nodes.create_index([("snapshotId", ASCENDING), ("path", ASCENDING)], unique=True)

    db.edges.create_index([("snapshotId", ASCENDING), ("from", ASCENDING), ("to", ASCENDING)], unique=True)
    db.edges.create_index([("snapshotId", ASCENDING), ("to", ASCENDING)])  # walked by $graphLookup
    db.edges.create_index([("snapshotId", ASCENDING), ("kind", ASCENDING), ("to", ASCENDING)])

    db.packages.create_index([("snapshotId", ASCENDING), ("name", ASCENDING)], unique=True)
    db.advisories.create_index([("name", ASCENDING)])
    db.bump_runs.create_index([("repo", ASCENDING), ("library", ASCENDING), ("createdAt", DESCENDING)])

    for adv in ADVISORIES:
        key = f"{adv['name']}{adv['affectedRange']}"
        db.advisories.replace_one({"_id": key}, {"_id": key, **adv}, upsert=True)
    return db
