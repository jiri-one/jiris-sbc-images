#!/usr/bin/env python3
"""Convert a package's ARM64 Image or EFI zboot kernel to a raw ARM64 Image.

EFI zboot stores the payload location and compression method in its header:
https://github.com/torvalds/linux/blob/v7.2/drivers/firmware/efi/libstub/zboot-header.S
https://github.com/torvalds/linux/blob/v7.2/drivers/firmware/efi/libstub/Makefile.zboot

The caller must authenticate the package before calling this script. Conversion
checks the format and sizes; it does not replace package signature verification.
"""

import argparse
import gzip
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile


ARM64_MAGIC = b"ARM\x64"
LINUX_PE_MAGIC = b"\xcd\x23\x82\x81"
MAX_IMAGE_BYTES = 512 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


def read_exact_at(stream, offset, size):
    stream.seek(offset)
    data = stream.read(size)
    if len(data) != size:
        raise ValueError("truncated kernel image")
    return data


def zboot_payload(stream, header, file_size):
    """Return validated (offset, compressed size, method, expanded size)."""
    if header[:8] != b"MZ\0\0zimg" or header[56:60] != LINUX_PE_MAGIC:
        raise ValueError("not a Linux EFI zboot header")
    if header[16:24] != b"\0" * 8:
        raise ValueError("unsupported EFI zboot reserved header fields")
    if b"\0" not in header[24:56]:
        raise ValueError("unterminated EFI zboot compression name")
    method = header[24:56].split(b"\0", 1)[0].decode("ascii")
    if method not in {"gzip", "zstd", "zstd22"}:
        raise ValueError(f"unsupported EFI zboot compression: {method!r}")

    offset, size = struct.unpack_from("<II", header, 8)
    if offset < 64 or size == 0 or offset + size > file_size:
        raise ValueError("EFI zboot payload falls outside the input file")

    pe_offset = struct.unpack_from("<I", header, 60)[0]
    if pe_offset < 64 or pe_offset + 26 > offset:
        raise ValueError("invalid EFI zboot PE header location")
    pe = read_exact_at(stream, pe_offset, 26)
    if pe[:4] != b"PE\0\0" or struct.unpack_from("<H", pe, 4)[0] != 0xAA64:
        raise ValueError("EFI zboot executable is not an ARM64 PE image")
    sections = struct.unpack_from("<H", pe, 6)[0]
    optional_size = struct.unpack_from("<H", pe, 20)[0]
    if (
        not sections
        or optional_size < 2
        or struct.unpack_from("<H", pe, 24)[0] != 0x20B
        or pe_offset + 24 + optional_size + sections * 40 > offset
    ):
        raise ValueError("invalid EFI zboot PE section header bounds")

    # gzip includes ISIZE in its own trailer. For zstd, Linux appends an
    # uncompressed-size word which the zboot payload_size deliberately excludes.
    if method == "gzip":
        if size < 18:
            raise ValueError("truncated gzip payload")
        trailer_offset = offset + size - 4
    else:
        trailer_offset = offset + size
        if trailer_offset + 4 > file_size:
            raise ValueError("missing EFI zboot uncompressed-size trailer")
    expanded_size = struct.unpack("<I", read_exact_at(stream, trailer_offset, 4))[0]
    if not 64 <= expanded_size <= MAX_IMAGE_BYTES:
        raise ValueError("EFI zboot expanded size is outside the supported range")
    return offset, size, method, expanded_size


def copy_bounded(source, destination, limit):
    total = 0
    while chunk := source.read(min(CHUNK_BYTES, limit - total + 1)):
        total += len(chunk)
        if total > limit:
            raise ValueError("expanded kernel exceeds its expected size")
        destination.write(chunk)
    return total


def decompress_payload(payload, output, method, expected_size):
    if method == "gzip":
        with gzip.GzipFile(fileobj=payload, mode="rb") as decoder:
            actual_size = copy_bounded(decoder, output, expected_size)
    else:
        # Keep diagnostics off the pipe so malformed input cannot deadlock the
        # producer. Stream both input and output instead of loading a kernel
        # several hundred megabytes long into Python memory.
        with tempfile.TemporaryFile() as errors:
            with subprocess.Popen(
                ["zstd", "--decompress", "--stdout", "--quiet"],
                stdin=payload,
                stdout=subprocess.PIPE,
                stderr=errors,
            ) as decoder:
                try:
                    actual_size = copy_bounded(decoder.stdout, output, expected_size)
                except BaseException:
                    decoder.kill()
                    decoder.wait()
                    raise
                if decoder.wait() != 0:
                    errors.seek(0)
                    message = errors.read(4096).decode("utf-8", errors="replace").strip()
                    raise ValueError(f"zstd decompression failed: {message}")
    if actual_size != expected_size:
        raise ValueError("expanded kernel size does not match the zboot trailer")
    return actual_size


def validate_arm64_image(image, file_size, padded=False):
    header = read_exact_at(image, 0, 64)
    if header[56:60] != ARM64_MAGIC:
        raise ValueError("decompressed payload is not an ARM64 Linux Image")
    image_size, flags = struct.unpack_from("<QQ", header, 16)
    if not 64 <= image_size <= MAX_IMAGE_BYTES:
        raise ValueError("ARM64 Image declares an invalid memory size")
    if flags & 1:
        raise ValueError("big-endian ARM64 Images are not supported on this board")
    if file_size > image_size:
        raise ValueError("ARM64 Image is larger than its declared memory size")
    # Makefile.zboot pads its input through BSS to the Image memory size.
    if padded and file_size != image_size:
        raise ValueError("EFI zboot payload does not match the ARM64 Image memory size")


def extract_kernel(source_path, destination_path):
    source_path, destination_path = Path(source_path), Path(destination_path)
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("input and output must be different files")
    temporary_path = None
    try:
        with source_path.open("rb") as source:
            file_size = os.fstat(source.fileno()).st_size
            if not 64 <= file_size <= MAX_IMAGE_BYTES:
                raise ValueError("input kernel size is outside the supported range")
            header = read_exact_at(source, 0, 64)
            is_zboot = header[4:8] == b"zimg"
            if is_zboot:
                offset, size, method, expected_size = zboot_payload(source, header, file_size)
                description = f"EFI zboot ({method})"
            elif header[56:60] == ARM64_MAGIC:
                validate_arm64_image(source, file_size)
                description = "ARM64 Image"
            else:
                raise ValueError("input is neither an ARM64 Image nor a Linux EFI zboot kernel")

            with tempfile.NamedTemporaryFile(
                prefix=f".{destination_path.name}.",
                suffix=".tmp",
                dir=destination_path.parent,
                mode="w+b",
                delete=False,
            ) as output:
                temporary_path = Path(output.name)
                if is_zboot:
                    with tempfile.TemporaryFile() as payload:
                        source.seek(offset)
                        remaining = size
                        while remaining:
                            chunk = source.read(min(CHUNK_BYTES, remaining))
                            if not chunk:
                                raise ValueError("truncated EFI zboot payload")
                            payload.write(chunk)
                            remaining -= len(chunk)
                        payload.seek(0)
                        actual_size = decompress_payload(payload, output, method, expected_size)
                else:
                    source.seek(0)
                    actual_size = copy_bounded(source, output, file_size)
                    if actual_size != file_size:
                        raise ValueError("input Image changed during conversion")

                output.flush()
                validate_arm64_image(output, actual_size, padded=is_zboot)
                output.seek(0)
                digest = hashlib.file_digest(output, "sha256").hexdigest()
                os.fchmod(output.fileno(), 0o644)
                os.fsync(output.fileno())
            os.replace(temporary_path, destination_path)
            temporary_path = None
            directory_fd = os.open(destination_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return description, actual_size, digest
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="package-owned vmlinuz")
    parser.add_argument("output", type=Path, help="raw Image for U-Boot")
    arguments = parser.parse_args()
    try:
        description, size, digest = extract_kernel(arguments.input, arguments.output)
    except (OSError, ValueError, EOFError) as error:
        print(f"extract-kernel: {error}", file=sys.stderr)
        return 1
    print(f"{description} -> ARM64 Image: {size} bytes; SHA256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
