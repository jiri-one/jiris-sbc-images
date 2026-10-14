#!/usr/bin/env python3
"""Reject raw ARM64 Images and wrong-architecture EFI binaries before staging."""

from pathlib import Path
import struct
import sys


def validate(path: Path) -> None:
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError("missing DOS/PE header")
        offset = struct.unpack_from("<I", header, 0x3C)[0]
        if offset < 64:
            raise ValueError("invalid PE header offset")
        stream.seek(offset)
        pe = stream.read(24 + 70)
    if len(pe) != 94 or pe[:4] != b"PE\0\0":
        raise ValueError("missing PE signature or truncated header")
    if struct.unpack_from("<H", pe, 4)[0] != 0xAA64:
        raise ValueError("EFI machine is not AArch64")
    if struct.unpack_from("<H", pe, 20)[0] < 70:
        raise ValueError("truncated PE optional header")
    if struct.unpack_from("<H", pe, 24)[0] != 0x20B:
        raise ValueError("EFI image is not PE32+")
    if struct.unpack_from("<H", pe, 24 + 68)[0] != 10:
        raise ValueError("PE subsystem is not an EFI application")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: validate-efi.py FILE [FILE ...]")
    for argument in sys.argv[1:]:
        try:
            validate(Path(argument))
        except (OSError, ValueError) as error:
            sys.exit(f"{argument}: {error}")
