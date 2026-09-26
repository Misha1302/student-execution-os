"""Update-policy domain and release-side trust helpers.

The Android client has a matching ES-module model because it must evaluate policy
without depending on the application API.  This Python package is the canonical
publisher/CI representation and deliberately contains no hosting credentials.
"""

from .model import (
    ArtifactKind,
    MandatoryMode,
    MandatoryPolicy,
    MinimumOsVersions,
    ReleaseNotes,
    ReleaseSeverity,
    ReleaseStatus,
    RollbackCompatibility,
    Rollout,
    SemVer,
    SignatureMetadata,
    UpdateArtifact,
    UpdateChannel,
    UpdatePolicy,
    UpdateRelease,
    UpdaterMetadata,
)
from .signing import PolicySignatureError, public_key_b64, sign_policy, verify_policy

__all__ = [
    "ArtifactKind", "MandatoryMode", "MandatoryPolicy", "MinimumOsVersions",
    "PolicySignatureError", "ReleaseNotes", "ReleaseSeverity", "ReleaseStatus",
    "RollbackCompatibility", "Rollout", "SemVer", "SignatureMetadata",
    "UpdateArtifact", "UpdateChannel", "UpdatePolicy", "UpdateRelease",
    "UpdaterMetadata", "public_key_b64", "sign_policy", "verify_policy",
]
