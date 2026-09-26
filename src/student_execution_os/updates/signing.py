from __future__ import annotations

import base64
from dataclasses import replace
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .model import SignatureMetadata, UpdatePolicy


class PolicySignatureError(ValueError):
    pass


def _b64decode(value: str, what: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise PolicySignatureError(f"invalid base64 {what}") from exc


def load_private_key(value: bytes | str) -> Ed25519PrivateKey:
    raw = value.encode() if isinstance(value, str) else value
    if raw.startswith(b"-----BEGIN"):
        key = serialization.load_pem_private_key(raw, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise PolicySignatureError("signing key is not Ed25519")
        return key
    decoded = _b64decode(raw.decode().strip(), "private key")
    if len(decoded) != 32:
        raise PolicySignatureError("Ed25519 private key seed must be 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(decoded)


def load_public_key(value: str) -> Ed25519PublicKey:
    raw = _b64decode(value, "public key")
    if len(raw) != 32:
        raise PolicySignatureError("Ed25519 public key must be 32 bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def public_key_b64(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def private_seed_b64(key: Ed25519PrivateKey) -> str:
    raw = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    return base64.b64encode(raw).decode("ascii")


def sign_policy(policy: UpdatePolicy, private_key: Ed25519PrivateKey) -> UpdatePolicy:
    unsigned = replace(
        policy,
        signature_metadata=SignatureMetadata(policy.signature_metadata.key_id, "Ed25519", None),
    )
    signature = base64.b64encode(private_key.sign(unsigned.canonical_bytes())).decode("ascii")
    return replace(unsigned, signature_metadata=replace(unsigned.signature_metadata, signature=signature))


def verify_policy(policy: UpdatePolicy, trusted_keys: Mapping[str, str]) -> None:
    metadata = policy.signature_metadata
    encoded = trusted_keys.get(metadata.key_id)
    if encoded is None:
        raise PolicySignatureError(f"untrusted update key id: {metadata.key_id}")
    if metadata.algorithm != "Ed25519" or not metadata.signature:
        raise PolicySignatureError("unsupported or missing policy signature")
    signature = _b64decode(metadata.signature, "signature")
    if len(signature) != 64:
        raise PolicySignatureError("Ed25519 signature must be 64 bytes")
    try:
        load_public_key(encoded).verify(signature, policy.canonical_bytes())
    except InvalidSignature as exc:
        raise PolicySignatureError("invalid update policy signature") from exc
