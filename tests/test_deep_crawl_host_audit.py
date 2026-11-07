"""Offline tests: Docker host audit never exposes env values or mutates host."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import unittest
from unittest.mock import patch

from scripts import deep_crawl_host_audit as subject


_ID = "sha256:" + "a" * 64
_SECRET = "CRAWL4AI_API_TOKEN=TOP_SECRET_NEVER_OUTPUT"


def _raw(*, flags=None, image="unclecode/crawl4ai:latest",
         image_id=_ID, state="running"):
    env = [_SECRET] + (flags or [])
    return "\n".join(json.dumps(x) for x in (image, image_id, state, env)) + "\n"


def _completed(raw, code=0, err=""):
    return subprocess.CompletedProcess(args=[], returncode=code,
                                       stdout=raw, stderr=err)


class DeepCrawlHostAuditTests(unittest.TestCase):
    def test_fixed_read_only_inspect_and_no_secret_disclosure(self):
        with (patch.object(subject.subprocess, "run", return_value=_completed(
                _raw(flags=["CRAWL4AI_ALLOW_DEEP_CRAWL=false"]))) as command,
              contextlib.redirect_stdout(io.StringIO()) as output):
            rc = subject.main()
        self.assertEqual(rc, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["inspect"], "verified")
        self.assertEqual(report["state"], "running")
        self.assertEqual(report["configured_image"], "unclecode/crawl4ai:latest")
        self.assertEqual(report["local_image_id"], _ID)
        self.assertEqual(report["deep_crawl_container_env"], "disabled_in_container_env")
        self.assertFalse(report["deep_crawl_ready"])
        self.assertFalse(report["docker_modified"])
        self.assertNotIn("TOP_SECRET", output.getvalue())
        self.assertNotIn("CRAWL4AI_API_TOKEN", output.getvalue())
        self.assertEqual(command.call_count, 1)
        args, kwargs = command.call_args
        self.assertEqual(args[0][:4], ["docker", "inspect", "--type=container",
                                       "--format"])
        self.assertEqual(args[0][-1], "crawl4ai")
        self.assertEqual(kwargs["timeout"], 8)
        self.assertTrue(kwargs["capture_output"])
        self.assertFalse(kwargs["check"])

    def test_reports_flag_truth_without_claiming_effective_containment(self):
        samples = [
            ([], "not_set"),
            (["CRAWL4AI_ALLOW_DEEP_CRAWL=true"], "enabled_in_container_env"),
            (["CRAWL4AI_ALLOW_DEEP_CRAWL=false"], "disabled_in_container_env"),
            (["CRAWL4AI_ALLOW_DEEP_CRAWL=0"], "disabled_in_container_env"),
            (["CRAWL4AI_ALLOW_DEEP_CRAWL=1"], "unrecognized_value"),
            (["CRAWL4AI_ALLOW_DEEP_CRAWL=True"], "unrecognized_value"),
            (["CRAWL4AI_ALLOW_DEEP_CRAWL=true",
              "CRAWL4AI_ALLOW_DEEP_CRAWL=false"], "ambiguous"),
        ]
        for flags, wanted in samples:
            with (self.subTest(flags=flags),
                  patch.object(subject.subprocess, "run",
                               return_value=_completed(_raw(flags=flags)))):
                report = subject.report()
            self.assertEqual(report["deep_crawl_container_env"], wanted)
            self.assertFalse(report["deep_crawl_ready"])
            self.assertEqual(report["per_discovered_url_authorization"],
                             "not_verified")
            self.assertEqual(report["effective_page_depth_wall_limits"],
                             "not_verified")
            self.assertEqual(report["egress_guard"], "not_tested")

    def test_bounded_malformed_or_failed_inspects_fail_closed(self):
        invalid = [
            _completed(""), _completed("NOT JSON"),
            _completed("PRIVATE_CONTENT", code=1, err=_SECRET),
            _completed(_raw() + "another line\n"),
            _completed(_raw(image_id="sha256:not-a-digest")),
            _completed(_raw(image="--invalid")),
            _completed(_raw(state="untrusted")),
            _completed("X" * (subject._MAX_CAPTURE_BYTES + 1)),
            _completed(_raw(flags=[42])),
        ]
        for response in invalid:
            with (self.subTest(raw=response.stdout[:20]),
                  patch.object(subject.subprocess, "run",
                               return_value=response),
                  contextlib.redirect_stdout(io.StringIO()) as output):
                code = subject.main()
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue())["inspect"], "unavailable")
            self.assertNotIn("SECRET", output.getvalue())
            self.assertNotIn("PRIVATE", output.getvalue())

    def test_timeout_and_absent_docker_do_not_leak_exceptions(self):
        for exc in (OSError(_SECRET),
                    subprocess.TimeoutExpired(["docker", "inspect"], 8,
                                              output=_SECRET)):
            with (self.subTest(exc=type(exc).__name__),
                  patch.object(subject.subprocess, "run", side_effect=exc),
                  contextlib.redirect_stdout(io.StringIO()) as output):
                code = subject.main()
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue())["inspect"], "unavailable")
            self.assertNotIn("TOP_SECRET", output.getvalue())

    def test_refuses_unbounded_or_malformed_env_lists(self):
        for env in (None, "string", ["x=1"] * 257,
                    [_SECRET, "CRAWL4AI_ALLOW_DEEP_CRAWL"],
                    [_SECRET, "CRAWL4AI_ALLOW_DEEP_CRAWL=" + "x" * 16385]):
            with self.subTest(env_type=type(env).__name__):
                self.assertEqual(subject._flag_state(env), "unverified")


if __name__ == "__main__":
    unittest.main()
