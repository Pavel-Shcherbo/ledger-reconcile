"""Normalize the documented synthetic provider-v1 format; no real vendor support."""

import argparse
import csv
import hashlib
import io
import json
import re
from pathlib import Path

from reconcile import FIELDS

PROVIDER_FIELDS = (
    "event_id",
    "reference",
    "kind",
    "parent_reference",
    "currency",
    "gross_minor",
    "fee_minor",
    "net_minor",
)


def normalize(path, basis):
    if basis not in ("gross", "net"):
        raise ValueError("Choose an explicit gross or net comparison basis")
    raw = path.read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    if reader.fieldnames != list(PROVIDER_FIELDS):
        raise ValueError("Expected the documented synthetic provider-v1 header")
    events = {}
    duplicates = 0
    kinds = {}
    for line, row in enumerate(reader, 2):
        if set(row) != set(PROVIDER_FIELDS) or any(v is None for v in row.values()):
            raise ValueError(f"Line {line}: malformed row")
        for field, value in row.items():
            if value != value.strip() or (not value and field != "parent_reference"):
                raise ValueError(f"Line {line}: empty or padded {field}")
        if row["kind"] not in ("payment", "refund"):
            raise ValueError(f"Line {line}: unsupported event kind")
        if not re.fullmatch(r"[A-Z]{3}", row["currency"]):
            raise ValueError(f"Line {line}: invalid currency shape")
        amounts = {}
        for field in ("gross_minor", "fee_minor", "net_minor"):
            if not re.fullmatch(r"-?(0|[1-9][0-9]*)", row[field]):
                raise ValueError(f"Line {line}: {field} must be integer minor units")
            amounts[field] = int(row[field])
        gross, fee, net = (
            amounts[f] for f in ("gross_minor", "fee_minor", "net_minor")
        )
        if fee < 0 or gross - fee != net:
            raise ValueError(
                f"Line {line}: require nonnegative fee and net = gross - fee"
            )
        parent = row["parent_reference"]
        if row["kind"] == "payment":
            if gross <= 0 or parent:
                raise ValueError(
                    f"Line {line}: payment needs positive gross and no parent"
                )
        elif gross >= 0 or not parent or parent == row["reference"]:
            raise ValueError(
                f"Line {line}: refund needs negative gross and distinct parent"
            )
        previous = events.get(row["event_id"])
        if previous is not None:
            if previous != row:
                raise ValueError(f"Line {line}: conflicting event_id")
            duplicates += 1
            continue
        key = (row["reference"], row["currency"])
        if key in kinds and kinds[key] != row["kind"]:
            raise ValueError(
                f"Line {line}: payment and refund cannot share a reference"
            )
        kinds[key] = row["kind"]
        events[row["event_id"]] = row
    normalized = []
    audit = []
    totals = {}
    for event_id, row in sorted(events.items()):
        normalized.append(
            {
                "record_id": event_id,
                "reference": row["reference"],
                "currency": row["currency"],
                "amount_minor": int(row[basis + "_minor"]),
            }
        )
        amounts = {f: int(row[f]) for f in ("gross_minor", "fee_minor", "net_minor")}
        audit.append({**row, **amounts})
        total = totals.setdefault(row["currency"], dict.fromkeys(amounts, 0))
        for field, amount in amounts.items():
            total[field] += amount
    return normalized, {
        "adapter": "synthetic-provider-v1",
        "comparison_basis": basis,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "exact_duplicates_ignored": duplicates,
        "unique_events": len(events),
        "totals_by_currency": dict(sorted(totals.items())),
        "events": audit,
        "refund_parent_scope": (
            "Reference retained; parent existence, currency, "
            "and refund limits not validated"
        ),
    }


def write_batch(source, basis, output_dir):
    records, audit = normalize(source, basis)
    # A new directory protects prior imports; this is not a transactional store.
    output_dir.mkdir()
    csv_path = output_dir / "normalized.csv"
    with csv_path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records)
    audit["normalized_sha256"] = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    with (output_dir / "audit.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
    return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--basis", choices=("gross", "net"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        write_batch(args.source, args.basis, args.output_dir)
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
