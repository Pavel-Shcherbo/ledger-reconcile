import itertools
import json
import tempfile
import unittest
from pathlib import Path

from reconcile import Record, load_export, main, reconcile


class ReconciliationTests(unittest.TestCase):
    def test_equal_totals_do_not_hide_opposing_errors(self):
        left = [Record("a", "p1", "USD", 100), Record("b", "p2", "USD", 200)]
        right = [Record("c", "p1", "USD", 200), Record("d", "p2", "USD", 100)]
        result = reconcile(left, right)
        self.assertEqual(result["totals_by_currency"]["USD"]["delta_minor"], 0)
        self.assertEqual(result["counts"], {"amount_mismatch": 2})
        self.assertTrue(result["has_exceptions"])

    def test_split_payment_is_ambiguous_even_when_sum_matches(self):
        result = reconcile(
            [Record("a", "p", "USD", 100)],
            [Record("b", "p", "USD", 40), Record("c", "p", "USD", 60)],
        )
        self.assertEqual(result["counts"], {"ambiguous": 1})

    def test_currency_mismatch_never_matches(self):
        result = reconcile(
            [Record("a", "p", "USD", 100)], [Record("b", "p", "EUR", 100)]
        )
        self.assertEqual(result["counts"], {"left_only": 1, "right_only": 1})

    def test_signed_refund_and_row_order(self):
        left = [Record("a", "p1", "USD", 100), Record("b", "refund", "USD", -25)]
        right = [Record("c", "refund", "USD", -25), Record("d", "p1", "USD", 100)]
        expected = reconcile(left, right)
        for a, b in itertools.product(
            itertools.permutations(left), itertools.permutations(right)
        ):
            self.assertEqual(reconcile(list(a), list(b)), expected)
        self.assertEqual(expected["counts"], {"matched": 2})

    def test_import_deduplicates_only_identical_record_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "x.csv"
            path.write_text(
                "record_id,reference,currency,amount_minor\na,p,USD,100\na,p,USD,100\n"
            )
            records, proof = load_export(path)
            self.assertEqual(len(records), 1)
            self.assertEqual(proof["exact_duplicates_ignored"], 1)
            path.write_text(path.read_text() + "a,p,USD,101\n")
            with self.assertRaisesRegex(ValueError, "conflicting record_id"):
                load_export(path)

    def test_invalid_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "x.csv"
            for row in (
                "a,p,USD,1.50",
                "a,p,USD,01",
                "a,p,usd,100",
                "a,,USD,100",
                "a,p,USD,100,extra",
            ):
                with self.subTest(row=row):
                    path.write_text(
                        "record_id,reference,currency,amount_minor\n" + row + "\n"
                    )
                    with self.assertRaises(ValueError):
                        load_export(path)

    def test_cli_exit_codes_and_input_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            a, b, output = (root / n for n in ("a.csv", "b.csv", "out.json"))
            content = "record_id,reference,currency,amount_minor\na,p,USD,100\n"
            a.write_text(content)
            b.write_text(content)
            self.assertEqual(main([str(a), str(b), "--output", str(output)]), 0)
            report = json.loads(output.read_text())
            self.assertEqual(
                report["inputs"]["left"]["sha256"], report["inputs"]["right"]["sha256"]
            )
            b.write_text(content.replace("100", "99"))
            self.assertEqual(
                main([str(a), str(b), "--output", str(root / "mismatch.json")]), 1
            )
            self.assertEqual(main([str(a), str(b), "--output", str(output)]), 2)
            self.assertEqual(json.loads(output.read_text()), report)


if __name__ == "__main__":
    unittest.main()
