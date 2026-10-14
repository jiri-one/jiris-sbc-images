#!/usr/bin/env python3
"""Record fetch-inputs.sh's verified inputs and exported configs/sources.env."""

import argparse
import configparser
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

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


def manifest(verified_fingerprint):
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
    return {
        "observed_on": datetime.now(timezone.utc).date().isoformat(),
        "bootstrap": {
            "url": f"{os.environ['BOOTSTRAP_BASE'].rstrip('/')}/{os.environ['BOOTSTRAP_NAME']}",
            "sha256": checked_archive("BOOTSTRAP_NAME", "BOOTSTRAP_SHA256"),
            "authentication": "HTTPS and pinned maintainer-published SHA256; detached signature unavailable",
        },
        "firmware": {
            "url": os.environ["FIRMWARE_URL"],
            "sha256": checked_archive("FIRMWARE_NAME", "FIRMWARE_SHA256"),
            "authentication": "HTTPS GitHub release asset digest, pinned in configs/sources.env",
        },
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verified-fingerprint", required=True,
                        help="Fingerprint already checked by fetch-inputs.sh")
    parser.add_argument("--output", type=Path, default=PROJECT / "artifacts/input-manifest.json")
    args = parser.parse_args()
    document = manifest(args.verified_fingerprint)
    if args.output.is_symlink() or (args.output.exists() and not args.output.is_file()):
        raise ValueError("Manifest output must be a regular file")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2) + "\n")
    print(f"Recorded verified input provenance: {args.output}")


if __name__ == "__main__":
    main()
