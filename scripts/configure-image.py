#!/usr/bin/env python3
"""Add one SSH public key to an unbooted release image without root or mounts.

Requires Linux, Python 3.11+, coreutils, util-linux, e2fsprogs and OpenSSH.
Example: ./scripts/configure-image.py release.img --ssh-key ~/.ssh/id_ed25519.pub \
             --output my-board.img
The source stays unchanged; the output must not already exist.
"""

import argparse
import base64
from contextlib import contextmanager
from datetime import date
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

MIB = 1024 * 1024
ROOT_TYPE = "B921B045-1DF0-41C3-AF44-4C6F280D3FAE"
BOARDS = {
    "RPI4_ROOT": ("rpi", "rpi4", MIB, "C12A7328-F81F-11D2-BA4B-00A0C93EC93B", {"sd-usb"}),
    "M1S_ROOT": ("odroid", "odroid-m1s", 16 * MIB, "0FC63DAF-8483-4772-8E79-3D69D8477DE4", {"sd-emmc", "nvme"}),
}
KEY_TYPES = {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256",
             "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"}


def run(arguments, **kwargs):
    result = subprocess.run(arguments, capture_output=True,
                            env={**os.environ, "LC_ALL": "C"}, **kwargs)
    if result.returncode:
        detail = (result.stderr + result.stdout).decode(errors="replace").strip()
        raise ValueError(f"{arguments[0]} failed: {detail}")
    return result


@contextmanager
def regular_input(path):
    """Hold a read-only descriptor so tools cannot follow a replaced input path."""
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"Expected a regular file, not a symlink or device: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"Expected a regular file: {path}")
        yield fd
    finally:
        os.close(fd)


def public_key(fd, work):
    data = os.read(fd, 16385)
    if not data or len(data) > 16384:
        raise ValueError("SSH public key must be a single line of at most 16 KiB")
    line = data.removesuffix(b"\n").decode("utf-8")
    if any(not char.isprintable() and char != "\t" for char in line):
        raise ValueError("SSH public key must be one line without control characters")
    fields = line.split(None, 2)
    if len(fields) < 2 or fields[0] not in KEY_TYPES:
        raise ValueError("Use an Ed25519, RSA or ECDSA public key without authorized_keys options")
    blob = base64.b64decode(fields[1], validate=True)
    length = int.from_bytes(blob[:4], "big")
    if blob[4:4 + length] != fields[0].encode():
        raise ValueError("SSH public key type does not match its encoded contents")
    key = (" ".join(fields) + "\n").encode()
    (work / "key.pub").write_bytes(key)
    fingerprint = run(["ssh-keygen", "-l", "-f", "key.pub"], cwd=work).stdout.decode().strip()
    if fields[0] == "ssh-rsa" and int(fingerprint.split()[0]) < 2048:
        raise ValueError("RSA keys must be at least 2048 bits")
    return key, fingerprint


def partition(image):
    for option in ("--verify", "--json"):
        result = run(["sfdisk", option, str(image)])
        if result.stderr.strip():
            raise ValueError("Partition table warning: " + result.stderr.decode().strip())
    table = json.loads(result.stdout)["partitiontable"]
    parts = table.get("partitions", [])
    if (table.get("label") != "gpt" or table.get("sectorsize") != 512
            or table.get("unit") != "sectors" or len(parts) != 2):
        raise ValueError("Expected a GPT image with two 512-byte-sector partitions")
    boot, root = parts
    label = root.get("name")
    if label not in BOARDS:
        raise ValueError("Unrecognized board root partition")
    _, _, boot_start, boot_type, _ = BOARDS[label]
    offset, length = root["start"] * 512, root["size"] * 512
    if (boot["start"] * 512 != boot_start or boot["size"] * 512 != 1024 * MIB
            or boot.get("type", "").upper() != boot_type
            or root.get("type", "").upper() != ROOT_TYPE
            or boot.get("name") != label.replace("ROOT", "BOOT")
            or offset != boot_start + 1024 * MIB or length <= 0
            or offset + length != image.stat().st_size - MIB):
        raise ValueError("Partition layout does not match a supported release image")
    return label, root["uuid"], offset, length


def copy_partition(image, filesystem, offset, length, *, restore=False):
    """Keep holes on extraction; on restore write only chunks that changed.

    Comparing on restore also copies newly zeroed bytes correctly, without
    allocating the unchanged empty space of a multi-gigabyte sparse image.
    """
    with image.open("r+b" if restore else "rb") as disk:
        with filesystem.open("rb" if restore else "w+b") as fs:
            if not restore:
                fs.truncate(length)
            disk.seek(offset)
            for position in range(0, length, 4 * MIB):
                size = min(4 * MIB, length - position)
                source = fs if restore else disk
                data = source.read(size)
                if len(data) != size:
                    raise ValueError("Unexpected end of partition data")
                if restore:
                    if disk.read(size) != data:
                        disk.seek(offset + position)
                        disk.write(data)
                elif data.count(0) != size:
                    fs.seek(position)
                    fs.write(data)
            destination = disk if restore else fs
            destination.flush()
            os.fsync(destination.fileno())


class Filesystem:
    def __init__(self, work):
        self.work = work

    def command(self, command, *, write=False, missing=False):
        args = ["debugfs"] + (["-w"] if write else []) + ["-R", command, "root.ext4"]
        result = run(args, cwd=self.work)
        # debugfs returns success for many command failures. Only its version
        # banner is normal stderr; missing files are allowed solely for stat.
        errors = "\n".join(line for line in result.stderr.decode().splitlines()
                           if not line.startswith("debugfs ")).strip()
        if errors:
            if missing and errors == f"{command[5:]}: File not found by ext2_lookup":
                return None
            raise ValueError(f"debugfs {command}: {errors}")
        return result.stdout

    def inode(self, path, *, missing=False):
        output = self.command(f"stat {path}", missing=missing)
        if output is None:
            return None
        text = output.decode()
        kind = re.search(r"Type:\s+(\S+)\s+Mode:\s+([0-7]+)", text)
        owner = re.search(r"User:\s+(\d+)\s+Group:\s+(\d+).*?Size:\s+(\d+)", text)
        links = re.search(r"Links:\s+(\d+)", text)
        if not (kind and owner and links):
            raise ValueError(f"Cannot read inode metadata: {path}")
        return (kind[1], int(kind[2], 8), int(owner[1]), int(owner[2]),
                int(owner[3]), int(links[1]))

    def read(self, path, *, missing=False):
        info = self.inode(path, missing=missing)
        if info is None:
            return None
        if info[0] != "regular" or info[4] > 65536 or info[5] != 1:
            raise ValueError(f"Expected a small regular file with one link: {path}")
        data = self.command(f"cat {path}")
        if len(data) != info[4]:
            raise ValueError(f"Incomplete file read: {path}")
        return data


def check_image(fs, label, root_uuid):
    user, board, _, _, flavours = BOARDS[label]
    stats = fs.command("stats").decode()
    if not re.search(rf"^Filesystem volume name:\s+{label}$", stats, re.MULTILINE):
        raise ValueError("Root filesystem label does not match the partition table")
    features = re.search(r"^Filesystem features:\s+(.+)$", stats, re.MULTILINE)
    if not features or not {"has_journal", "extent"}.issubset(features[1].split()):
        raise ValueError("Expected an ext4 root filesystem with a journal and extents")
    for path, uid in (("/", 0), ("/etc", 0), ("/home", 0), (f"/home/{user}", 1000)):
        info = fs.inode(path)
        if info[0] != "directory" or info[2] != uid or info[1] & 0o022:
            raise ValueError(f"Unsafe directory type, owner or permissions: {path}")
    config = dict(line.split("=", 1) for line in
                  fs.read(f"/etc/{board}-image.conf").decode().splitlines() if "=" in line)
    if config.get("ROOT_PARTUUID", "").lower() != root_uuid.lower() or config.get("IMAGE_FLAVOUR") not in flavours:
        raise ValueError("Board configuration does not match the partition table")
    if fs.read("/etc/machine-id", missing=True):
        raise ValueError("Image has a machine-id and appears to have booted already")
    accounts = {row.split(":")[0]: row.split(":") for row in fs.read("/etc/passwd").decode().splitlines()}
    account = accounts.get(user, [])
    if len(account) != 7 or account[2:4] != ["1000", "1000"] or account[5] != f"/home/{user}":
        raise ValueError("Image does not contain the expected UID/GID 1000 login account")
    shadows = {row.split(":")[0]: row.split(":") for row in fs.read("/etc/shadow").decode().splitlines()}
    today = (date.today() - date(1970, 1, 1)).days
    for name in ("root", user):
        entry = shadows.get(name, [])
        if len(entry) != 9 or not entry[1].startswith(("!", "*")):
            raise ValueError("Expected locked root and user passwords in a release image")
        if name == user and (not entry[2].isdigit() or int(entry[2]) == 0
                or (entry[4] not in ("", "-1") and int(entry[2]) + int(entry[4]) <= today)
                or (entry[7] not in ("", "-1") and int(entry[7]) <= today)):
            raise ValueError("The login account or its password is expired")
    return user


def install_key(fs, user, key):
    directory = f"/home/{user}/.ssh"
    target = f"{directory}/authorized_keys"
    info = fs.inode(directory, missing=True)
    if info is not None and info[0] != "directory":
        raise ValueError("The login account's .ssh path is not a directory")
    existing = fs.read(target, missing=True) if info else None
    if existing:
        raise ValueError("Image already contains an authorized SSH key")
    if info is None:
        fs.command(f"mkdir {directory}", write=True)
    if existing is not None:
        fs.command(f"rm {target}", write=True)
    fs.command(f"write key.pub {target}", write=True)
    for path, mode in ((directory, "040700"), (target, "0100600")):
        for field, value in (("uid", "1000"), ("gid", "1000"), ("mode", mode)):
            fs.command(f"set_inode_field {path} {field} {value}", write=True)
    if (fs.inode(directory)[:4] != ("directory", 0o700, 1000, 1000)
            or fs.inode(target)[:4] != ("regular", 0o600, 1000, 1000)
            or fs.read(target) != key):
        raise ValueError("Installed SSH key or inode metadata failed verification")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--ssh-key", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    for tool in ("cp", "sfdisk", "debugfs", "e2fsck", "ssh-keygen"):
        if shutil.which(tool) is None:
            raise ValueError(f"Required tool is missing: {tool}")
    output = args.output.absolute()
    if os.path.lexists(output):
        raise ValueError(f"Output already exists; nothing was overwritten: {output}")
    with regular_input(args.image) as source, regular_input(args.ssh_key) as key_fd:
        original = os.fstat(source)
        if original.st_size % 512:
            raise ValueError("Raw image size is not a multiple of 512 bytes")
        with tempfile.TemporaryDirectory(prefix=".configure-image-", dir=output.parent) as temporary:
            work = Path(temporary)
            key, fingerprint = public_key(key_fd, work)
            image = work / "image.img"
            print("Copying and checking the release image...", flush=True)
            run(["cp", "--reflink=auto", "--sparse=always", "--", f"/proc/self/fd/{source}", str(image)], pass_fds=(source,))
            image.chmod(0o600)
            label, root_uuid, offset, length = partition(image)
            filesystem = work / "root.ext4"
            copy_partition(image, filesystem, offset, length)
            run(["e2fsck", "-f", "-n", str(filesystem)])
            fs = Filesystem(work)
            user = check_image(fs, label, root_uuid)
            install_key(fs, user, key)
            run(["e2fsck", "-f", "-n", str(filesystem)])
            copy_partition(image, filesystem, offset, length, restore=True)
            current = os.fstat(source)
            if (current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (original.st_size, original.st_mtime_ns, original.st_ctime_ns):
                raise ValueError("Source image changed while it was being configured")
            # link is atomic and refuses an existing destination, including a
            # symlink created after the initial check. Both paths share a disk.
            os.link(image, output)
            directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    print(f"Created {output}\nSSH login: {user}\nKey: {fingerprint}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        sys.exit(f"Error: {error}")
