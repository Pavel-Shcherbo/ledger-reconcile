"""Conservative reconciliation of two explicitly normalized payment exports."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

FIELDS = ("record_id", "reference", "currency", "amount_minor")


@dataclass(frozen=True)
class Record:
    record_id: str
    reference: str
    currency: str
    amount_minor: int


def load_export(path: Path) -> tuple[list[Record], dict]:
    """Hash and parse the same bytes; exact repeated records count only once."""
    raw = path.read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    if reader.fieldnames != list(FIELDS):
        raise ValueError(f"{path.name}: expected CSV header {','.join(FIELDS)}")
    unique: dict[str, Record] = {}
    duplicates = 0
    for line, row in enumerate(reader, start=2):
        if set(row) != set(FIELDS) or any(row[k] is None for k in FIELDS):
            raise ValueError(f"{path.name}:{line}: malformed CSV row")
        if any(not row[k] or row[k] != row[k].strip() for k in FIELDS):
            raise ValueError(f"{path.name}:{line}: empty or padded value")
        if not re.fullmatch(r"[A-Z]{3}", row["currency"]):
            raise ValueError(
                f"{path.name}:{line}: currency must be three uppercase letters"
            )
        if not re.fullmatch(r"-?(0|[1-9][0-9]*)", row["amount_minor"]):
            raise ValueError(f"{path.name}:{line}: amount_minor must be an integer")
        record = Record(
            row["record_id"],
            row["reference"],
            row["currency"],
            int(row["amount_minor"]),
        )
        previous = unique.get(record.record_id)
        if previous is not None:
            if previous != record:
                raise ValueError(
                    f"{path.name}:{line}: conflicting record_id {record.record_id!r}"
                )
            duplicates += 1
        else:
            unique[record.record_id] = record
    return list(unique.values()), {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "unique_records": len(unique),
        "exact_duplicates_ignored": duplicates,
    }


def group_status(left: list[int], right: list[int]) -> str:
    """Classify a nonempty group without guessing split-settlement matches."""
    if not left and not right:
        raise ValueError("A reconciliation group must contain records")
    if len(left) > 1 or len(right) > 1:
        return "ambiguous"
    if not left:
        return "right_only"
    if not right:
        return "left_only"
    return "matched" if left[0] == right[0] else "amount_mismatch"


def reconcile(left: list[Record], right: list[Record]) -> dict:
    groups = [defaultdict(list), defaultdict(list)]
    totals = [defaultdict(int), defaultdict(int)]
    for records, group, total in zip((left, right), groups, totals):
        for record in records:
            group[(record.reference, record.currency)].append(record)
            total[record.currency] += record.amount_minor
    items = []
    counts: dict[str, int] = defaultdict(int)
    for reference, currency in sorted(groups[0].keys() | groups[1].keys()):
        a = sorted(groups[0][reference, currency], key=lambda x: x.record_id)
        b = sorted(groups[1][reference, currency], key=lambda x: x.record_id)
        status = group_status(
            [record.amount_minor for record in a],
            [record.amount_minor for record in b],
        )
        counts[status] += 1
        items.append(
            {
                "reference": reference,
                "currency": currency,
                "status": status,
                "left": [asdict(x) for x in a],
                "right": [asdict(x) for x in b],
                "delta_minor": sum(x.amount_minor for x in a)
                - sum(x.amount_minor for x in b),
            }
        )
    return {
        "schema_version": 1,
        "policy": (
            "exact reference + currency; unique one-to-one; signed integer minor units"
        ),
        "counts": dict(sorted(counts.items())),
        "has_exceptions": any(x["status"] != "matched" for x in items),
        "totals_by_currency": {
            c: {
                "left_minor": totals[0][c],
                "right_minor": totals[1][c],
                "delta_minor": totals[0][c] - totals[1][c],
            }
            for c in sorted(totals[0].keys() | totals[1].keys())
        },
        "items": items,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    inputs = (args.left.resolve(), args.right.resolve())
    if args.output.resolve() in inputs:
        parser.error("output must not overwrite an input export")
    try:
        left, left_proof = load_export(args.left)
        right, right_proof = load_export(args.right)
        result = reconcile(left, right)
        result["inputs"] = {"left": left_proof, "right": right_proof}
        # Exclusive creation prevents an accidental overwrite of an existing report.
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    except (ValueError, UnicodeError, OSError, csv.Error) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 1 if result["has_exceptions"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
