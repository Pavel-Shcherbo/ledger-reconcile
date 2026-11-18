"""Local SQLite import batches and explicit revisions for synthetic provider events."""

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from adapter import normalize
from reconcile import FIELDS

APPLICATION_ID = 0x4C524543


def check_store(connection, allow_empty=False):
    app_id = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if app_id == APPLICATION_ID and version == 1:
        return
    objects = connection.execute("SELECT name FROM sqlite_master").fetchall()
    if allow_empty and app_id == 0 and version == 0 and not objects:
        return
    raise ValueError("Not a supported Ledger Reconcile database")


SCHEMA = """
CREATE TABLE IF NOT EXISTS streams(name TEXT PRIMARY KEY, basis TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batches(
 id INTEGER PRIMARY KEY, stream TEXT NOT NULL, source_hash TEXT NOT NULL,
 correction_request TEXT NOT NULL, summary TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(stream, source_hash));
CREATE TABLE IF NOT EXISTS revisions(
 stream TEXT NOT NULL, event_id TEXT NOT NULL, revision INTEGER NOT NULL,
 batch_id INTEGER NOT NULL REFERENCES batches(id), payload TEXT NOT NULL,
 PRIMARY KEY(stream,event_id,revision));
"""


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def import_batch(db, source, stream, basis, corrections=None):
    """Corrections maps event ID to expected current revision; changed rows only."""
    if not stream or stream != stream.strip():
        raise ValueError("Stream name must be nonempty and unpadded")
    corrections = corrections or {}
    if any(type(v) is not int or v < 1 for v in corrections.values()):
        raise ValueError("Correction revisions must be positive integers")
    _, proof = normalize(source, basis)
    events = proof["events"]
    if set(corrections) - {x["event_id"] for x in events}:
        raise ValueError("Correction requested for an event absent from this batch")
    with closing(sqlite3.connect(db)) as connection:
        check_store(connection, allow_empty=True)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA)
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute("PRAGMA user_version = 1")
        # Serialize validation and writes so expected revision is checked atomically.
        connection.execute("BEGIN IMMEDIATE")
        try:
            previous_basis = connection.execute(
                "SELECT basis FROM streams WHERE name=?", (stream,)
            ).fetchone()
            if previous_basis and previous_basis[0] != basis:
                raise ValueError("A stream cannot change its gross/net basis")
            previous_batch = connection.execute(
                "SELECT id,correction_request,summary FROM batches "
                "WHERE stream=? AND source_hash=?",
                (stream, proof["source_sha256"]),
            ).fetchone()
            if previous_batch:
                if previous_batch[1] != encode(corrections):
                    raise ValueError(
                        "Previously imported bytes used different corrections"
                    )
                connection.rollback()
                return {**json.loads(previous_batch[2]), "replayed_batch": True}
            connection.execute(
                "INSERT OR IGNORE INTO streams VALUES(?,?)", (stream, basis)
            )
            cursor = connection.execute(
                "INSERT INTO batches(stream,source_hash,correction_request,summary) "
                "VALUES(?,?,?,?)",
                (stream, proof["source_sha256"], encode(corrections), "{}"),
            )
            batch_id = cursor.lastrowid
            counts = {"new": 0, "unchanged": 0, "corrected": 0}
            for event in events:
                event_id = event["event_id"]
                payload = encode(event)
                old = connection.execute(
                    "SELECT revision,payload FROM revisions "
                    "WHERE stream=? AND event_id=? ORDER BY revision DESC LIMIT 1",
                    (stream, event_id),
                ).fetchone()
                if old is None:
                    if event_id in corrections:
                        raise ValueError(f"Cannot correct unknown event {event_id}")
                    revision = 1
                    counts["new"] += 1
                elif old[1] == payload:
                    if event_id in corrections:
                        raise ValueError(
                            f"Correction has no changed content: {event_id}"
                        )
                    counts["unchanged"] += 1
                    continue
                else:
                    if corrections.get(event_id) != old[0]:
                        raise ValueError(
                            f"Conflict or stale expected revision: {event_id}"
                        )
                    old_event = json.loads(old[1])
                    identity = ("reference", "currency", "kind", "parent_reference")
                    if any(old_event[k] != event[k] for k in identity):
                        raise ValueError(
                            f"Correction cannot change event identity: {event_id}"
                        )
                    revision = old[0] + 1
                    counts["corrected"] += 1
                connection.execute(
                    "INSERT INTO revisions VALUES(?,?,?,?,?)",
                    (stream, event_id, revision, batch_id, payload),
                )
            summary = {
                "batch_id": batch_id,
                "stream": stream,
                "basis": basis,
                "source_sha256": proof["source_sha256"],
                **counts,
                "exact_duplicates_ignored": proof["exact_duplicates_ignored"],
                "replayed_batch": False,
            }
            connection.execute(
                "UPDATE batches SET summary=? WHERE id=?", (encode(summary), batch_id)
            )
            connection.commit()
            return summary
        except Exception:
            connection.rollback()
            raise


def current_events(db, stream, *, as_of_batch=None):
    # Read-only mode must not create a database when the user mistypes a path.
    uri = Path(db).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        check_store(connection)
        basis = connection.execute(
            "SELECT basis FROM streams WHERE name=?", (stream,)
        ).fetchone()
        if basis is None:
            raise ValueError("Unknown stream")
        if as_of_batch is not None:
            batch = connection.execute(
                "SELECT 1 FROM batches WHERE stream=? AND id=?", (stream, as_of_batch)
            ).fetchone()
            if batch is None:
                raise ValueError("Unknown batch in stream")
        events = connection.execute(
            "SELECT r.payload FROM revisions r WHERE r.stream=? AND r.revision="
            "(SELECT MAX(v.revision) FROM revisions v WHERE v.stream=r.stream "
            "AND v.event_id=r.event_id AND (? IS NULL OR v.batch_id<=?)) "
            "ORDER BY r.event_id",
            (stream, as_of_batch, as_of_batch),
        ).fetchall()
    return basis[0], [json.loads(row[0]) for row in events]


def current_records(db, stream, *, as_of_batch=None):
    basis, events = current_events(db, stream, as_of_batch=as_of_batch)
    return [
        {
            "record_id": event["event_id"],
            "reference": event["reference"],
            "currency": event["currency"],
            "amount_minor": event[basis + "_minor"],
        }
        for event in events
    ]


def check_refunds(db, stream, *, as_of_batch=None):
    """Check refund integrity against the selected snapshot of one stream."""
    _, events = current_events(db, stream, as_of_batch=as_of_batch)
    payments = {}
    for event in events:
        if event["kind"] == "payment":
            payments.setdefault(event["reference"], []).append(event)
    issues = []
    groups = {}
    refunds = [x for x in events if x["kind"] == "refund"]
    for refund in refunds:
        candidates = payments.get(refund["parent_reference"], [])
        matching = [p for p in candidates if p["currency"] == refund["currency"]]
        if len(matching) != 1:
            code = (
                "missing_parent"
                if not candidates
                else "currency_mismatch"
                if not matching
                else "ambiguous_parent"
            )
            issues.append(
                {
                    "code": code,
                    "refund_id": refund["event_id"],
                    "parent_reference": refund["parent_reference"],
                    "currency": refund["currency"],
                }
            )
            continue
        parent = matching[0]
        group = groups.setdefault(
            parent["event_id"],
            {
                "parent_id": parent["event_id"],
                "currency": parent["currency"],
                "payment_gross_minor": parent["gross_minor"],
                "refunded_gross_minor": 0,
                "refund_ids": [],
            },
        )
        group["refunded_gross_minor"] -= refund["gross_minor"]
        group["refund_ids"].append(refund["event_id"])
    for group in groups.values():
        if group["refunded_gross_minor"] > group["payment_gross_minor"]:
            issues.append({"code": "exceeds_payment", **group})
    result = {
        "stream": stream,
        "refund_count": len(refunds),
        "issues": issues,
        "has_issues": bool(issues),
        "scope": (
            "Latest stored events only; gross amounts exclude fees. "
            "Not a completeness guarantee."
        ),
    }
    if as_of_batch is not None:
        result["as_of_batch"] = as_of_batch
        result["scope"] = (
            "Latest stored revisions through the selected batch; "
            "gross amounts exclude fees. Not a completeness guarantee."
        )
    return result


def event_history(db, stream, event_id):
    """Return stored revisions and source hashes for one event."""
    uri = Path(db).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        check_store(connection)
        rows = connection.execute(
            "SELECT r.revision, r.batch_id, b.source_hash, r.payload "
            "FROM revisions r JOIN batches b ON r.batch_id=b.id "
            "WHERE r.stream=? AND r.event_id=? ORDER BY r.revision",
            (stream, event_id),
        ).fetchall()
    if not rows:
        raise ValueError("Unknown event in stream")
    return [
        {
            "revision": revision,
            "batch_id": batch_id,
            "source_sha256": source_hash,
            "event": json.loads(payload),
        }
        for revision, batch_id, source_hash, payload in rows
    ]


def import_history(db, stream):
    """Return committed import batches in order for a single stream."""
    uri = Path(db).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        check_store(connection)
        rows = connection.execute(
            "SELECT b.created_at, b.correction_request, b.summary "
            "FROM batches b WHERE b.stream=? ORDER BY b.id",
            (stream,),
        ).fetchall()
    if not rows:
        raise ValueError("Unknown stream")
    return [
        {
            **json.loads(summary),
            "created_at_utc": created_at.replace(" ", "T") + "Z",
            "expected_revisions": json.loads(correction_request),
        }
        for created_at, correction_request, summary in rows
    ]


def backup_store(db, output):
    """Create a checked, exclusive backup of all streams, including WAL data."""
    output = Path(output)
    uri = Path(db).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source:
        check_store(source)
        with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
            snapshot = Path(temporary) / "snapshot.sqlite"
            with closing(sqlite3.connect(snapshot)) as destination:
                source.backup(destination)
                check_store(destination)
                if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("Backup failed SQLite integrity check")
                if destination.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("Backup contains invalid batch references")
            with snapshot.open("rb") as stream:
                os.fsync(stream.fileno())
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            # A hard link publishes the completed file atomically and cannot
            # replace an existing path. Both paths are on the same filesystem.
            os.link(snapshot, output)
    return {"sha256": digest, "scope": "All streams and stored revisions"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("import")
    load.add_argument("source", type=Path)
    load.add_argument("--basis", choices=("gross", "net"), required=True)
    load.add_argument("--correct", action="append", default=[], metavar="ID:REVISION")
    export = sub.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--as-of-batch", type=int, help="Export through this batch ID")
    check = sub.add_parser("check-refunds")
    check.add_argument("--as-of-batch", type=int, help="Check through this batch ID")
    history = sub.add_parser("history")
    history.add_argument("event_id")
    batches = sub.add_parser("batches", help="List committed imports for a stream")
    for cmd in (load, export, check, history, batches):
        cmd.add_argument("--db", type=Path, required=True)
        cmd.add_argument("--stream", required=True)
    backup = sub.add_parser("backup", help="Back up all streams and revisions")
    backup.add_argument("--db", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            print(json.dumps(backup_store(args.db, args.output), indent=2))
        elif args.command == "import":
            corrections = {}
            for spec in args.correct:
                event_id, revision = spec.rsplit(":", 1)
                if event_id in corrections:
                    raise ValueError("Duplicate correction option")
                corrections[event_id] = int(revision)
            print(
                json.dumps(
                    import_batch(
                        args.db, args.source, args.stream, args.basis, corrections
                    ),
                    indent=2,
                )
            )
        elif args.command == "history":
            print(
                json.dumps(event_history(args.db, args.stream, args.event_id), indent=2)
            )
        elif args.command == "batches":
            print(json.dumps(import_history(args.db, args.stream), indent=2))
        elif args.command == "check-refunds":
            result = check_refunds(args.db, args.stream, as_of_batch=args.as_of_batch)
            print(json.dumps(result, indent=2))
            if result["has_issues"]:
                raise SystemExit(1)
        else:
            if args.output.resolve() == args.db.resolve():
                raise ValueError("Export cannot overwrite database")
            records = current_records(
                args.db, args.stream, as_of_batch=args.as_of_batch
            )
            with args.output.open("x", encoding="utf-8", newline="") as out:
                writer = csv.DictWriter(out, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(records)
    except (OSError, ValueError, sqlite3.Error, csv.Error) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
