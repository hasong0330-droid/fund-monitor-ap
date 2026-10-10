import json
import tempfile
import unittest
from pathlib import Path

from storage import (
    AlreadyRunningError,
    atomic_write_json,
    single_instance_lock,
)


class StorageTests(unittest.TestCase):
    def test_atomic_json_write_is_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            atomic_write_json(path, {"ok": True, "items": [1, 2]})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["items"], [1, 2])

    def test_single_instance_lock_rejects_second_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.lock"
            with single_instance_lock(path):
                with self.assertRaises(AlreadyRunningError):
                    with single_instance_lock(path):
                        pass


if __name__ == "__main__":
    unittest.main()
