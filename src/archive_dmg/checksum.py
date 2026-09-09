"""SHA-256 checksum computation and the standard ``.sha256`` companion file."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from pathlib import Path

_CHUNK_SIZE = 4 * 1024 * 1024


def sha256_file(path: Path, *, on_bytes_read: Callable[[int], None] | None = None) -> str:
    """Compute the hex-encoded SHA-256 digest of ``path``.

    Reads in fixed-size chunks so multi-gigabyte DMGs do not need to fit in
    memory. ``on_bytes_read`` is invoked with the number of bytes read from
    each chunk, letting callers drive a progress bar without this module
    knowing anything about Rich.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            if on_bytes_read is not None:
                on_bytes_read(len(chunk))
    return digest.hexdigest()


def sha256_hex_to_base64(hex_digest: str) -> str:
    """Convert a hex SHA-256 digest to the base64 form S3 uses for ``ChecksumSHA256``."""
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


def format_sha256_line(hex_digest: str, filename: str) -> str:
    """Format a digest line compatible with ``shasum -a 256 -c``.

    The filename must be a bare name (no directory components) since the
    checksum file is expected to sit alongside the archive it describes.
    """
    return f"{hex_digest}  {filename}\n"


def write_sha256_file(dmg_path: Path, hex_digest: str) -> Path:
    """Write ``<dmg_path>.sha256`` next to the DMG and return its path."""
    sha256_path = dmg_path.with_name(dmg_path.name + ".sha256")
    sha256_path.write_text(format_sha256_line(hex_digest, dmg_path.name))
    return sha256_path


def parse_sha256_line(text: str) -> str | None:
    """Extract the hex digest from a ``shasum``-style checksum file.

    Returns ``None`` when the text does not contain a recognizable 64-character
    hex digest, so callers can report "no usable checksum" rather than treating
    a malformed companion file as a verification failure.
    """
    for line in text.splitlines():
        candidate = line.strip().split(maxsplit=1)
        if not candidate:
            continue
        digest = candidate[0].strip("*").lower()
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            return digest
    return None
