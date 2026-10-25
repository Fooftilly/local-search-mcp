"""Offline rollback candidate preparation; tests never change live worktrees."""
from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
import json
import io
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import backup_state
from scripts import rollback_candidate as candidate


class RollbackCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Rollback Test")
        self.git("config", "user.email", "rollback@example.invalid")
        (self.root / "bridge.py").write_text("old behavior\n")
        (self.root / "requirements.txt").write_text("httpx>=0.28,<1\n")
        (self.root / "start.sh").write_text("#!/bin/sh\necho safe\n")
        (self.root / "start.sh").chmod(0o755)
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.target = self.git("rev-parse", "HEAD")
        (self.root / "bridge.py").write_text("new behavior\n")
        self.git("add", ".")
        self.git("commit", "-qm", "current")
        self.current = self.git("rev-parse", "HEAD")
        self.state = self.base / "data"
        self.state.mkdir(mode=0o700)
        with closing(sqlite3.connect(self.state / "domains.sqlite3")) as db:
            db.execute("CREATE TABLE domains (domain TEXT, status TEXT)")
            db.execute("INSERT INTO domains VALUES ('example.org','allowed')")
            db.commit()
        (self.state / "domains.sqlite3").chmod(0o600)
        self.archive = self.base / "backup.zip"
        backup_state.backup(self.archive, state_dir=self.state)
        self.dir = self.base / "rollback-staged"

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_stage_only_local_source_and_verified_private_state(self):
        with patch.object(candidate.restore_smoke, "smoke", return_value=(1, 0)):
            report = candidate.prepare(self.target, self.archive, self.dir,
                                       root=self.root)
        self.assertEqual(report["source_commit"], self.current)
        self.assertEqual(report["target_commit"], self.target)
        self.assertFalse(report["activation_performed"])
        self.assertFalse(report["ready_for_live_use"])
        self.assertEqual((self.dir / "source" / "bridge.py").read_text(),
                         "old behavior\n")
        self.assertEqual((self.root / "bridge.py").read_text(), "new behavior\n")
        self.assertEqual(stat.S_IMODE(
            (self.dir / "source" / "start.sh").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(
            (self.dir / "source" / "bridge.py").stat().st_mode), 0o600)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.current)
        self.assertTrue((self.dir / "state" / "domains.sqlite3").exists())
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(
            (self.dir / "state" / "domains.sqlite3").stat().st_mode), 0o600)
        self.assertEqual(json.loads((self.dir / "candidate.json").read_text()), report)

    def test_candidate_inventory_is_private_and_read_only(self):
        from scripts import verify_rollback_candidate
        with patch.object(candidate.restore_smoke, "smoke", return_value=(1, 0)):
            report = candidate.prepare(self.target, self.archive, self.dir,
                                       root=self.root)
        self.assertEqual(report["schema"], "local-search-mcp/rollback-candidate/v2")
        self.assertEqual(set(report["inventory"]), {
            "source/bridge.py", "source/requirements.txt", "source/start.sh",
            "state/domains.sqlite3",
        })
        with (patch.object(candidate.update, "restart",
                           side_effect=AssertionError("must not restart")),
              patch.object(candidate.update, "write_state",
                           side_effect=AssertionError("must not write baseline")),
              patch.object(candidate, "_copy_git_blobs",
                           side_effect=AssertionError("must not access Git")),
              redirect_stdout(io.StringIO()) as output,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(verify_rollback_candidate.main([str(self.dir)]), 0,
                             errors.getvalue())
        result = json.loads(output.getvalue())
        self.assertTrue(result["integrity_verified"])
        self.assertFalse(result["activation_performed"])
        self.assertFalse(result["ready_for_live_use"])
        self.assertNotIn(str(self.archive), output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_inventory_rejects_source_edits_symlink_and_extra_file(self):
        from scripts import verify_rollback_candidate
        with patch.object(candidate.restore_smoke, "smoke", return_value=(1, 0)):
            candidate.prepare(self.target, self.archive, self.dir, root=self.root)
        source = self.dir / "source" / "bridge.py"
        original = source.read_bytes()
        source.write_bytes(original + b"unexpected")
        with self.assertRaisesRegex(ValueError, "changed"):
            candidate.inspect(self.dir)
        source.write_bytes(original)
        self.assertTrue(candidate.inspect(self.dir)["integrity_verified"])
        executable = self.dir / "source" / "start.sh"
        executable.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "changed"):
            candidate.inspect(self.dir)
        executable.chmod(0o700)
        extra_dir = self.dir / "source" / "unexpected_empty_dir"
        extra_dir.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "changed"):
            candidate.inspect(self.dir)
        extra_dir.rmdir()
        source.unlink()
        source.symlink_to(self.root / "bridge.py")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            candidate.inspect(self.dir)
        source.unlink()
        source.write_bytes(original)
        source.chmod(0o600)
        unexpected = self.dir / "state" / "config.json"
        unexpected.write_text('{"token":"MUST_NOT_READ"}')
        unexpected.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "unexpected"):
            candidate.inspect(self.dir)
        unexpected.unlink()
        outside = self.base / "PRIVATE_INTERNAL_ARCHIVE_PATH.zip"
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(verify_rollback_candidate.main([str(outside)]), 1)
        self.assertNotIn(str(outside), errors.getvalue())

    def test_inventory_rejects_tampered_policy_permissions_and_manifest(self):
        with patch.object(candidate.restore_smoke, "smoke", return_value=(1, 0)):
            candidate.prepare(self.target, self.archive, self.dir, root=self.root)
        db = self.dir / "state" / "domains.sqlite3"
        db.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            candidate.inspect(self.dir)
        db.chmod(0o600)
        db.write_bytes(b"not a database")
        with self.assertRaises(ValueError):
            candidate.inspect(self.dir)
        # Even a rewritten manifest must use the current staging schema.
        metadata = self.dir / "candidate.json"
        doc = json.loads(metadata.read_text())
        doc["schema"] = "local-search-mcp/rollback-candidate/v1"
        metadata.write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            candidate.inspect(self.dir)

    def test_nested_source_parents_created_private_with_usual_umask(self):
        self.git("checkout", "-q", "--detach", self.target)
        deep = self.root / "nested" / "inner" / "module.py"
        deep.parent.mkdir(parents=True)
        deep.write_text("print('historical fixture')\n")
        self.git("add", ".")
        self.git("commit", "-qm", "nested historical source")
        target = self.git("rev-parse", "HEAD")
        destination = self.base / "private-raw-copy"
        destination.mkdir(mode=0o700)
        old_mask = os.umask(0o022)
        try:
            files = candidate._copy_git_blobs(self.root, target, destination)
        finally:
            os.umask(old_mask)
        self.assertGreaterEqual(files, 4)
        for folder in (destination / "nested", destination / "nested" / "inner"):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)

    def test_inspection_never_reopens_stage_files_after_hash_checks(self):
        with patch.object(candidate.restore_smoke, "smoke", return_value=(1, 0)):
            candidate.prepare(self.target, self.archive, self.dir, root=self.root)
        with patch.object(Path, "read_bytes", side_effect=AssertionError(
                "Staged state was reopened by path")):
            result = candidate.inspect(self.dir)
        self.assertTrue(result["integrity_verified"])

    def test_malicious_deeply_nested_manifest_is_redacted(self):
        from scripts import verify_rollback_candidate
        with patch.object(candidate.restore_smoke, "smoke", return_value=(1, 0)):
            candidate.prepare(self.target, self.archive, self.dir, root=self.root)
        manifest = self.dir / "candidate.json"
        manifest.write_text("[" * 600 + '"SECRET_INTERNAL_DATA"' + "]" * 600)
        manifest.chmod(0o600)
        with (redirect_stdout(io.StringIO()) as output,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(verify_rollback_candidate.main([str(self.dir)]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("SECRET_INTERNAL_DATA", errors.getvalue())
        self.assertNotIn(str(self.dir), errors.getvalue())

    def test_oversize_candidate_inventory_refuses_stage_without_leaks(self):
        giant = {"source/" + str(i).zfill(6) + "-" + "x" * 500:
                 "F:0600:" + "f" * 64 for i in range(3500)}
        with (patch.object(candidate.restore_smoke, "smoke", return_value=(1, 0)),
              patch.object(candidate, "_inventory", return_value=giant)):
            with self.assertRaisesRegex(ValueError, "manifest limit"):
                candidate.prepare(self.target, self.archive, self.dir,
                                  root=self.root)
        self.assertFalse(self.dir.exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.current)

    def test_failure_cleans_new_candidate_without_touching_checkout(self):
        with (patch.object(candidate.restore_smoke, "smoke", return_value=(1, 0)),
              patch.object(candidate, "_copy_git_blobs",
                           side_effect=ValueError("invalid tracked source"))):
            with self.assertRaisesRegex(ValueError, "invalid tracked source"):
                candidate.prepare(self.target, self.archive, self.dir,
                                  root=self.root)
        self.assertFalse(self.dir.exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.current)
        self.assertEqual((self.root / "bridge.py").read_text(), "new behavior\n")

    def test_refuses_existing_destination_and_wrong_permissions(self):
        self.dir.mkdir()
        with patch.object(candidate.restore_smoke, "smoke", return_value=(1, 0)):
            with self.assertRaisesRegex(ValueError, "must not exist"):
                candidate.prepare(self.target, self.archive, self.dir,
                                  root=self.root)
        self.assertTrue(self.dir.exists())
        self.dir.rmdir()
        self.base.chmod(0o755)
        with patch.object(candidate.restore_smoke, "smoke", return_value=(1, 0)):
            with self.assertRaisesRegex(ValueError, "private"):
                candidate.prepare(self.target, self.archive, self.dir,
                                  root=self.root)
        self.assertFalse(self.dir.exists())
        self.base.chmod(0o700)

    def test_rejects_gitlink_symlink_and_unsafe_names(self):
        code = self.base / "output"
        code.mkdir()
        for name in ("../escape", "/absolute", "safe/../../escape",
                     "double//separator", "back\\slash"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                candidate._valid_member(name)
        bad_tree = b"120000 link\0" + bytes.fromhex("a" * 40)
        with self.assertRaisesRegex(ValueError, "Unsupported tracked Git entry"):
            candidate._parse_tree(bad_tree, 20)
        with self.assertRaisesRegex(ValueError, "Invalid local Git object identifier"):
            candidate._object(self.root, "--upload-pack=/tmp/evil", "blob", 100)
        self.assertEqual(list(code.rglob("*")), [])

    def test_reused_empty_subtrees_cannot_exhaust_git_object_walk(self):
        """A shallow fan-out of directories with no blobs must be bounded."""
        root_oid = "a" * 40
        empty_oid = "b" * 40
        children = b"".join(
            b"40000 sub" + str(i).encode("ascii") + b"\0" +
            bytes.fromhex(empty_oid) for i in range(candidate.MAX_TREE_VISITS + 2)
        )
        queried = []

        def git_object(_root, oid, kind, _limit):
            queried.append((oid, kind))
            if kind == "commit":
                return b"tree " + root_oid.encode("ascii") + b"\nauthor Fixture\n"
            if oid == root_oid and kind == "tree":
                return children
            if oid == empty_oid and kind == "tree":
                return b""
            self.fail("Unexpected Git object lookup")

        destination = self.base / "bounded-source"
        destination.mkdir()
        with patch.object(candidate, "_object", side_effect=git_object):
            with self.assertRaisesRegex(ValueError, "Too many Git tree visits"):
                candidate._copy_git_blobs(self.root, self.target, destination)
        self.assertLessEqual(len(queried), candidate.MAX_TREE_VISITS + 1)
        self.assertEqual(list(destination.iterdir()), [])

    def test_local_smudge_filter_never_executes_during_staging(self):
        marker = self.base / "filter-triggered"
        command = self.base / "smudge.sh"
        command.write_text("#!/bin/sh\nprintf 'ran' > " + str(marker) + "\ncat\n")
        command.chmod(0o700)
        attributes = self.base / "global-attributes"
        attributes.write_text("*.py filter=unsafe\n")
        self.git("config", "core.attributesFile", str(attributes))
        self.git("config", "filter.unsafe.smudge", str(command))
        with patch.object(candidate.restore_smoke, "smoke", return_value=(1, 0)):
            candidate.prepare(self.target, self.archive, self.dir, root=self.root)
        self.assertFalse(marker.exists(), "Raw blob staging executed a smudge command")
        self.assertEqual((self.dir / "source" / "bridge.py").read_text(),
                         "old behavior\n")

    def test_cli_never_applies_unconfirmed_rollback(self):
        with patch.object(candidate, "prepare",
                          side_effect=AssertionError("Do not stage")):
            self.assertEqual(candidate.main([
                "--target", self.target, "--archive", str(self.archive),
                "--destination", str(self.dir)]), 1)
        self.assertFalse(self.dir.exists())

    def test_unavailable_bad_git_or_corrupt_backup_cannot_stage(self):
        bad_targets = (self.current, self.target[:8], "f" * 40)
        for target in bad_targets:
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    candidate.prepare(target, self.archive, self.dir, root=self.root)
                self.assertFalse(self.dir.exists())
        self.archive.write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            candidate.prepare(self.target, self.archive, self.dir, root=self.root)
        self.assertFalse(self.dir.exists())


if __name__ == "__main__":
    unittest.main()
