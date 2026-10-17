#!/usr/bin/env python3
"""Record fetch-inputs.sh's verified inputs and exported configs/sources.env."""

import argparse
import configparser
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

PROJECT = Path(__file__).resolve().parent.parent


def sha256(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular downloaded input: {path}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_archive(name_variable, hash_variable):
    name = os.environ[name_variable]
    if Path(name).name != name or name in (".", ".."):
        raise ValueError(f"Invalid archive name: {name_variable}")
    actual = sha256(PROJECT / "cache" / name)
    if actual != os.environ[hash_variable]:
        raise ValueError(f"Downloaded input differs from pinned {hash_variable}")
    return actual


def runtime_firmware():
    commit = os.environ["RUNTIME_FIRMWARE_COMMIT"]
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("Expected a pinned runtime firmware commit")
    files = {}
    for name, hash_variable in (("start4.elf", "RUNTIME_START4_SHA256"),
                                ("fixup4.dat", "RUNTIME_FIXUP4_SHA256")):
        actual = sha256(PROJECT / "cache" / f"raspberrypi-firmware-{commit}" / name)
        if actual != os.environ[hash_variable]:
            raise ValueError(f"Downloaded input differs from pinned {hash_variable}")
        if sha256(PROJECT / "build/firmware" / name) != actual:
            raise ValueError(f"Staged runtime firmware differs from verified input: {name}")
        files[name] = {
            "url": f"{os.environ['RUNTIME_FIRMWARE_BASE'].rstrip('/')}/{name}",
            "sha256": actual,
        }
    return {
        "project": "raspberrypi/firmware",
        "version": os.environ["RUNTIME_FIRMWARE_VERSION"],
        "commit": commit,
        "files": files,
        "authentication": "HTTPS at pinned release commit and SHA256 in configs/sources.env",
    }


def manifest(verified_fingerprint, boot_mode="native"):
    if boot_mode not in ("native", "uefi"):
        raise ValueError("Boot mode must be native or uefi")
    fingerprint = os.environ["PORTS_KEY_FINGERPRINT"]
    if verified_fingerprint != fingerprint:
        raise ValueError("The verified public key differs from the pinned fingerprint")
    pacman = configparser.ConfigParser(interpolation=None, allow_no_value=True)
    with (PROJECT / "configs/pacman.conf").open() as stream:
        pacman.read_file(stream)
    architecture = pacman["options"]["Architecture"]
    repositories = [pacman[repository]["Server"].replace("$repo", repository)
                    .replace("$arch", architecture)
                    for repository in pacman.sections() if repository != "options"]
    if architecture != "aarch64" or not repositories:
        raise ValueError("Expected configured AArch64 package repositories")
    document = {
        "boot_mode": boot_mode,
        "observed_on": datetime.now(timezone.utc).date().isoformat(),
        "bootstrap": {
            "url": f"{os.environ['BOOTSTRAP_BASE'].rstrip('/')}/{os.environ['BOOTSTRAP_NAME']}",
            "sha256": checked_archive("BOOTSTRAP_NAME", "BOOTSTRAP_SHA256"),
            "authentication": "HTTPS and pinned maintainer-published SHA256; detached signature unavailable",
        },
        "runtime_firmware": runtime_firmware(),
        "package_signer": {
            "fingerprint": fingerprint,
            "public_key_sha256": sha256(PROJECT / "cache/ports-public.key"),
            "source": os.environ["PORTS_KEY_URL"],
            "trust": "Only this key locally signed in target pacman; official archlinux-keyring retained for arch=any packages",
        },
        "repositories": repositories,
        "host_tools_base": os.environ["TOOLS_BASE"],
        "physical_board_tested": False,
    }
    if boot_mode == "uefi":
        document["firmware"] = {
            "project": "pftf/RPi4",
            "version": os.environ["FIRMWARE_VERSION"],
            "url": os.environ["FIRMWARE_URL"],
            "sha256": checked_archive("FIRMWARE_NAME", "FIRMWARE_SHA256"),
            "authentication": "HTTPS GitHub release asset digest, pinned in configs/sources.env",
        }
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verified-fingerprint", required=True,
                        help="Fingerprint already checked by fetch-inputs.sh")
    parser.add_argument("--boot-mode", choices=("uefi", "native"),
                        default=os.environ.get("RPI4_BOOT_MODE") or "native",
                        help="Boot profile (default: native; UEFI is deprecated)")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.boot_mode not in ("native", "uefi"):
        parser.error("RPI4_BOOT_MODE must be native or uefi")
    destination = args.output or PROJECT / ("artifacts/native/input-manifest.json" if args.boot_mode == "native"
                                          else "artifacts/input-manifest.json")
    document = manifest(args.verified_fingerprint, args.boot_mode)
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise ValueError("Manifest output must be a regular file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, indent=2) + "\n")
    print(f"Recorded verified input provenance: {destination}")


if __name__ == "__main__":
    main()
