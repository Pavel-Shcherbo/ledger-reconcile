import tempfile
import unittest
from pathlib import Path

from adapter import PROVIDER_FIELDS
from store import check_refunds, import_batch


class RefundTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.db = self.root / "store.sqlite"
        self.serial = 0

    def add(self, rows, corrections=None):
        self.serial += 1
        path = self.root / f"{self.serial}.csv"
        path.write_text(",".join(PROVIDER_FIELDS) + "\n" + "\n".join(rows) + "\n")
        return import_batch(self.db, path, "provider", "net", corrections)

    def test_partial_refunds_across_batches_and_fee_exclusion(self):
        self.add(["p,payment,payment,,USD,100,10,90"])
        self.add(["r1,refund1,refund,payment,USD,-60,5,-65"])
        self.add(["r2,refund2,refund,payment,USD,-40,5,-45"])
        before = self.db.read_bytes()
        result = check_refunds(self.db, "provider")
        self.assertEqual(result["refund_count"], 2)
        self.assertFalse(result["has_issues"])
        self.assertEqual(self.db.read_bytes(), before)

    def test_missing_parent_resolves_when_later_batch_arrives(self):
        self.add(["r,r,refund,p,USD,-20,0,-20"])
        self.assertEqual(
            check_refunds(self.db, "provider")["issues"][0]["code"], "missing_parent"
        )
        self.add(["p,p,payment,,USD,100,0,100"])
        self.assertFalse(check_refunds(self.db, "provider")["has_issues"])

    def test_currency_mismatch(self):
        self.add(["p,p,payment,,EUR,100,0,100", "r,r,refund,p,USD,-20,0,-20"])
        self.assertEqual(
            check_refunds(self.db, "provider")["issues"][0]["code"], "currency_mismatch"
        )

    def test_multiple_payment_records_cannot_be_guessed(self):
        self.add(["p1,p,payment,,USD,100,0,100", "p2,p,payment,,USD,100,0,100"])
        self.add(["r,r,refund,p,USD,-20,0,-20"])
        self.assertEqual(
            check_refunds(self.db, "provider")["issues"][0]["code"], "ambiguous_parent"
        )

    def test_cumulative_refunds_and_parent_correction(self):
        self.add(["p,p,payment,,USD,100,0,100"])
        self.add(["r1,r1,refund,p,USD,-60,0,-60", "r2,r2,refund,p,USD,-50,0,-50"])
        issue = check_refunds(self.db, "provider")["issues"][0]
        self.assertEqual(issue["code"], "exceeds_payment")
        self.assertEqual(issue["refunded_gross_minor"], 110)
        self.add(["p,p,payment,,USD,120,0,120"], {"p": 1})
        self.assertFalse(check_refunds(self.db, "provider")["has_issues"])


if __name__ == "__main__":
    unittest.main()
