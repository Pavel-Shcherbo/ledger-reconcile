import json
import tempfile
import unittest
from pathlib import Path

from adapter import PROVIDER_FIELDS, normalize, write_batch
from reconcile import load_export, reconcile


class AdapterTests(unittest.TestCase):
    source = Path(__file__).parent / "examples" / "provider-v1.csv"

    def test_gross_and_net_are_explicit_and_fees_retained(self):
        gross, proof = normalize(self.source, "gross")
        net, _ = normalize(self.source, "net")
        self.assertEqual(
            [r["amount_minor"] for r in gross], [10000, -2000, -1000, 3000]
        )
        self.assertEqual([r["amount_minor"] for r in net], [9750, -2000, -1025, 2950])
        self.assertEqual(
            proof["totals_by_currency"]["USD"],
            {"gross_minor": 7000, "fee_minor": 275, "net_minor": 6725},
        )
        self.assertEqual(proof["exact_duplicates_ignored"], 1)
        self.assertEqual(proof["events"][1]["parent_reference"], "payment-200")
        with self.assertRaises(ValueError):
            normalize(self.source, None)

    def test_distinct_partial_refunds_survive_reconciliation(self):
        rows, _ = normalize(self.source, "gross")
        from reconcile import Record

        records = [Record(**r) for r in rows]
        result = reconcile(records, records)
        self.assertEqual(result["counts"], {"matched": 4})

    def test_invalid_arithmetic_signs_links_and_conflicts(self):
        valid = "e,p,payment,,USD,100,5,95"
        cases = [
            "e,p,payment,,USD,100,5,96",
            "e,p,payment,,USD,100,-5,105",
            "e,p,payment,,USD,-100,5,-105",
            "e,r,refund,,USD,-100,0,-100",
            "e,r,refund,r,USD,-100,0,-100",
            "e,r,refund,p,USD,100,0,100",
            "e,p,payment,,USD,1.00,0,1.00",
            valid + "\ne,p,payment,,USD,100,6,94",
            valid + "\nf,p,refund,original,USD,-50,0,-50",
        ]
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "input.csv"
            for case in cases:
                with self.subTest(case=case):
                    source.write_text(",".join(PROVIDER_FIELDS) + "\n" + case + "\n")
                    with self.assertRaises(ValueError):
                        normalize(source, "gross")

    def test_batch_can_be_read_by_core_and_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "batch"
            proof = write_batch(self.source, "gross", output)
            rows, core_proof = load_export(output / "normalized.csv")
            self.assertEqual(len(rows), 4)
            self.assertEqual(proof["normalized_sha256"], core_proof["sha256"])
            self.assertEqual(json.loads((output / "audit.json").read_text()), proof)
            before = (output / "normalized.csv").read_bytes()
            with self.assertRaises(FileExistsError):
                write_batch(self.source, "net", output)
            self.assertEqual((output / "normalized.csv").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
