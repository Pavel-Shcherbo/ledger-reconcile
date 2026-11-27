"""Export unmatched reconciliation groups for manual review as UTF-8 CSV."""

import argparse
import csv
import io
import json
from pathlib import Path

from reconcile import group_status
from report import REASONS, require_object

FIELDS = (
    "reference",
    "currency",
    "status",
    "left_record_ids",
    "right_record_ids",
    "left_minor",
    "right_minor",
    "delta_minor",
    "explanation",
)


def text_cell(value):
    if not isinstance(value, str):
        raise ValueError("Identifiers must be strings")
    if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(
        ("=", "+", "-", "@")
    ):
        return "'" + value
    return value


def records_summary(records):
    if not isinstance(records, list):
        raise ValueError("Records must be a list")
    ids, total = [], 0
    for record in records:
        require_object(record, "Record")
        if not isinstance(record["record_id"], str):
            raise ValueError("Record IDs must be strings")
        if type(record["amount_minor"]) is not int:
            raise ValueError("Amounts must be integer minor units")
        ids.append(record["record_id"])
        total += record["amount_minor"]
    return (
        json.dumps(ids, ensure_ascii=False),
        total,
        [r["amount_minor"] for r in records],
    )


def render_csv(report):
    require_object(report, "Report")
    if type(report.get("schema_version")) is not int or report["schema_version"] != 1:
        raise ValueError("Unsupported report schema")
    if not isinstance(report["items"], list):
        raise ValueError("Items must be a list")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(FIELDS)
    for item in report["items"]:
        require_object(item, "Item")
        status = item["status"]
        if not isinstance(status, str) or status not in REASONS:
            raise ValueError("Unknown reconciliation status")
        left_ids, left, left_amounts = records_summary(item["left"])
        right_ids, right, right_amounts = records_summary(item["right"])
        if status != group_status(left_amounts, right_amounts):
            raise ValueError("Status does not match record amounts and cardinality")
        if type(item["delta_minor"]) is not int or item["delta_minor"] != left - right:
            raise ValueError("Delta does not match record amounts")
        reference = text_cell(item["reference"])
        currency = text_cell(item["currency"])
        if status == "matched":
            continue
        writer.writerow(
            (
                reference,
                currency,
                status,
                left_ids,
                right_ids,
                left,
                right,
                left - right,
                REASONS[status],
            )
        )
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        rendered = render_csv(json.loads(args.report.read_text(encoding="utf-8")))
        with args.output.open("x", encoding="utf-8", newline="") as stream:
            stream.write(rendered)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
