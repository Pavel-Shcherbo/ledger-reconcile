import copy
import csv
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from exceptions_csv import FIELDS, render_csv


class ExceptionsCSVTests(unittest.TestCase):
    def setUp(self):
        self.report = json.loads(
            (Path(__file__).parent / "examples/report.json").read_text(encoding="utf-8")
        )

    def test_exception_groups_preserve_amounts_and_ambiguity(self):
        rows = list(csv.DictReader(io.StringIO(render_csv(self.report))))
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            {r["status"] for r in rows},
            {
                "amount_mismatch",
                "left_only",
                "right_only",
                "ambiguous",
            },
        )
        ambiguous = next(r for r in rows if r["status"] == "ambiguous")
        self.assertEqual(len(json.loads(ambiguous["right_record_ids"])), 2)
        for row in rows:
            self.assertEqual(
                int(row["delta_minor"]),
                int(row["left_minor"]) - int(row["right_minor"]),
            )

    def test_csv_quoting_and_formula_like_text(self):
        for value in ("=1+1", " +1", "-1", "@SUM(A1)", "\tformula", 'a,"b\nс'):
            with self.subTest(value=value):
                data = copy.deepcopy(self.report)
                data["items"] = [
                    next(i for i in data["items"] if i["status"] != "matched")
                ]
                data["items"][0]["reference"] = value
                row = next(csv.DictReader(io.StringIO(render_csv(data))))
                expected = value if value == 'a,"b\nс' else "'" + value
                self.assertEqual(row["reference"], expected)

    def test_invalid_amount_and_delta_are_rejected_and_empty_has_headers(self):
        for field, value in (
            ("amount_minor", True),
            ("amount_minor", 1.5),
            ("delta_minor", 999),
        ):
            data = copy.deepcopy(self.report)
            item = next(i for i in data["items"] if i["status"] == "amount_mismatch")
            if field == "amount_minor":
                item["left"][0][field] = value
            else:
                item[field] = value
            with self.assertRaises(ValueError):
                render_csv(data)
        self.assertEqual(
            list(
                csv.reader(
                    io.StringIO(
                        render_csv(
                            {
                                "schema_version": 1,
                                "items": [],
                            }
                        )
                    )
                )
            ),
            [list(FIELDS)],
        )

    def test_inconsistent_statuses_are_rejected_before_output(self):
        for status in ("matched", "left_only", "right_only", "ambiguous"):
            with self.subTest(status=status):
                data = copy.deepcopy(self.report)
                item = next(
                    i for i in data["items"] if i["status"] == "amount_mismatch"
                )
                item["status"] = status
                with tempfile.TemporaryDirectory() as folder:
                    source = Path(folder) / "report.json"
                    output = Path(folder) / "exceptions.csv"
                    source.write_text(json.dumps(data), encoding="utf-8")
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(Path(__file__).with_name("exceptions_csv.py")),
                            str(source),
                            "--output",
                            str(output),
                        ],
                        capture_output=True,
                        timeout=30,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertNotIn(b"Traceback", result.stderr)
                    self.assertFalse(output.exists())

    def test_matched_groups_and_empty_groups_are_validated(self):
        for mutation in ("bool_amount", "wrong_delta", "empty_group"):
            with self.subTest(mutation=mutation):
                data = copy.deepcopy(self.report)
                item = next(i for i in data["items"] if i["status"] == "matched")
                if mutation == "bool_amount":
                    item["left"][0]["amount_minor"] = True
                elif mutation == "wrong_delta":
                    item["delta_minor"] = 123
                else:
                    item["left"] = item["right"] = []
                with self.assertRaises(ValueError):
                    render_csv(data)

    def test_cli_refuses_overwrite_and_does_not_change_input(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, output = root / "report.json", root / "exceptions.csv"
            source.write_text(json.dumps(self.report), encoding="utf-8")
            before = source.read_bytes()
            command = [
                sys.executable,
                str(Path(__file__).with_name("exceptions_csv.py")),
                str(source),
                "--output",
                str(output),
            ]
            self.assertEqual(
                subprocess.run(command, capture_output=True, timeout=30).returncode, 0
            )
            original = output.read_bytes()
            self.assertEqual(
                subprocess.run(command, capture_output=True, timeout=30).returncode, 2
            )
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(source.read_bytes(), before)
            source.write_text("[]", encoding="utf-8")
            command[-1] = str(root / "invalid.csv")
            failure = subprocess.run(command, capture_output=True, timeout=30)
            self.assertEqual(failure.returncode, 2)
            self.assertNotIn(b"Traceback", failure.stderr)
            self.assertFalse((root / "invalid.csv").exists())


if __name__ == "__main__":
    unittest.main()
