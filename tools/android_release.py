#!/usr/bin/env python3
"""Dispatch and verify the production Android release workflow.

This is an operator frontend only. Production signing, APK publication and signed
update-policy publication remain owned by .github/workflows/android-release.yml.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = "android-release.yml"
RELEASE_BRANCH = "main"


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


def run_command(
    args: Iterable[str], *, check: bool = True, capture: bool = True
) -> CommandResult:
    command = [str(item) for item in args]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=capture,
        check=False,
    )
    result = CommandResult(completed.stdout or "", completed.stderr or "", completed.returncode)
    if check and completed.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {completed.returncode}"
        raise ReleaseError(f"command failed: {' '.join(command)}\n{detail}")
    return result


def git(*args: str) -> str:
    return run_command(("git", *args)).stdout.strip()


def gh(*args: str, check: bool = True) -> CommandResult:
    return run_command(("gh", *args), check=check)


def github_repo_from_remote(remote: str) -> str:
    value = remote.strip()
    if not value:
        raise ReleaseError("origin remote is empty")

    if re.match(r"^[^/@:]+@github\.com:", value):
        path = value.split(":", 1)[1]
    else:
        parsed = urlparse(value)
        if parsed.hostname != "github.com":
            raise ReleaseError(f"origin is not a github.com repository: {value}")
        path = parsed.path

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        raise ReleaseError(f"cannot derive owner/repository from origin: {value}")
    return f"{parts[0]}/{parts[1]}"


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise ReleaseError(f"required command is not installed: {name}")


def load_policy(repo: str, channel: str) -> dict | None:
    branch = f"update-{channel.lower()}"
    result = gh(
        "api",
        "-H",
        "Accept: application/vnd.github.raw+json",
        f"repos/{repo}/contents/policy.json?ref={branch}",
        check=False,
    )
    if result.returncode == 0:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"{branch}/policy.json is not valid JSON") from exc
    if "404" in result.stderr or "Not Found" in result.stderr or "No commit found" in result.stderr:
        return None
    raise ReleaseError(result.stderr.strip() or f"cannot read {branch}/policy.json")


def current_build_number(policies: Iterable[dict | None]) -> int:
    numbers: list[int] = []
    for policy in policies:
        if not policy:
            continue
        for release in policy.get("releases", []):
            value = release.get("build_number")
            if isinstance(value, int):
                numbers.append(value)
    return max(numbers, default=0)


def current_sequence(policy: dict | None) -> int:
    if not policy:
        return 0
    value = policy.get("sequence")
    if not isinstance(value, int):
        raise ReleaseError("published policy sequence is missing or not an integer")
    return value


def prompt_text(label: str, *, default: str | None = None, required: bool = True) -> str:
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        try:
            value = input(f"{label}{suffix}: ").strip()
        except EOFError as exc:
            raise ReleaseError(f"missing required interactive input: {label}") from exc
        if not value and default is not None:
            value = default
        if value or not required:
            return value
        print(f"{label} is required.", file=sys.stderr)


def prompt_choice(label: str, choices: tuple[str, ...], default: str) -> str:
    options = "/".join(choices)
    while True:
        value = prompt_text(f"{label} ({options})", default=default).upper()
        if value in choices:
            return value
        print(f"Choose one of: {', '.join(choices)}", file=sys.stderr)


def prompt_int(label: str, default: int) -> int:
    while True:
        value = prompt_text(label, default=str(default))
        try:
            parsed = int(value)
        except ValueError:
            print(f"{label} must be an integer.", file=sys.stderr)
            continue
        if parsed > 0:
            return parsed
        print(f"{label} must be greater than zero.", file=sys.stderr)


@dataclass(frozen=True)
class ReleaseInputs:
    version: str
    build_number: int
    channel: str
    policy_sequence: int
    rollout: str
    severity: str
    mandatory: str
    required_after: str
    minimum_supported_version: str
    summary_en: str
    summary_ru: str
    change_en: str
    change_ru: str


def collect_inputs(args: argparse.Namespace, policies: dict[str, dict | None]) -> ReleaseInputs:
    channel = (args.channel or "").upper()
    if not channel:
        channel = prompt_choice("Channel", ("STABLE", "BETA"), "STABLE")
    if channel not in {"STABLE", "BETA"}:
        raise ReleaseError("channel must be STABLE or BETA")

    suggested_build = current_build_number(policies.values()) + 1
    suggested_sequence = current_sequence(policies[channel]) + 1

    version = args.version or prompt_text("Version (SemVer, without v)")
    build_number = args.build_number or prompt_int("Android build number", suggested_build)
    policy_sequence = args.policy_sequence or prompt_int("Policy sequence", suggested_sequence)
    rollout = str(args.rollout or prompt_choice("Initial rollout %", ("0", "5", "25", "50", "100"), "5"))
    severity = (args.severity or prompt_choice(
        "Severity", ("NORMAL", "IMPORTANT", "SECURITY", "CRITICAL"), "NORMAL"
    )).upper()
    mandatory = (args.mandatory or prompt_choice(
        "Mandatory policy", ("OPTIONAL", "REQUIRED_AFTER", "UNSUPPORTED_CLIENT"), "OPTIONAL"
    )).upper()
    required_after = args.required_after or ""
    if mandatory == "REQUIRED_AFTER" and not required_after:
        required_after = prompt_text("Required after (ISO UTC, e.g. 2026-10-10T12:00:00Z)")
    minimum_supported_version = args.minimum_supported_version or ""
    if args.minimum_supported_version is None and sys.stdin.isatty():
        minimum_supported_version = prompt_text(
            "Minimum supported version (optional)", required=False
        )

    summary_en = args.summary_en or prompt_text("Release summary (EN)")
    summary_ru = args.summary_ru or prompt_text("Release summary (RU)")
    change_en = args.change_en or prompt_text("Primary change (EN)")
    change_ru = args.change_ru or prompt_text("Primary change (RU)")

    if build_number <= current_build_number(policies.values()):
        raise ReleaseError("build number must be greater than every published channel build number")
    if policy_sequence <= current_sequence(policies[channel]):
        raise ReleaseError(f"policy sequence must be greater than the current {channel} sequence")
    if rollout not in {"0", "5", "25", "50", "100"}:
        raise ReleaseError("rollout must be one of 0, 5, 25, 50, 100")
    if severity not in {"NORMAL", "IMPORTANT", "SECURITY", "CRITICAL"}:
        raise ReleaseError("invalid severity")
    if mandatory not in {"OPTIONAL", "REQUIRED_AFTER", "UNSUPPORTED_CLIENT"}:
        raise ReleaseError("invalid mandatory policy")
    if mandatory == "REQUIRED_AFTER" and not required_after:
        raise ReleaseError("required_after is required for REQUIRED_AFTER")

    return ReleaseInputs(
        version=version,
        build_number=build_number,
        channel=channel,
        policy_sequence=policy_sequence,
        rollout=rollout,
        severity=severity,
        mandatory=mandatory,
        required_after=required_after,
        minimum_supported_version=minimum_supported_version,
        summary_en=summary_en,
        summary_ru=summary_ru,
        change_en=change_en,
        change_ru=change_ru,
    )


def release_exists(repo: str, version: str) -> bool:
    result = gh("api", f"repos/{repo}/releases/tags/v{version}", check=False)
    if result.returncode == 0:
        return True
    if "404" in result.stderr or "Not Found" in result.stderr:
        return False
    raise ReleaseError(result.stderr.strip() or f"cannot check release v{version}")


def dispatch_args(repo: str, source_sha: str, dispatch_id: str, values: ReleaseInputs) -> list[str]:
    fields = {
        "source_sha": source_sha,
        "dispatch_id": dispatch_id,
        "version": values.version,
        "build_number": str(values.build_number),
        "channel": values.channel,
        "policy_sequence": str(values.policy_sequence),
        "rollout": values.rollout,
        "severity": values.severity,
        "mandatory": values.mandatory,
        "required_after": values.required_after,
        "minimum_supported_version": values.minimum_supported_version,
        "summary_en": values.summary_en,
        "summary_ru": values.summary_ru,
        "change_en": values.change_en,
        "change_ru": values.change_ru,
    }
    command = ["workflow", "run", WORKFLOW, "--repo", repo, "--ref", RELEASE_BRANCH]
    for key, value in fields.items():
        command.extend(("-f", f"{key}={value}"))
    return command


def list_workflow_runs(repo: str) -> list[dict]:
    result = gh(
        "api",
        f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=50",
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseError("GitHub returned invalid workflow-runs JSON") from exc
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise ReleaseError("GitHub workflow-runs response has no workflow_runs list")
    return runs


def wait_for_dispatched_run(repo: str, dispatch_id: str, timeout_seconds: int = 90) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for run in list_workflow_runs(repo):
            if dispatch_id in str(run.get("display_title", "")):
                return run
        time.sleep(2)
    raise ReleaseError("workflow dispatch succeeded but the correlated run did not appear")


def verify_release(repo: str, source_sha: str, values: ReleaseInputs) -> str:
    release_result = gh("api", f"repos/{repo}/releases/tags/v{values.version}")
    try:
        release = json.loads(release_result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseError("GitHub returned invalid release JSON") from exc

    if release.get("target_commitish") != source_sha:
        raise ReleaseError(
            f"release target mismatch: expected {source_sha}, got {release.get('target_commitish')}"
        )
    expected_apk = f"student-execution-os-{values.version}-android-universal.apk"
    assets = {item.get("name") for item in release.get("assets", [])}
    if expected_apk not in assets or "provenance.json" not in assets:
        raise ReleaseError("GitHub Release is missing the APK or provenance.json")

    policy = load_policy(repo, values.channel)
    if not policy:
        raise ReleaseError(f"update-{values.channel.lower()} policy was not published")
    if policy.get("latest_version") != values.version:
        raise ReleaseError("published update policy points to a different version")
    if policy.get("sequence") != values.policy_sequence:
        raise ReleaseError("published update policy has a different sequence")
    releases = policy.get("releases", [])
    latest = next((item for item in releases if item.get("version") == values.version), None)
    if not latest or latest.get("build_number") != values.build_number:
        raise ReleaseError("published update policy has a different build number")

    return str(release.get("html_url") or f"https://github.com/{repo}/releases/tag/v{values.version}")


def preflight() -> tuple[str, str]:
    require_tool("git")
    require_tool("gh")
    auth = gh("auth", "status", "--hostname", "github.com", check=False)
    if auth.returncode != 0:
        raise ReleaseError("GitHub CLI is not authenticated; run: gh auth login")

    branch = git("branch", "--show-current")
    if branch != RELEASE_BRANCH:
        raise ReleaseError(f"production release must be dispatched from {RELEASE_BRANCH}, not {branch or 'detached HEAD'}")
    dirty = git("status", "--porcelain")
    if dirty:
        raise ReleaseError(f"working tree is not clean:\n{dirty}")

    repo = github_repo_from_remote(git("remote", "get-url", "origin"))
    gh("repo", "view", repo, "--json", "nameWithOwner")
    git("fetch", "--quiet", "origin", RELEASE_BRANCH)
    source_sha = git("rev-parse", "HEAD")
    remote_sha = git("rev-parse", f"origin/{RELEASE_BRANCH}")
    if source_sha != remote_sha:
        raise ReleaseError(
            f"local HEAD is not origin/{RELEASE_BRANCH}\nlocal:  {source_sha}\nremote: {remote_sha}\n"
            "Commit/push/pull before releasing."
        )
    return repo, source_sha


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--version")
    result.add_argument("--build-number", type=int)
    result.add_argument("--channel", choices=("STABLE", "BETA"))
    result.add_argument("--policy-sequence", type=int)
    result.add_argument("--rollout", choices=("0", "5", "25", "50", "100"))
    result.add_argument("--severity", choices=("NORMAL", "IMPORTANT", "SECURITY", "CRITICAL"))
    result.add_argument("--mandatory", choices=("OPTIONAL", "REQUIRED_AFTER", "UNSUPPORTED_CLIENT"))
    result.add_argument("--required-after")
    result.add_argument("--minimum-supported-version")
    result.add_argument("--summary-en")
    result.add_argument("--summary-ru")
    result.add_argument("--change-en")
    result.add_argument("--change-ru")
    result.add_argument("--yes", action="store_true", help="dispatch without the final confirmation prompt")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        repo, source_sha = preflight()
        policies = {
            "STABLE": load_policy(repo, "STABLE"),
            "BETA": load_policy(repo, "BETA"),
        }
        values = collect_inputs(args, policies)
        if release_exists(repo, values.version):
            raise ReleaseError(f"immutable GitHub Release v{values.version} already exists")

        dispatch_id = uuid.uuid4().hex
        print("\nProduction Android release")
        print(f"  repository:      {repo}")
        print(f"  source SHA:      {source_sha}")
        print(f"  version:         {values.version}")
        print(f"  build number:    {values.build_number}")
        print(f"  channel:         {values.channel}")
        print(f"  policy sequence: {values.policy_sequence}")
        print(f"  rollout:         {values.rollout}%")
        if not args.yes:
            answer = prompt_text("Dispatch this production release? (yes/no)", default="no")
            if answer.lower() not in {"y", "yes"}:
                print("Release cancelled.")
                return 0

        gh(*dispatch_args(repo, source_sha, dispatch_id, values))
        run = wait_for_dispatched_run(repo, dispatch_id)
        run_id = str(run.get("id"))
        run_url = str(run.get("html_url") or "")
        if run.get("head_sha") != source_sha:
            # The workflow has its own source_sha guard too; watch it to get the canonical failure.
            print(f"GitHub resolved main to {run.get('head_sha')}, expected {source_sha}; workflow will fail closed.")
        print(f"Workflow: {run_url or run_id}")
        watched = run_command(
            ("gh", "run", "watch", run_id, "--repo", repo, "--exit-status", "--interval", "5"),
            check=False,
            capture=False,
        )
        if watched.returncode != 0:
            raise ReleaseError(f"android release workflow failed: {run_url or run_id}")

        release_url = verify_release(repo, source_sha, values)
        print(f"Release verified: {release_url}")
        return 0
    except ReleaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nRelease cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
