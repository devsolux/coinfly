import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coinfly.domain.context import DomainError
from coinfly.worker.ownership import AuthorityOwnership
from coinfly.worker.paths import native_user_home


class OwnershipTests(unittest.TestCase):
    def test_alias_environment_track_and_run_directory_are_not_lock_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = AuthorityOwnership("attested-account", lock_directory=root)
            first.acquire()
            try:
                second = AuthorityOwnership("attested-account", lock_directory=root)
                self.assertEqual(first.path, second.path)
                with self.assertRaises(DomainError):
                    second.acquire()
            finally:
                first.close()
            second.acquire()
            second.close()

    def test_different_beneficial_authorities_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            with AuthorityOwnership("a", lock_directory=Path(directory)):
                with AuthorityOwnership("b", lock_directory=Path(directory)) as second:
                    self.assertTrue(second.held)

    @unittest.skipIf(os.name == "nt", "Unix symbolic-link test")
    def test_symlink_lock_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owner = AuthorityOwnership("a", lock_directory=root)
            target = root / "unrelated"
            target.write_text("untouched")
            owner.path.symlink_to(target)
            with self.assertRaises(DomainError):
                owner.acquire()
            self.assertEqual(target.read_text(), "untouched")

    def test_native_home_ignores_poisoned_environment(self):
        original = native_user_home()
        with patch.dict(os.environ, {"HOME": "/untrusted", "USERPROFILE": "C:\\untrusted"}):
            self.assertEqual(native_user_home(), original)
