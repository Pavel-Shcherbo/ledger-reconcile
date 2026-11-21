import copy
import json
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from reconcile import load_export, reconcile
from report import render


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.statuses = []
        self.scripts = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr" and "data-status" in attrs:
            self.statuses.append(attrs["data-status"])
        if tag == "script":
            self.scripts += 1


class ReportTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent / "examples"
        a, ap = load_export(root / "ledger.csv")
        b, bp = load_export(root / "provider.csv")
        self.report = reconcile(a, b)
        self.report["inputs"] = {"left": ap, "right": bp}

    def test_full_pipeline_retains_every_group_and_provenance(self):
        page = render(self.report)
        parsed = PageParser()
        parsed.feed(page)
        self.assertEqual(len(parsed.statuses), 6)
        self.assertEqual(parsed.statuses.count("matched"), 2)
        self.assertEqual(parsed.scripts, 1)
        self.assertIn(self.report["inputs"]["left"]["sha256"], page)
        self.assertIn("1 exact duplicates ignored", page)
        self.assertIn("Full-export totals (unaffected by filters)", page)

    def test_untrusted_values_cannot_create_script_or_markup(self):
        data = copy.deepcopy(self.report)
        payload = '</script><script>alert("x")</script><img src=x>'
        data["items"][0]["reference"] = payload
        data["items"][0]["left"][0]["record_id"] = payload
        data["inputs"]["left"]["sha256"] = payload
        page = render(data)
        parsed = PageParser()
        parsed.feed(page)
        self.assertEqual(parsed.scripts, 1)
        self.assertNotIn(payload, page)
        self.assertIn("&lt;/script&gt;", page)

    def test_rejects_unknown_schema_and_status(self):
        for key, value in (("schema_version", 2), ("status", "<script>")):
            with self.subTest(key=key):
                data = copy.deepcopy(self.report)
                if key == "status":
                    data["items"][0][key] = value
                else:
                    data[key] = value
                with self.assertRaises(ValueError):
                    render(data)

    def test_empty_report_is_renderable(self):
        page = render(reconcile([], []))
        parsed = PageParser()
        parsed.feed(page)
        self.assertEqual(parsed.statuses, [])
        self.assertIn("No rows match", page)

    def test_wrong_json_container_types_fail_without_output_or_traceback(self):
        cases = [[], None, "report", 1]
        for field in ("inputs", "totals_by_currency"):
            data = copy.deepcopy(self.report)
            data[field] = []
            cases.append(data)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "report.json"
            output = Path(folder) / "report.html"
            for index, data in enumerate(cases):
                with self.subTest(case=index):
                    source.write_text(json.dumps(data), encoding="utf-8")
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(Path(__file__).with_name("report.py")),
                            str(source),
                            "--output",
                            str(output),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertIn("must be a JSON object", result.stderr)
                    self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
