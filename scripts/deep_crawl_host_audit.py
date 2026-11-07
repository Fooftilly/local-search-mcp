"""Read-only, sanitized audit of the operator's existing Crawl4AI container (#299).

Never start/stop a container, inspect an arbitrary target, print Docker
environment values, fetch public URLs, or enable any deep-crawl feature.
The only Docker operation is a fixed, formatted inspect of 'crawl4ai'.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

from scripts.crawler_compose import validate_image, ComposeSetupError

_CONTAINER = "crawl4ai"
_MAX_CAPTURE_BYTES = 256 * 1024
_FORMAT = ("{{json .Config.Image}}\n{{json .Image}}\n"
           "{{json .State.Status}}\n{{json .Config.Env}}")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _flag_state(env: object) -> str:
    """Treat duplicate, malformed and unrecognized settings as unknown.

    Never echo any environment string: it may contain the crawler API token
    and other operator credentials unrelated to the deep-crawl setting.
    """
    if type(env) is not list or len(env) > 256:
        return "unverified"
    values = []
    for item in env:
        if type(item) is not str or len(item) > 16384:
            return "unverified"
        key, separator, value = item.partition("=")
        if key == "CRAWL4AI_ALLOW_DEEP_CRAWL":
            if not separator:
                return "unverified"
            values.append(value)
    if len(values) > 1:
        return "ambiguous"
    if not values:
        return "not_set"
    if values[0] == "true":
        return "enabled_in_container_env"
    if values[0] in ("", "false", "0"):
        return "disabled_in_container_env"
    return "unrecognized_value"


def report() -> dict:
    result = {
        "mode": "read_only_deep_crawl_container_audit",
        "container": _CONTAINER,
        "inspect": "unavailable",
        "state": "unverified",
        "configured_image": None,
        "local_image_id": None,
        "deep_crawl_container_env": "unverified",
        "effective_deep_crawl_capability": "not_verified",
        "effective_page_depth_wall_limits": "not_verified",
        "per_discovered_url_authorization": "not_verified",
        "egress_guard": "not_tested",
        "deep_crawl_ready": False,
        "public_crawls": 0,
        "settings_modified": False,
        "docker_modified": False,
    }
    try:
        proc = subprocess.run(
            ["docker", "inspect", "--type=container", "--format", _FORMAT,
             _CONTAINER],
            capture_output=True, text=True, check=False, timeout=8,
        )
        # Avoid printing Docker's stdout OR stderr: either could contain
        # operator-private metadata, including environment values.
        if proc.returncode != 0 or len(proc.stdout) > _MAX_CAPTURE_BYTES:
            return result
        lines = proc.stdout.splitlines()
        if len(lines) != 4:
            return result
        image, image_id, state, env = (json.loads(s) for s in lines)
        if type(image) is not str or type(image_id) is not str or type(state) is not str:
            return result
        image = validate_image(image)
        if not _IMAGE_ID.fullmatch(image_id):
            return result
        if state not in ("running", "exited", "created", "restarting",
                         "paused", "dead"):
            return result
        result.update({
            "inspect": "verified",
            "state": state,
            "configured_image": image,
            # Docker .Image is a LOCAL image ID, not a signed registry digest
            # or proof that the image contents match a published release.
            "local_image_id": image_id,
            "deep_crawl_container_env": _flag_state(env),
        })
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError,
            ComposeSetupError, subprocess.TimeoutExpired):
        return result
    return result


def main() -> int:
    outcome = report()
    print(json.dumps(outcome, sort_keys=True, indent=2))
    return 0 if outcome["inspect"] == "verified" else 1


if __name__ == "__main__":
    sys.exit(main())
