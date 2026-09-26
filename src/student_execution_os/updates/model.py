from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from functools import total_ordering
from typing import Any, Iterable
from urllib.parse import urlparse


_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _aware(value, "timestamp").isoformat().replace("+00:00", "Z")


def _parse_time(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO timestamp")
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
    except ValueError as exc:
        raise ValueError(f"invalid {name}") from exc


def _integer(value: Any, name: str) -> int:
    if type(value) is not int:  # bool must not pass as an integer in signed metadata
        raise ValueError(f"{name} must be an integer")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


@total_ordering
@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> "SemVer":
        match = _SEMVER.fullmatch(str(value))
        if not match:
            raise ValueError(f"invalid SemVer: {value!r}")
        pre = tuple(match.group(4).split(".")) if match.group(4) else ()
        build = tuple(match.group(5).split(".")) if match.group(5) else ()
        if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in pre):
            raise ValueError(f"numeric prerelease identifiers cannot have leading zeroes: {value!r}")
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)), pre, build)

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            base += "-" + ".".join(self.prerelease)
        if self.build:
            base += "+" + ".".join(self.build)
        return base

    def _compare_pre(self, other: "SemVer") -> int:
        if not self.prerelease and not other.prerelease:
            return 0
        if not self.prerelease:
            return 1
        if not other.prerelease:
            return -1
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            left_num, right_num = left.isdigit(), right.isdigit()
            if left_num and right_num:
                return -1 if int(left) < int(right) else 1
            if left_num != right_num:
                return -1 if left_num else 1
            return -1 if left < right else 1
        return (len(self.prerelease) > len(other.prerelease)) - (len(self.prerelease) < len(other.prerelease))

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        core = (self.major, self.minor, self.patch)
        other_core = (other.major, other.minor, other.patch)
        return core < other_core if core != other_core else self._compare_pre(other) < 0

    def __eq__(self, other: object) -> bool:
        # SemVer build metadata does not participate in precedence.
        return isinstance(other, SemVer) and (
            self.major, self.minor, self.patch, self.prerelease
        ) == (other.major, other.minor, other.patch, other.prerelease)

    def __hash__(self) -> int:
        # Equal SemVer precedence values must also have equal hashes; build
        # metadata is deliberately excluded for the same reason as __eq__.
        return hash((self.major, self.minor, self.patch, self.prerelease))


class UpdateChannel(StrEnum):
    STABLE = "STABLE"
    BETA = "BETA"


class ReleaseStatus(StrEnum):
    DRAFT = "DRAFT"
    AVAILABLE = "AVAILABLE"
    PAUSED = "PAUSED"
    WITHDRAWN = "WITHDRAWN"


class ReleaseSeverity(StrEnum):
    NORMAL = "NORMAL"
    IMPORTANT = "IMPORTANT"
    SECURITY = "SECURITY"
    CRITICAL = "CRITICAL"


class MandatoryMode(StrEnum):
    OPTIONAL = "OPTIONAL"
    REQUIRED_AFTER = "REQUIRED_AFTER"
    UNSUPPORTED_CLIENT = "UNSUPPORTED_CLIENT"


class ArtifactKind(StrEnum):
    APK = "APK"


class RollbackCompatibility(StrEnum):
    FULL = "FULL"
    BINARY_ONLY = "BINARY_ONLY"
    NOT_SUPPORTED = "NOT_SUPPORTED"


@dataclass(frozen=True)
class Rollout:
    percentage: int

    def __post_init__(self) -> None:
        if type(self.percentage) is not int or not 0 <= self.percentage <= 100:
            raise ValueError("rollout percentage must be an integer from 0 to 100")

    def eligible(self, installation_id: str, release_id: str) -> bool:
        if not installation_id or not release_id:
            raise ValueError("rollout identities cannot be empty")
        # FNV-1a is a cohort hash, not a security primitive. It is specified here
        # to keep publisher tests and the zero-build JavaScript client identical.
        bucket_hash = 0x811C9DC5
        for byte in f"{installation_id}\n{release_id}".encode():
            bucket_hash ^= byte
            bucket_hash = (bucket_hash * 0x01000193) & 0xFFFFFFFF
        return bucket_hash % 10_000 < self.percentage * 100

    def to_dict(self) -> dict[str, Any]:
        return {"percentage": self.percentage}

    @classmethod
    def from_dict(cls, raw: Any) -> "Rollout":
        if not isinstance(raw, dict) or set(raw) != {"percentage"}:
            raise ValueError("rollout must contain exactly percentage")
        return cls(raw["percentage"])


@dataclass(frozen=True)
class MandatoryPolicy:
    mode: MandatoryMode = MandatoryMode.OPTIONAL
    required_after: datetime | None = None

    def __post_init__(self) -> None:
        if self.mode is MandatoryMode.REQUIRED_AFTER:
            if self.required_after is None:
                raise ValueError("REQUIRED_AFTER needs required_after")
            object.__setattr__(self, "required_after", _aware(self.required_after, "required_after"))
        elif self.required_after is not None:
            raise ValueError("required_after is only valid for REQUIRED_AFTER")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mode": self.mode.value}
        if self.required_after is not None:
            out["required_after"] = _iso(self.required_after)
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> "MandatoryPolicy":
        if not isinstance(raw, dict):
            raise ValueError("mandatory_policy must be an object")
        allowed = {"mode", "required_after"}
        if set(raw) - allowed or "mode" not in raw:
            raise ValueError("invalid mandatory_policy fields")
        mode = MandatoryMode(raw["mode"])
        required = _parse_time(raw["required_after"], "required_after") if "required_after" in raw else None
        return cls(mode, required)


@dataclass(frozen=True)
class ReleaseNotes:
    summary: str
    changes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.summary.strip() or len(self.summary) > 500:
            raise ValueError("release-note summary must be 1..500 characters")
        if not self.changes or any(not item.strip() or len(item) > 500 for item in self.changes):
            raise ValueError("release notes need non-empty changes")

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "changes": list(self.changes)}

    @classmethod
    def from_dict(cls, raw: Any) -> "ReleaseNotes":
        if (
            not isinstance(raw, dict)
            or set(raw) != {"summary", "changes"}
            or not isinstance(raw["summary"], str)
            or not isinstance(raw["changes"], list)
            or any(not isinstance(item, str) for item in raw["changes"])
        ):
            raise ValueError("invalid localized release notes")
        return cls(raw["summary"], tuple(raw["changes"]))


@dataclass(frozen=True)
class MinimumOsVersions:
    android_sdk: int | None = None

    def __post_init__(self) -> None:
        if self.android_sdk is not None and self.android_sdk < 24:
            raise ValueError("android_sdk cannot be below the application's minSdk 24")

    def to_dict(self) -> dict[str, Any]:
        return {} if self.android_sdk is None else {"android_sdk": self.android_sdk}

    @classmethod
    def from_dict(cls, raw: Any) -> "MinimumOsVersions":
        if not isinstance(raw, dict) or set(raw) - {"android_sdk"}:
            raise ValueError("invalid minimum_os_versions")
        return cls(_integer(raw["android_sdk"], "android_sdk") if "android_sdk" in raw else None)


@dataclass(frozen=True)
class UpdaterMetadata:
    package_name: str
    version_code: int
    min_sdk: int = 24

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", self.package_name):
            raise ValueError("invalid Android package name")
        if self.version_code <= 0 or self.min_sdk < 24:
            raise ValueError("invalid updater metadata")

    def to_dict(self) -> dict[str, Any]:
        return {"package_name": self.package_name, "version_code": self.version_code, "min_sdk": self.min_sdk}

    @classmethod
    def from_dict(cls, raw: Any) -> "UpdaterMetadata":
        if not isinstance(raw, dict) or set(raw) != {"package_name", "version_code", "min_sdk"}:
            raise ValueError("invalid updater_metadata")
        return cls(
            _string(raw["package_name"], "package_name"),
            _integer(raw["version_code"], "version_code"),
            _integer(raw["min_sdk"], "min_sdk"),
        )


@dataclass(frozen=True)
class UpdateArtifact:
    platform: str
    architecture: str
    artifact_kind: ArtifactKind
    url: str
    size_bytes: int
    sha256: str
    updater_metadata: UpdaterMetadata

    def __post_init__(self) -> None:
        if self.platform != "android" or self.architecture not in {"universal", "arm64", "x64"}:
            raise ValueError("unsupported artifact platform/architecture")
        parsed = urlparse(self.url)
        local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "10.0.2.2"}
        if (parsed.scheme != "https" and not local_http) or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("artifact URL must be public HTTPS (or an explicit local test host) without credentials or fragment")
        if not 0 < self.size_bytes <= 500 * 1024 * 1024:
            raise ValueError("artifact size is outside the 500 MiB safety bound")
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("sha256 must be lowercase hexadecimal")

    @property
    def identity(self) -> str:
        return f"{self.platform}-{self.architecture}-{self.artifact_kind.value.lower()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform, "architecture": self.architecture,
            "artifact_kind": self.artifact_kind.value, "url": self.url,
            "size_bytes": self.size_bytes, "sha256": self.sha256,
            "updater_metadata": self.updater_metadata.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "UpdateArtifact":
        required = {"platform", "architecture", "artifact_kind", "url", "size_bytes", "sha256", "updater_metadata"}
        if not isinstance(raw, dict) or set(raw) != required:
            raise ValueError("invalid artifact fields")
        return cls(
            _string(raw["platform"], "platform"), _string(raw["architecture"], "architecture"),
            ArtifactKind(raw["artifact_kind"]), _string(raw["url"], "url"),
            _integer(raw["size_bytes"], "size_bytes"), _string(raw["sha256"], "sha256"),
            UpdaterMetadata.from_dict(raw["updater_metadata"]),
        )


@dataclass(frozen=True)
class UpdateRelease:
    version: SemVer
    build_number: int
    channel: UpdateChannel
    published_at: datetime
    status: ReleaseStatus
    severity: ReleaseSeverity
    release_notes: dict[str, ReleaseNotes]
    minimum_os_versions: MinimumOsVersions
    minimum_api_version: int | None
    mandatory_policy: MandatoryPolicy
    rollback_compatibility: RollbackCompatibility
    artifacts: tuple[UpdateArtifact, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "published_at", _aware(self.published_at, "published_at"))
        if self.build_number <= 0:
            raise ValueError("build_number must be positive")
        if self.channel is UpdateChannel.STABLE and self.version.is_prerelease:
            raise ValueError("stable releases cannot be prereleases")
        if (
            not self.release_notes
            or any(not isinstance(key, str) for key in self.release_notes)
            or not set(self.release_notes).issubset({"en", "ru"})
        ):
            raise ValueError("release notes must use en/ru locale keys")
        if self.minimum_api_version is not None and self.minimum_api_version <= 0:
            raise ValueError("minimum_api_version must be positive")
        if not self.artifacts:
            raise ValueError("release needs at least one artifact")
        identities = [artifact.identity for artifact in self.artifacts]
        if len(identities) != len(set(identities)):
            raise ValueError("artifact identities must be unique within a release")
        for artifact in self.artifacts:
            if artifact.updater_metadata.version_code != self.build_number:
                raise ValueError("artifact version_code must equal release build_number")

    @property
    def release_id(self) -> str:
        return f"{self.channel.value}:{self.version}:{self.build_number}"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": str(self.version), "build_number": self.build_number,
            "channel": self.channel.value, "published_at": _iso(self.published_at),
            "status": self.status.value, "severity": self.severity.value,
            "release_notes": {key: value.to_dict() for key, value in sorted(self.release_notes.items())},
            "minimum_os_versions": self.minimum_os_versions.to_dict(),
            "mandatory_policy": self.mandatory_policy.to_dict(),
            "rollback_compatibility": self.rollback_compatibility.value,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }
        if self.minimum_api_version is not None:
            out["minimum_api_version"] = self.minimum_api_version
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> "UpdateRelease":
        required = {
            "version", "build_number", "channel", "published_at", "status", "severity", "release_notes",
            "minimum_os_versions", "mandatory_policy", "rollback_compatibility", "artifacts",
        }
        allowed = required | {"minimum_api_version"}
        if not isinstance(raw, dict) or not required.issubset(raw) or set(raw) - allowed:
            raise ValueError("invalid release fields")
        notes = raw["release_notes"]
        if (
            not isinstance(notes, dict)
            or any(not isinstance(key, str) for key in notes)
            or not isinstance(raw["artifacts"], list)
        ):
            raise ValueError("invalid release notes/artifacts")
        return cls(
            SemVer.parse(_string(raw["version"], "version")),
            _integer(raw["build_number"], "build_number"), UpdateChannel(raw["channel"]),
            _parse_time(raw["published_at"], "published_at"), ReleaseStatus(raw["status"]),
            ReleaseSeverity(raw["severity"]), {k: ReleaseNotes.from_dict(v) for k, v in notes.items()},
            MinimumOsVersions.from_dict(raw["minimum_os_versions"]),
            _integer(raw["minimum_api_version"], "minimum_api_version") if "minimum_api_version" in raw else None,
            MandatoryPolicy.from_dict(raw["mandatory_policy"]),
            RollbackCompatibility(raw["rollback_compatibility"]),
            tuple(UpdateArtifact.from_dict(item) for item in raw["artifacts"]),
        )


@dataclass(frozen=True)
class SignatureMetadata:
    key_id: str
    algorithm: str = "Ed25519"
    signature: str | None = None

    def __post_init__(self) -> None:
        if not _KEY_ID.fullmatch(self.key_id) or self.algorithm != "Ed25519":
            raise ValueError("invalid signature metadata")

    def to_dict(self, *, include_signature: bool) -> dict[str, Any]:
        out = {"key_id": self.key_id, "algorithm": self.algorithm}
        if include_signature:
            if not self.signature:
                raise ValueError("signed policy is missing its signature")
            out["signature"] = self.signature
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> "SignatureMetadata":
        if not isinstance(raw, dict) or set(raw) != {"key_id", "algorithm", "signature"}:
            raise ValueError("invalid signature_metadata fields")
        return cls(
            _string(raw["key_id"], "key_id"), _string(raw["algorithm"], "algorithm"),
            _string(raw["signature"], "signature"),
        )


@dataclass(frozen=True)
class UpdatePolicy:
    schema_version: int
    sequence: int
    generated_at: datetime
    expires_at: datetime
    channel: UpdateChannel
    latest_version: SemVer
    minimum_supported_version: SemVer | None
    rollout: Rollout
    releases: tuple[UpdateRelease, ...]
    signature_metadata: SignatureMetadata

    def __post_init__(self) -> None:
        generated = _aware(self.generated_at, "generated_at")
        expires = _aware(self.expires_at, "expires_at")
        object.__setattr__(self, "generated_at", generated)
        object.__setattr__(self, "expires_at", expires)
        if self.schema_version != 1 or self.sequence < 1 or expires <= generated:
            raise ValueError("invalid policy schema, sequence or validity interval")
        if not self.releases:
            raise ValueError("policy needs releases")
        identities: set[tuple[str, int]] = set()
        latest_matches = 0
        for release in self.releases:
            if release.channel is not self.channel:
                raise ValueError("release channel differs from policy channel")
            identity = (str(release.version), release.build_number)
            if identity in identities:
                raise ValueError("release identity must be unique")
            identities.add(identity)
            if str(release.version) == str(self.latest_version):
                latest_matches += 1
        if latest_matches != 1:
            raise ValueError("latest_version must identify exactly one release")
        if self.channel is UpdateChannel.STABLE and self.latest_version.is_prerelease:
            raise ValueError("stable policy cannot target a prerelease")

    def to_dict(self, *, include_signature: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": self.schema_version, "sequence": self.sequence,
            "generated_at": _iso(self.generated_at), "expires_at": _iso(self.expires_at),
            "channel": self.channel.value, "latest_version": str(self.latest_version),
            "rollout": self.rollout.to_dict(),
            "releases": [release.to_dict() for release in self.releases],
            "signature_metadata": self.signature_metadata.to_dict(include_signature=include_signature),
        }
        if self.minimum_supported_version is not None:
            out["minimum_supported_version"] = str(self.minimum_supported_version)
        return out

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(include_signature=False), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")

    def signed_json(self) -> str:
        return json.dumps(
            self.to_dict(include_signature=True), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ) + "\n"

    def latest_release(self) -> UpdateRelease:
        return next(release for release in self.releases if str(release.version) == str(self.latest_version))

    @classmethod
    def from_dict(cls, raw: Any) -> "UpdatePolicy":
        required = {
            "schema_version", "sequence", "generated_at", "expires_at", "channel", "latest_version",
            "rollout", "releases", "signature_metadata",
        }
        allowed = required | {"minimum_supported_version"}
        if not isinstance(raw, dict) or not required.issubset(raw) or set(raw) - allowed:
            raise ValueError("invalid policy fields")
        if not isinstance(raw["releases"], list):
            raise ValueError("releases must be an array")
        return cls(
            _integer(raw["schema_version"], "schema_version"), _integer(raw["sequence"], "sequence"),
            _parse_time(raw["generated_at"], "generated_at"), _parse_time(raw["expires_at"], "expires_at"),
            UpdateChannel(raw["channel"]), SemVer.parse(_string(raw["latest_version"], "latest_version")),
            SemVer.parse(_string(raw["minimum_supported_version"], "minimum_supported_version"))
            if "minimum_supported_version" in raw else None,
            Rollout.from_dict(raw["rollout"]), tuple(UpdateRelease.from_dict(item) for item in raw["releases"]),
            SignatureMetadata.from_dict(raw["signature_metadata"]),
        )

    @classmethod
    def from_json(cls, text: str | bytes) -> "UpdatePolicy":
        return cls.from_dict(json.loads(text))


def ensure_unique_artifact_names(paths: Iterable[str]) -> None:
    names = [str(path).rsplit("/", 1)[-1] for path in paths]
    if len(names) != len(set(names)):
        raise ValueError("artifact names must be unique")
