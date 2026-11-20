"""Exercise the public commands together with isolated synthetic files."""

import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CLITests(unittest.TestCase):
    def test_correction_snapshot_backup_and_report(self):
        source_root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def run(script, *args, expected=0):
                result = subprocess.run(
                    [sys.executable, str(source_root / script), *map(str, args)],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                return result.stdout

            header = (
                "event_id,reference,kind,parent_reference,currency,"
                "gross_minor,fee_minor,net_minor\n"
            )
            (root / "initial.csv").write_text(
                header + "p,payment,payment,,USD,100,5,95\n", encoding="utf-8"
            )
            (root / "corrected.csv").write_text(
                header + "p,payment,payment,,USD,120,5,115\n", encoding="utf-8"
            )
            flags = ("--db", "source.sqlite", "--stream", "provider")
            first = json.loads(
                run("store.py", "import", "initial.csv", *flags, "--basis", "gross")
            )
            run(
                "store.py",
                "import",
                "corrected.csv",
                *flags,
                "--basis",
                "gross",
                "--correct",
                "p:1",
            )
            run(
                "store.py",
                "export",
                *flags,
                "--as-of-batch",
                first["batch_id"],
                "--output",
                "historical.csv",
            )
            backup = json.loads(
                run(
                    "store.py",
                    "backup",
                    "--db",
                    "source.sqlite",
                    "--output",
                    "snapshot.sqlite",
                )
            )
            self.assertEqual(
                backup["sha256"],
                hashlib.sha256((root / "snapshot.sqlite").read_bytes()).hexdigest(),
            )
            before = (root / "snapshot.sqlite").read_bytes()
            restored = ("--db", "snapshot.sqlite", "--stream", "provider")
            run("store.py", "export", *restored, "--output", "current.csv")
            history = json.loads(run("store.py", "history", "p", *restored))
            self.assertEqual(len(history), 2)
            self.assertFalse(
                json.loads(run("store.py", "check-refunds", *restored))["has_issues"]
            )
            for name, amount in (("historical.csv", "100"), ("current.csv", "120")):
                with (root / name).open(newline="", encoding="utf-8") as stream:
                    self.assertEqual(
                        list(csv.DictReader(stream))[0]["amount_minor"], amount
                    )
            run(
                "reconcile.py",
                "historical.csv",
                "current.csv",
                "--output",
                "comparison.json",
                expected=1,
            )
            report = json.loads((root / "comparison.json").read_text(encoding="utf-8"))
            self.assertEqual(report["counts"], {"amount_mismatch": 1})
            self.assertEqual(report["totals_by_currency"]["USD"]["delta_minor"], -20)
            run("report.py", "comparison.json", "--output", "comparison.html")
            page = (root / "comparison.html").read_text(encoding="utf-8")
            self.assertIn('data-status="amount_mismatch"', page)
            self.assertIn(report["inputs"]["left"]["sha256"], page)
            run(
                "store.py",
                "backup",
                "--db",
                "source.sqlite",
                "--output",
                "snapshot.sqlite",
                expected=2,
            )
            self.assertEqual((root / "snapshot.sqlite").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
