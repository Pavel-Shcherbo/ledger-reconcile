import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapter import PROVIDER_FIELDS
from store import backup_store, current_records, event_history, import_batch


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "store.sqlite"

    def source(self, name, rows):
        p = self.root / name
        p.write_text(",".join(PROVIDER_FIELDS) + "\n" + "\n".join(rows) + "\n")
        return p

    def initial(self):
        return self.source("initial.csv", ["b,p,payment,,USD,100,5,95"])

    def test_replay_and_overlapping_exports_do_not_duplicate(self):
        p = self.initial()
        first = import_batch(self.db, p, "provider", "gross")
        self.assertTrue(import_batch(self.db, p, "provider", "gross")["replayed_batch"])
        overlap = self.source(
            "overlap.csv", ["b,p,payment,,USD,100,5,95", "c,r,refund,p,USD,-20,0,-20"]
        )
        result = import_batch(self.db, overlap, "provider", "gross")
        self.assertEqual((result["new"], result["unchanged"]), (1, 1))
        self.assertEqual(
            sum(x["amount_minor"] for x in current_records(self.db, "provider")), 80
        )
        self.assertEqual(first["batch_id"], 1)

    def test_conflict_rolls_back_entire_batch(self):
        import_batch(self.db, self.initial(), "s", "gross")
        conflict = self.source(
            "bad.csv", ["a,new,payment,,USD,40,0,40", "b,p,payment,,USD,200,5,195"]
        )
        with self.assertRaisesRegex(ValueError, "Conflict"):
            import_batch(self.db, conflict, "s", "gross")
        self.assertEqual([x["record_id"] for x in current_records(self.db, "s")], ["b"])
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 1
            )

    def test_explicit_correction_retains_history_and_stale_write_fails(self):
        original = self.initial()
        import_batch(self.db, original, "s", "net")
        changed = self.source("changed.csv", ["b,p,payment,,USD,120,5,115"])
        self.assertEqual(
            import_batch(self.db, changed, "s", "net", {"b": 1})["corrected"], 1
        )
        self.assertTrue(
            import_batch(self.db, changed, "s", "net", {"b": 1})["replayed_batch"]
        )
        import_batch(self.db, original, "s", "net")
        self.assertEqual(current_records(self.db, "s")[0]["amount_minor"], 115)
        stale = self.source("stale.csv", ["b,p,payment,,USD,130,5,125"])
        with self.assertRaisesRegex(ValueError, "stale"):
            import_batch(self.db, stale, "s", "net", {"b": 1})
        with sqlite3.connect(self.db) as connection:
            history = connection.execute(
                "SELECT revision,payload FROM revisions ORDER BY revision"
            ).fetchall()
        self.assertEqual([x[0] for x in history], [1, 2])
        self.assertIn('"gross_minor":100', history[0][1])

    def test_basis_and_namespace_are_explicit(self):
        p = self.initial()
        import_batch(self.db, p, "one", "gross")
        with self.assertRaisesRegex(ValueError, "basis"):
            import_batch(self.db, p, "one", "net")
        import_batch(self.db, p, "two", "net")
        self.assertEqual(current_records(self.db, "two")[0]["amount_minor"], 95)
        with self.assertRaisesRegex(ValueError, "Unknown stream"):
            current_records(self.db, "missing")

    def test_identity_change_is_not_an_amount_correction(self):
        import_batch(self.db, self.initial(), "s", "gross")
        changed = self.source("identity.csv", ["b,different,payment,,USD,120,0,120"])
        with self.assertRaisesRegex(ValueError, "identity"):
            import_batch(self.db, changed, "s", "gross", {"b": 1})

    def test_read_missing_db_does_not_create_file(self):
        with self.assertRaises(sqlite3.OperationalError):
            current_records(self.db, "s")
        self.assertFalse(self.db.exists())

    def test_unrelated_database_is_not_modified(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE customer_data(value TEXT)")
            connection.execute("INSERT INTO customer_data VALUES('keep')")
        before = self.db.read_bytes()
        with self.assertRaisesRegex(ValueError, "Not a supported"):
            import_batch(self.db, self.initial(), "s", "gross")
        self.assertEqual(self.db.read_bytes(), before)

    def test_backup_restores_wal_data_and_all_streams(self):
        original = self.initial()
        import_batch(self.db, original, "s", "gross")
        backup = self.root / "backup.sqlite"
        with sqlite3.connect(self.db) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            changed = self.source("changed.csv", ["b,p,payment,,USD,120,5,115"])
            import_batch(self.db, changed, "s", "gross", {"b": 1})
            import_batch(self.db, original, "other", "net")
            proof = backup_store(self.db, backup)
            self.assertEqual(len(proof["sha256"]), 64)
            self.assertEqual(
                current_records(backup, "s"), current_records(self.db, "s")
            )
            self.assertEqual(current_records(backup, "other")[0]["amount_minor"], 95)
            self.assertEqual(len(event_history(backup, "s", "b")), 2)
        self.assertEqual(current_records(backup, "s")[0]["amount_minor"], 120)
        before = backup.read_bytes()
        with self.assertRaises(FileExistsError):
            backup_store(self.db, backup)
        self.assertEqual(backup.read_bytes(), before)

    def test_backup_rejects_missing_or_unrelated_source_without_output(self):
        backup = self.root / "backup.sqlite"
        with self.assertRaises(sqlite3.OperationalError):
            backup_store(self.db, backup)
        self.assertFalse(self.db.exists())
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE unrelated(value TEXT)")
        with self.assertRaisesRegex(ValueError, "Not a supported"):
            backup_store(self.db, backup)
        self.assertFalse(backup.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_backup_is_private_even_with_permissive_umask(self):
        import_batch(self.db, self.initial(), "s", "gross")
        backup = self.root / "backup.sqlite"
        previous = os.umask(0o022)
        try:
            backup_store(self.db, backup)
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_backup_publication_failure_cleans_temporary_copy(self):
        import_batch(self.db, self.initial(), "s", "gross")
        backup = self.root / "backup.sqlite"
        before_files = set(self.root.iterdir())
        before_source = self.db.read_bytes()
        with patch("store.os.link", side_effect=OSError("Publication failed")):
            with self.assertRaisesRegex(OSError, "Publication failed"):
                backup_store(self.db, backup)
        self.assertFalse(backup.exists())
        self.assertEqual(set(self.root.iterdir()), before_files)
        self.assertEqual(self.db.read_bytes(), before_source)

    def test_restore_without_source_preserves_replay_and_correction_guards(self):
        original = self.initial()
        import_batch(self.db, original, "s", "gross")
        changed = self.source("changed.csv", ["b,p,payment,,USD,120,5,115"])
        import_batch(self.db, changed, "s", "gross", {"b": 1})
        snapshot = self.root / "snapshot.sqlite"
        backup_store(self.db, snapshot)
        snapshot_bytes = snapshot.read_bytes()
        self.db.unlink()

        restored = self.root / "restored.sqlite"
        backup_store(snapshot, restored)
        replay = import_batch(restored, original, "s", "gross")
        self.assertTrue(replay["replayed_batch"])
        self.assertEqual(current_records(restored, "s")[0]["amount_minor"], 120)
        further = self.source("further.csv", ["b,p,payment,,USD,130,5,125"])
        before = event_history(restored, "s", "b")
        with self.assertRaises(ValueError):
            import_batch(restored, further, "s", "gross", {"b": 1})
        self.assertEqual(event_history(restored, "s", "b"), before)
        result = import_batch(restored, further, "s", "gross", {"b": 2})
        self.assertEqual(result["batch_id"], 3)
        self.assertEqual(current_records(restored, "s")[0]["amount_minor"], 130)
        self.assertEqual(
            [r["revision"] for r in event_history(restored, "s", "b")], [1, 2, 3]
        )
        self.assertEqual(snapshot.read_bytes(), snapshot_bytes)
        self.assertFalse(self.db.exists())

    def test_replay_and_stale_correction_do_not_rewrite_existing_database(self):
        original = self.initial()
        import_batch(self.db, original, "s", "gross")
        changed = self.source("changed.csv", ["b,p,payment,,USD,120,5,115"])
        for action in ("replay", "stale"):
            with self.subTest(action=action):
                before = self.db.read_bytes()
                if action == "replay":
                    result = import_batch(self.db, original, "s", "gross")
                    self.assertTrue(result["replayed_batch"])
                else:
                    with self.assertRaises(ValueError):
                        import_batch(self.db, changed, "s", "gross", {"b": 2})
                self.assertEqual(self.db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
