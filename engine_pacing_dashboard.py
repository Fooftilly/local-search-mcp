"""Owner-only, CAS-protected editor for opt-in isolated SearXNG engine pacing.

Policy edits neither wake Docker nor change SearXNG engine flags. The live
engine inventory must be verifiable before enabling any pacing policy.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat

import engine_dashboard
import engine_pacing
import request_cache
from web_templates import render_template

LOCK_NAME = ".engine-pacing-policy.lock"
TMP_PREFIX = ".engine-pacing-policy-"
MAX_RULE_TEXT = 4096


def snapshot() -> dict:
    """Read exactly the same private policy consumed by bridge.py."""
    directory = engine_pacing._private_dir()
    try:
        raw = (engine_pacing._read_regular(
            directory, engine_pacing.FILENAME, engine_pacing.MAX_POLICY_BYTES)
               if directory is not None else None)
    finally:
        if directory is not None:
            os.close(directory)
    if raw is None:
        return {"enabled": False, "engines": {}, "state": hashlib.sha256(b"").hexdigest()}
    engine_pacing._parse(raw)
    document = json.loads(raw.decode("utf-8"), object_pairs_hook=engine_pacing._unique_pairs)
    return {"enabled": document["enabled"],
            "engines": {name: config["min_spacing_seconds"]
                        for name, config in document["engines"].items()},
            "state": hashlib.sha256(raw).hexdigest()}


def _parse_rules(raw: str) -> dict[str, int]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RULE_TEXT:
        raise ValueError("Invalid pacing engine rules")
    result = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        if "=" not in line:
            raise ValueError("Each engine rule must be NAME=SECONDS")
        name, seconds = line.split("=", 1)
        if (not engine_pacing._ENGINE.fullmatch(name)
                or name != name.strip()
                or name.casefold().startswith("google")
                or name.casefold() in result
                or not re.fullmatch(r"[0-9]{1,4}", seconds)):
            raise ValueError("Invalid or repeated pacing engine rule")
        interval = int(seconds)
        if interval > engine_pacing.MAX_SPACING_SECONDS:
            raise ValueError("Pacing interval must be 0–3600 seconds")
        result[name.casefold()] = (name, interval)
        if len(result) > engine_pacing.MAX_ENGINES:
            raise ValueError("Too many pacing engine rules")
    return {name: interval for name, interval in result.values()}


def _verified_engine_names() -> set[str]:
    """Recheck owner-verified SearXNG inventory without starting the backend."""
    try:
        inventory, _, _ = engine_dashboard.snapshot()
    except (ValueError, OSError, engine_dashboard.engines.on_demand_docker.LifecycleError) as exc:
        raise ValueError("SearXNG inventory is unavailable; cannot enable pacing") from exc
    # Match SearXNG's exact /config spelling: bridge selection validation
    # is deliberately case-sensitive and does not normalize engine labels.
    return {item["name"] for item in inventory
            if item["enabled"] and not item["name"].casefold().startswith("google")
            and engine_pacing._ENGINE.fullmatch(item["name"])}


def apply_form(fields: dict[str, list[str]]) -> str:
    if not isinstance(fields, dict) or not set(fields).issubset(
            {"csrf", "state", "rules", "enabled", "confirm"}):
        raise ValueError("Unexpected pacing form fields")
    for name, values in fields.items():
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
            raise ValueError("Duplicate or invalid pacing form field")
    enabled = fields.get("enabled", []) == ["on"]
    if "enabled" in fields and not enabled:
        raise ValueError("Invalid pacing enable flag")
    state = fields.get("state", [""])[0]
    if not re.fullmatch(r"[a-f0-9]{64}", state):
        raise ValueError("Invalid pacing revision; reload the page")
    rules = _parse_rules(fields.get("rules", [""])[0])
    if enabled and not rules:
        raise ValueError("Enabled pacing requires at least one engine rule")
    document = {"schema": engine_pacing.SCHEMA, "enabled": enabled,
                "engines": {name: {"min_spacing_seconds": interval}
                            for name, interval in rules.items()}}
    payload = (json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > engine_pacing.MAX_POLICY_BYTES:
        raise ValueError("Pacing policy is too large")
    engine_pacing._parse(payload)

    directory = engine_pacing._private_dir(create=True)
    tmp_name = ""
    try:
        lock = os.open(LOCK_NAME,
                       os.O_RDWR | os.O_CREAT | engine_pacing._NOFOLLOW,
                       0o600, dir_fd=directory)
        try:
            info = os.fstat(lock)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError("Unsafe pacing settings lock")
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = engine_pacing._read_regular(
                directory, engine_pacing.FILENAME, engine_pacing.MAX_POLICY_BYTES)
            if previous is not None:
                engine_pacing._parse(previous)
            before = previous if previous is not None else b""
            if hashlib.sha256(before).hexdigest() != state:
                raise ValueError("Pacing policy changed; refresh before saving")
            old_enabled = (json.loads(previous)["enabled"] if previous is not None else False)
            expected_confirm = "ENABLE" if enabled else "DISABLE" if old_enabled else "APPLY"
            if fields.get("confirm") != [expected_confirm]:
                raise ValueError(f"Type {expected_confirm} to confirm pacing policy changes")
            if enabled:
                eligible = _verified_engine_names()
                if not all(name in eligible for name in rules):
                    raise ValueError("Pacing engine is unknown, case-mismatched, or disabled in verified SearXNG")
            tmp_name = TMP_PREFIX + secrets.token_hex(12)
            fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         engine_pacing._NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp_name, engine_pacing.FILENAME,
                       src_dir_fd=directory, dst_dir_fd=directory)
            tmp_name = ""
            os.fsync(directory)
        finally:
            os.close(lock)
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)
    request_cache.invalidate_search()
    return "Engine pacing policy saved. Existing searches are not interrupted."


def page(csrf: str, message: str = "", error: bool = False,
         submitted: dict[str, list[str]] | None = None) -> str:
    from html import escape
    values = snapshot()
    current = values["engines"]
    if submitted is None:
        rules = "\n".join(f"{name}={interval}" for name, interval in sorted(current.items()))
        enabled = values["enabled"]
        state = values["state"]
    else:
        rules = submitted.get("rules", [""])[0]
        enabled = submitted.get("enabled") == ["on"]
        state = submitted.get("state", [values["state"]])[0]
    alert = (render_template("engine-alert",
                             kind="error" if error else "success",
                             message=escape(message, quote=True))
             if message else "")
    return render_template(
        "engine-pacing-settings", alert=alert, csrf=escape(csrf, quote=True),
        state=escape(state, quote=True), rules=escape(rules, quote=True),
        checked=" checked" if enabled else "")
