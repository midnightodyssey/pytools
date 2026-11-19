from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, padding, rsa, x25519

from pytools.crypto import (
    CryptoError,
    EncodingError,
    InvalidKeyError,
    KeyFileError,
    KeyPasswordError,
    KeyType,
    SigningError,
    UnsupportedKeyTypeError,
    b64decode,
    b64encode,
    load_private_key,
    sign,
    urlsafe_b64decode,
    urlsafe_b64encode,
)
from pytools.errors import AppError

# RFC 8032 section 7.1, TEST 1 (empty message).
RFC_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
RFC_SIG = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
    "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)


def _pem(key: object, password: bytes | None = None) -> bytes:
    enc: serialization.KeySerializationEncryption = (
        serialization.BestAvailableEncryption(password)
        if password
        else serialization.NoEncryption()
    )
    return key.private_bytes(  # type: ignore[attr-defined, no-any-return]
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc
    )


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def ed_key() -> ed25519.Ed25519PrivateKey:
    return ed25519.Ed25519PrivateKey.generate()


def _verify_pss(rsa_key: rsa.RSAPrivateKey, sig: bytes, msg: bytes, salt: int) -> None:
    pss = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=salt)
    rsa_key.public_key().verify(sig, msg, pss, hashes.SHA256())


def test_hierarchy() -> None:
    for exc in (KeyFileError, InvalidKeyError, KeyPasswordError, UnsupportedKeyTypeError):
        assert issubclass(exc, CryptoError)
    assert issubclass(CryptoError, AppError)


# --- Ed25519 ---------------------------------------------------------------------------


def test_ed25519_rfc8032_test1() -> None:
    seed_key = ed25519.Ed25519PrivateKey.from_private_bytes(RFC_SEED)
    key = load_private_key(_pem(seed_key))
    assert key.key_type is KeyType.ED25519
    assert sign(key, b"") == RFC_SIG


def test_ed25519_sign_verifies_and_is_deterministic(ed_key: ed25519.Ed25519PrivateKey) -> None:
    key = load_private_key(_pem(ed_key))
    sig = sign(key, b"hello")
    assert sig == sign(key, b"hello")
    ed_key.public_key().verify(sig, b"hello")
    with pytest.raises(InvalidSignature):
        ed_key.public_key().verify(sig, b"other")


def test_public_key_pem_matches(ed_key: ed25519.Ed25519PrivateKey) -> None:
    key = load_private_key(_pem(ed_key))
    expected = ed_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert key.public_key_pem() == expected


def test_ed25519_rejects_salt_length(ed_key: ed25519.Ed25519PrivateKey) -> None:
    key = load_private_key(_pem(ed_key))
    with pytest.raises(SigningError, match="Ed25519"):
        sign(key, b"m", pss_salt_length=32)


# --- RSA -------------------------------------------------------------------------------


def test_rsa_default_salt_is_digest_length(rsa_key: rsa.RSAPrivateKey) -> None:
    key = load_private_key(_pem(rsa_key))
    assert key.key_type is KeyType.RSA
    assert key.key_size == 2048
    sig = sign(key, b"payload")
    _verify_pss(rsa_key, sig, b"payload", 32)
    with pytest.raises(InvalidSignature):
        _verify_pss(rsa_key, sig, b"payload", 20)


def test_rsa_signatures_are_randomised(rsa_key: rsa.RSAPrivateKey) -> None:
    key = load_private_key(_pem(rsa_key))
    assert sign(key, b"x") != sign(key, b"x")


@pytest.mark.parametrize("salt", [0, 20, 64])
def test_rsa_explicit_salt_length(rsa_key: rsa.RSAPrivateKey, salt: int) -> None:
    key = load_private_key(_pem(rsa_key))
    sig = sign(key, b"payload", pss_salt_length=salt)
    _verify_pss(rsa_key, sig, b"payload", salt)
    with pytest.raises(InvalidSignature):
        _verify_pss(rsa_key, sig, b"payload", 32)


def test_rsa_max_length_sentinel(rsa_key: rsa.RSAPrivateKey) -> None:
    key = load_private_key(_pem(rsa_key))
    sig = sign(key, b"p", pss_salt_length=padding.PSS.MAX_LENGTH)
    rsa_key.public_key().verify(
        sig,
        b"p",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )


@pytest.mark.parametrize("bad", [-1, True, 1.5, padding.PSS.AUTO])
def test_rsa_rejects_bad_salt(rsa_key: rsa.RSAPrivateKey, bad: object) -> None:
    key = load_private_key(_pem(rsa_key))
    with pytest.raises(SigningError):
        sign(key, b"m", pss_salt_length=bad)  # type: ignore[arg-type]


def test_rsa_salt_too_large_is_typed(rsa_key: rsa.RSAPrivateKey) -> None:
    key = load_private_key(_pem(rsa_key))
    with pytest.raises(SigningError):
        sign(key, b"m", pss_salt_length=10_000)


def test_sign_rejects_non_bytes_message(ed_key: ed25519.Ed25519PrivateKey) -> None:
    key = load_private_key(_pem(ed_key))
    with pytest.raises(TypeError, match="message"):
        sign(key, "text")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="message"):
        sign(key, bytearray(b"x"))  # type: ignore[arg-type]


def test_sign_rejects_non_key() -> None:
    with pytest.raises(TypeError, match="PrivateKey"):
        sign(object(), b"m")  # type: ignore[arg-type]


# --- loading ---------------------------------------------------------------------------


def test_path_and_bytes_agree(tmp_path: Path, ed_key: ed25519.Ed25519PrivateKey) -> None:
    pem = _pem(ed_key)
    file = tmp_path / "k.pem"
    file.write_bytes(pem)
    assert sign(load_private_key(file), b"m") == sign(load_private_key(pem), b"m")


def test_str_source_is_rejected(tmp_path: Path, ed_key: ed25519.Ed25519PrivateKey) -> None:
    with pytest.raises(TypeError, match=r"Path\(\.\.\.\).*encode"):
        load_private_key(_pem(ed_key).decode())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        load_private_key(str(tmp_path / "k.pem"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Path or bytes"):
        load_private_key(123)  # type: ignore[arg-type]


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(KeyFileError, match="not found"):
        load_private_key(tmp_path / "nope.pem")


def test_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(KeyFileError, match="unreadable"):
        load_private_key(tmp_path)  # a directory


@pytest.mark.parametrize("junk", [b"", b"not a pem", b"-----BEGIN PRIVATE KEY-----\nAAAA\n"])
def test_malformed(junk: bytes) -> None:
    with pytest.raises(InvalidKeyError, match="not a valid PEM"):
        load_private_key(junk)


def test_encrypted_key_password_handling(rsa_key: rsa.RSAPrivateKey) -> None:
    pem = _pem(rsa_key, b"s3cret")
    assert load_private_key(pem, b"s3cret").key_type is KeyType.RSA
    assert load_private_key(pem, "s3cret").key_type is KeyType.RSA
    with pytest.raises(KeyPasswordError, match="no password"):
        load_private_key(pem)
    with pytest.raises(KeyPasswordError, match="wrong password"):
        load_private_key(pem, "wrong")


def test_encrypted_ed25519(ed_key: ed25519.Ed25519PrivateKey) -> None:
    pem = _pem(ed_key, b"pw")
    assert load_private_key(pem, "pw").key_type is KeyType.ED25519


def test_password_for_plain_key_is_rejected(ed_key: ed25519.Ed25519PrivateKey) -> None:
    with pytest.raises(InvalidKeyError, match="not encrypted"):
        load_private_key(_pem(ed_key), "pw")


def test_bad_password_type(ed_key: ed25519.Ed25519PrivateKey) -> None:
    with pytest.raises(TypeError, match="password"):
        load_private_key(_pem(ed_key), 5)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("make", "name"),
    [
        (lambda: ec.generate_private_key(ec.SECP256R1()), "EC"),
        (lambda: ed448.Ed448PrivateKey.generate(), "Ed448"),
        (lambda: x25519.X25519PrivateKey.generate(), "X25519"),
        (lambda: dsa.generate_private_key(key_size=2048), "DSA"),
    ],
)
def test_unsupported_types_are_named(make: Callable[[], object], name: str) -> None:
    with pytest.raises(UnsupportedKeyTypeError, match=name):
        load_private_key(_pem(make()))


# --- repr ------------------------------------------------------------------------------


def test_repr_hides_material(rsa_key: rsa.RSAPrivateKey, ed_key: ed25519.Ed25519PrivateKey) -> None:
    rsa_wrapped = load_private_key(_pem(rsa_key))
    ed_wrapped = load_private_key(_pem(ed_key))
    assert repr(rsa_wrapped) == str(rsa_wrapped) == "PrivateKey(RSA, 2048 bits)"
    assert repr(ed_wrapped) == str(ed_wrapped) == "PrivateKey(Ed25519)"


# --- base64 ----------------------------------------------------------------------------


@pytest.mark.parametrize("data", [b"", b"a", b"ab", b"abc", bytes(range(256))])
def test_base64_round_trips(data: bytes) -> None:
    assert b64decode(b64encode(data)) == data
    assert urlsafe_b64decode(urlsafe_b64encode(data)) == data
    assert urlsafe_b64decode(urlsafe_b64encode(data, padded=False)) == data
    assert b64decode(b64encode(data).encode()) == data


def test_urlsafe_alphabet_and_padding() -> None:
    data = b"\xfb\xff\xfe\xff"
    assert b64encode(data) == "+//+/w=="
    assert urlsafe_b64encode(data) == "-__-_w=="
    assert urlsafe_b64encode(data, padded=False) == "-__-_w"


@pytest.mark.parametrize("bad", ["a", "ab!d", "ab d", "YQ=", "YQ==\n", "é", "=YQ=", "YQ==YQ=="])
def test_strict_standard_decode_fails(bad: str) -> None:
    with pytest.raises(EncodingError):
        b64decode(bad)


@pytest.mark.parametrize("bad", ["a", "ab+d", "ab/d", "ab d", "YQ=", "YQ==\n", "é", "YQ==="])
def test_strict_urlsafe_decode_fails(bad: str) -> None:
    with pytest.raises(EncodingError):
        urlsafe_b64decode(bad)


def test_b64_non_text_input() -> None:
    with pytest.raises(TypeError):
        b64decode(5)  # type: ignore[arg-type]


# --- logging / leakage -----------------------------------------------------------------


def test_no_material_in_logs_or_errors(
    caplog: pytest.LogCaptureFixture, tmp_path: Path, rsa_key: rsa.RSAPrivateKey
) -> None:
    caplog.set_level(logging.DEBUG)
    password = "hunter2-very-secret"
    ed = ed25519.Ed25519PrivateKey.from_private_bytes(RFC_SEED)
    plain_pem = _pem(ed)
    enc_pem = _pem(rsa_key, password.encode())
    ec_pem = _pem(ec.generate_private_key(ec.SECP256R1()))
    errors: list[str] = []

    key = load_private_key(plain_pem)
    rsa_loaded = load_private_key(enc_pem, password)
    sigs = [
        sign(key, b"msg"),
        sign(rsa_loaded, b"msg"),
        sign(rsa_loaded, b"msg", pss_salt_length=0),
    ]

    attempts: list[Callable[[], object]] = [
        lambda: load_private_key(enc_pem),
        lambda: load_private_key(enc_pem, password + "x"),
        lambda: load_private_key(plain_pem, password),
        lambda: load_private_key(plain_pem[:-30] + b"junk"),
        lambda: load_private_key(ec_pem),
        lambda: load_private_key(tmp_path / "missing.pem"),
        lambda: load_private_key(plain_pem.decode()),  # type: ignore[arg-type]
        lambda: sign(key, b"m", pss_salt_length=32),
        lambda: sign(rsa_loaded, b"m", pss_salt_length=-1),
        lambda: sign(rsa_loaded, "text"),  # type: ignore[arg-type]
    ]
    for attempt in attempts:
        with pytest.raises((CryptoError, TypeError)) as info:
            attempt()
        errors.append(str(info.value))
        cause = info.value.__cause__
        if cause is not None:
            errors.append(str(cause))

    pem_body = b"".join(plain_pem.splitlines()[1:-1])
    enc_body = b"".join(enc_pem.splitlines()[1:-1])
    secrets = [
        password.encode(),
        RFC_SEED,
        RFC_SEED.hex().encode(),
        pem_body,
        enc_body,
        b"BEGIN",
        *sigs,
        *(b64encode(s).encode() for s in sigs),
        *(urlsafe_b64encode(s).encode() for s in sigs),
        *(s.hex().encode() for s in sigs),
    ]
    haystack = "\n".join(
        [r.getMessage() for r in caplog.records]
        + [str(r.args) for r in caplog.records]
        + errors
        + [repr(key), str(rsa_loaded)]
    ).encode("utf-8", "replace")
    for secret in secrets:
        assert secret not in haystack
    assert caplog.records  # the debug "loaded" lines were actually captured
