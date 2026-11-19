"""Signing keys and base64 helpers (extra ``pytools[crypto]``).

* :func:`load_private_key` reads an **Ed25519** or **RSA** PEM private key. Any other
  type is refused with an error that names it.
* :func:`sign` signs with Ed25519 (RFC 8032) or RSA-PSS (SHA-256, MGF1(SHA-256)).
* Standard and URL-safe base64 helpers with strict decoding.

Callers build their own pre-sign message; venue-specific formats live in the consuming
app. No error message or log record from this module contains key bytes, PEM text, the
password or a signature.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from pytools.errors import AppError

__all__ = [
    "MIN_RSA_KEY_BITS",
    "CryptoError",
    "EncodingError",
    "InvalidKeyError",
    "KeyFileError",
    "KeyPasswordError",
    "KeyType",
    "PrivateKey",
    "PssSaltLength",
    "SigningError",
    "UnsupportedKeyTypeError",
    "b64decode",
    "b64encode",
    "load_private_key",
    "sign",
    "urlsafe_b64decode",
    "urlsafe_b64encode",
]

_log = logging.getLogger(__name__)

_ENCRYPTED_MARKER = b"ENCRYPTED"

MIN_RSA_KEY_BITS = 2048
"""Smallest RSA modulus :func:`load_private_key` accepts."""

PssSaltLength = int | object
"""A non-negative int, ``padding.PSS.DIGEST_LENGTH`` or ``padding.PSS.MAX_LENGTH``.

The two sentinels have no public type, so the alias is ``object``-wide; :func:`sign`
validates by identity at runtime.
"""

_PEM_BLOCK_RE = re.compile(rb"\s*-----BEGIN [^-\r\n]+-----.*-----END [^-\r\n]+-----\s*", re.DOTALL)
_PEM_BEGIN = b"-----BEGIN "


class CryptoError(AppError):
    """Root of every error raised by :mod:`pytools.crypto`."""


class KeyFileError(CryptoError):
    """The key file is missing or unreadable."""


class InvalidKeyError(CryptoError):
    """The key source is not a usable PEM private key (not PEM, malformed, or a password
    was given for an unencrypted key)."""


class KeyPasswordError(CryptoError):
    """The key is encrypted and the password is missing or wrong."""


class UnsupportedKeyTypeError(CryptoError):
    """A valid private key of a type other than Ed25519 or RSA."""


class SigningError(CryptoError):
    """Signing arguments are invalid or the signing operation failed."""


class EncodingError(CryptoError):
    """Input is not valid base64 for the requested alphabet."""


class KeyType(Enum):
    """Supported private key algorithms."""

    ED25519 = "Ed25519"
    RSA = "RSA"


@dataclass(frozen=True)
class PrivateKey:
    """A loaded private key. Its repr and str never reveal key material."""

    key_type: KeyType
    _key: ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey = field(repr=False)

    @property
    def key_size(self) -> int | None:
        """RSA modulus size in bits; ``None`` for Ed25519."""
        if isinstance(self._key, rsa.RSAPrivateKey):
            return self._key.key_size
        return None

    def public_key_pem(self) -> bytes:
        """The matching public key as SubjectPublicKeyInfo PEM."""
        return self._key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def __repr__(self) -> str:
        if self.key_size is not None:
            return f"PrivateKey({self.key_type.value}, {self.key_size} bits)"
        return f"PrivateKey({self.key_type.value})"

    __str__ = __repr__


_UNSUPPORTED_NAMES = {
    "EllipticCurvePrivateKey": "EC",
    "DSAPrivateKey": "DSA",
    "Ed448PrivateKey": "Ed448",
    "X25519PrivateKey": "X25519",
    "X448PrivateKey": "X448",
    "DHPrivateKey": "DH",
}


def _read_source(source: Path | bytes) -> bytes:
    if isinstance(source, Path):
        try:
            return source.read_bytes()
        except FileNotFoundError as exc:
            raise KeyFileError(f"key file not found: {source}") from exc
        except OSError as exc:
            raise KeyFileError(f"key file unreadable: {source} ({type(exc).__name__})") from exc
    if isinstance(source, bytes):
        return source
    if isinstance(source, str):
        raise TypeError(
            "ambiguous key source: a str could be a path or PEM text; "
            "pass Path(...) to read a file or pem.encode() for PEM content"
        )
    raise TypeError(f"key source must be Path or bytes, not {type(source).__name__}")


def _normalise_password(password: bytes | str | None) -> bytes | None:
    if password is None or isinstance(password, bytes):
        return password
    if isinstance(password, str):
        return password.encode("utf-8")
    raise TypeError(f"password must be bytes, str or None, not {type(password).__name__}")


def load_private_key(source: Path | bytes, password: bytes | str | None = None) -> PrivateKey:
    """Load an Ed25519 or RSA PEM private key.

    ``source`` is a :class:`~pathlib.Path` (read the file) or ``bytes`` (PEM content).
    A plain ``str`` is ambiguous and raises ``TypeError``. ``password`` is for encrypted
    keys; ``str`` is encoded as UTF-8.

    Raises:
        KeyFileError: the file is missing or unreadable.
        InvalidKeyError: not PEM, malformed, or a password was given for a plain key.
        KeyPasswordError: encrypted key with no password, or the password is wrong.
        UnsupportedKeyTypeError: a valid key that is neither Ed25519 nor RSA.
    """
    pem = _read_source(source)
    _check_single_pem_block(pem)
    secret = _normalise_password(password)
    encrypted = _ENCRYPTED_MARKER in pem
    if encrypted and secret is None:
        raise KeyPasswordError("private key is encrypted but no password was given")
    try:
        loaded = serialization.load_pem_private_key(pem, password=secret)
    except TypeError as exc:
        # cryptography raises TypeError when a password is given for an unencrypted key.
        raise InvalidKeyError("a password was given but the private key is not encrypted") from exc
    except ValueError as exc:
        if encrypted:
            raise KeyPasswordError(
                "cannot decrypt private key: wrong password or corrupt key data"
            ) from exc
        raise InvalidKeyError("not a valid PEM private key (malformed or not PEM)") from exc
    except UnsupportedAlgorithm as exc:
        raise InvalidKeyError("private key uses an unsupported algorithm or cipher") from exc
    key = _wrap(loaded)
    _log.debug("loaded private key: %s", key)
    return key


def _check_single_pem_block(pem: bytes) -> None:
    """Refuse to guess: exactly one PEM block, with nothing but whitespace around it."""
    blocks = pem.count(_PEM_BEGIN)
    if blocks > 1:
        raise InvalidKeyError(
            f"expected exactly one PEM block but found {blocks}; refusing to pick one"
        )
    if blocks == 0 or not _PEM_BLOCK_RE.fullmatch(pem):
        raise InvalidKeyError(
            "not a valid PEM private key (not exactly one PEM block, or data outside it)"
        )


def _wrap(loaded: object) -> PrivateKey:
    if isinstance(loaded, ed25519.Ed25519PrivateKey):
        return PrivateKey(KeyType.ED25519, loaded)
    if isinstance(loaded, rsa.RSAPrivateKey):
        if loaded.key_size < MIN_RSA_KEY_BITS:
            raise InvalidKeyError(
                f"RSA key is {loaded.key_size} bits; the minimum is {MIN_RSA_KEY_BITS}"
            )
        return PrivateKey(KeyType.RSA, loaded)
    raw = type(loaded).__name__
    name = _UNSUPPORTED_NAMES.get(raw, raw)
    raise UnsupportedKeyTypeError(
        f"unsupported private key type {name}; only Ed25519 and RSA are supported"
    )


def _validate_salt(salt_length: object) -> int | object:
    if isinstance(salt_length, bool):
        raise SigningError("pss_salt_length must be an int or a PSS length sentinel, not bool")
    if isinstance(salt_length, int):
        if salt_length < 0:
            raise SigningError("pss_salt_length must be >= 0")
        return salt_length
    if salt_length is padding.PSS.DIGEST_LENGTH or salt_length is padding.PSS.MAX_LENGTH:
        return salt_length
    raise SigningError(
        "pss_salt_length must be an int, PSS.DIGEST_LENGTH or PSS.MAX_LENGTH "
        "(PSS.AUTO is for verification only)"
    )


def sign(key: PrivateKey, message: bytes, *, pss_salt_length: PssSaltLength | None = None) -> bytes:
    """Sign ``message`` and return the raw signature bytes.

    Ed25519 follows RFC 8032 (deterministic). RSA uses PSS with SHA-256 and
    MGF1(SHA-256); the salt length defaults to the digest length (32 bytes) and is
    overridable with ``pss_salt_length``. Passing ``pss_salt_length`` with an Ed25519 key
    is rejected (it has no salt), rather than silently ignored.

    Raises:
        TypeError: ``key`` is not a :class:`PrivateKey` or ``message`` is not bytes.
        SigningError: bad ``pss_salt_length`` or the signing operation failed.
    """
    if not isinstance(key, PrivateKey):
        raise TypeError(f"key must be a PrivateKey, not {type(key).__name__}")
    if not isinstance(message, bytes):
        raise TypeError(f"message must be bytes, not {type(message).__name__}")
    raw = key._key
    if isinstance(raw, ed25519.Ed25519PrivateKey):
        if pss_salt_length is not None:
            raise SigningError("pss_salt_length is not applicable to an Ed25519 key")
        return raw.sign(message)
    salt = padding.PSS.DIGEST_LENGTH if pss_salt_length is None else _validate_salt(pss_salt_length)
    pss = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=salt)  # type: ignore[arg-type]
    try:
        return raw.sign(message, pss, hashes.SHA256())
    except ValueError as exc:
        # e.g. salt length too large for the key size.
        raise SigningError("RSA-PSS signing failed (salt length too large for key?)") from exc


_B64_RE = re.compile(rb"[A-Za-z0-9+/]*={0,2}")
_B64URL_RE = re.compile(rb"[A-Za-z0-9_-]*={0,2}")


def _ascii(data: str | bytes) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        try:
            return data.encode("ascii")
        except UnicodeEncodeError as exc:
            raise EncodingError("base64 input contains non-ASCII characters") from exc
    raise TypeError(f"base64 input must be str or bytes, not {type(data).__name__}")


def b64encode(data: bytes) -> str:
    """Standard base64 (``+/``, padded) as ``str``."""
    return base64.b64encode(data).decode("ascii")


def b64decode(text: str | bytes) -> bytes:
    """Strict standard base64 decode: alphabet, padding and whitespace are all checked."""
    raw = _ascii(text)
    if not _B64_RE.fullmatch(raw):
        raise EncodingError("invalid base64 input")
    try:
        out = base64.b64decode(raw, validate=True)
    except binascii.Error as exc:
        raise EncodingError("invalid base64 input (bad length or padding)") from exc
    if base64.b64encode(out) != raw:
        raise EncodingError("non-canonical base64 input (nonzero trailing bits)")
    return out


def urlsafe_b64encode(data: bytes, *, padded: bool = True) -> str:
    """URL-safe base64 (``-_``) as ``str``; ``padded=False`` strips trailing ``=``."""
    out = base64.urlsafe_b64encode(data).decode("ascii")
    return out if padded else out.rstrip("=")


def urlsafe_b64decode(text: str | bytes) -> bytes:
    """Strict URL-safe base64 decode. Padding is optional; if present it must be correct.

    Whitespace, the standard ``+/`` alphabet and any other character are rejected.
    """
    raw = _ascii(text)
    if not _B64URL_RE.fullmatch(raw):
        raise EncodingError("invalid URL-safe base64 input")
    body = raw.rstrip(b"=")
    if b"=" in raw and len(raw) % 4 != 0:
        raise EncodingError("invalid URL-safe base64 input (bad padding)")
    if len(body) % 4 == 1:
        raise EncodingError("invalid URL-safe base64 input (bad length)")
    padded = body + b"=" * (-len(body) % 4)
    try:
        out = base64.b64decode(padded.translate(bytes.maketrans(b"-_", b"+/")), validate=True)
    except binascii.Error as exc:
        raise EncodingError("invalid URL-safe base64 input") from exc
    if base64.urlsafe_b64encode(out).rstrip(b"=") != body:
        raise EncodingError("non-canonical URL-safe base64 input (nonzero trailing bits)")
    return out
