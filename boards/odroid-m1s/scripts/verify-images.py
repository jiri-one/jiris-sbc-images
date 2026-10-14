#!/usr/bin/env python3
"""Verify released regular image files, including their actual partition data."""

import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import uuid
import zlib

PROJECT = Path(__file__).resolve().parent.parent
ARTIFACTS = PROJECT / "artifacts"
VERIFY = PROJECT / "build/image-verification"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def gpt_header(stream, lba, disk_size):
    stream.seek(lba * 512)
    block = stream.read(512)
    require(block[:8] == b"EFI PART", "GPT header signature missing")
    header_bytes = struct.unpack_from("<I", block, 12)[0]
    require(92 <= header_bytes <= 512, "Invalid GPT header size")
    saved_crc = struct.unpack_from("<I", block, 16)[0]
    header = bytearray(block[:header_bytes])
    header[16:20] = b"\0" * 4
    require(zlib.crc32(header) == saved_crc, "GPT header CRC mismatch")
    current, backup, first, last = struct.unpack_from("<QQQQ", block, 24)
    require(current == lba and backup < disk_size // 512, "GPT header LBA invalid")
    entry_lba, count, entry_size, entries_crc = struct.unpack_from("<QIII", block, 72)
    require(entry_size >= 128 and count * entry_size <= 1024 * 1024, "GPT entries bounds invalid")
    stream.seek(entry_lba * 512)
    entries = stream.read(count * entry_size)
    require(zlib.crc32(entries) == entries_crc, "GPT partition-table CRC mismatch")
    parsed = []
    for position in range(count):
        entry = entries[position * entry_size:(position + 1) * entry_size]
        if entry[:16] == b"\0" * 16:
            continue
        start, end, flags = struct.unpack_from("<QQQ", entry, 32)
        require(first <= start <= end <= last, "Partition outside usable GPT range")
        parsed.append({"uuid": str(uuid.UUID(bytes_le=entry[16:32])),
                       "start": start, "sectors": end - start + 1, "flags": flags})
    return entries, parsed, backup


def extract_partition(stream, partition, output):
    size = partition["sectors"] * 512
    stream.seek(partition["start"] * 512)
    with output.open("wb") as target:
        target.truncate(size)
        remaining = size
        while remaining:
            chunk = stream.read(min(4 * 1024 * 1024, remaining))
            require(bool(chunk), "Truncated partition")
            if chunk.count(0) == len(chunk):
                target.seek(len(chunk), 1)
            else:
                target.write(chunk)
            remaining -= len(chunk)
    result = subprocess.run(["e2fsck", "-fn", str(output)], capture_output=True, text=True)
    (output.parent / f"{output.name}.fsck.log").write_text(result.stdout + result.stderr)
    require(result.returncode == 0, f"Filesystem check failed: {output}")


def read_file(filesystem, path):
    result = subprocess.run(["debugfs", "-R", f"cat {path}", str(filesystem)],
                            capture_output=True, check=True)
    require(b"File not found" not in result.stderr, f"Missing {path}")
    return result.stdout


def stat_file(filesystem, path):
    result = subprocess.run(["debugfs", "-R", f"stat {path}", str(filesystem)],
                            capture_output=True, text=True, check=True)
    return result.stdout


def verify_script(script):
    require(script[:4] == b"\x27\x05\x19\x56", "Missing U-Boot script header")
    header = bytearray(script[:64])
    hcrc = struct.unpack_from(">I", header, 4)[0]
    header[4:8] = b"\0" * 4
    require(zlib.crc32(header) == hcrc, "U-Boot script header CRC mismatch")
    payload_size = struct.unpack_from(">I", script, 12)[0]
    payload_crc = struct.unpack_from(">I", script, 24)[0]
    require(payload_size == len(script) - 64, "U-Boot script size mismatch")
    require(zlib.crc32(script[64:]) == payload_crc, "U-Boot script payload CRC mismatch")
    require(b"sysboot nvme 0:1 any ${pxefile_addr_r} /extlinux/extlinux.conf" in script,
            "NVMe script lacks explicit boot target")


def verify_image(image):
    metadata = json.loads(image.with_suffix(".json").read_text())
    target = VERIFY / image.stem
    target.mkdir(parents=True, exist_ok=True)
    require(image.is_file(), "Only regular image files may be checked")
    require(image.stat().st_size == metadata["image_bytes"], "Image size mismatch")
    with image.open("rb") as stream:
        mbr = stream.read(512)
        require(mbr[510:512] == b"\x55\xaa" and mbr[450] == 0xEE, "Missing protective MBR")
        entries, partitions, backup_lba = gpt_header(stream, 1, image.stat().st_size)
        backup, backup_partitions, primary_lba = gpt_header(stream, backup_lba, image.stat().st_size)
        require(entries == backup and partitions == backup_partitions and primary_lba == 1,
                "Primary and backup GPT disagree")
        uboot = (PROJECT / "build/uboot/u-boot-rockchip.bin").read_bytes()
        stream.seek(64 * 512)
        require(stream.read(len(uboot)) == uboot, "Raw U-Boot differs from verified build")
        require(64 * 512 + len(uboot) <= partitions[0]["start"] * 512, "U-Boot overlaps /boot")
        require(partitions[0]["start"] == 32768 and partitions[0]["flags"] & 4,
                "Boot partition offset or bootable attribute wrong")
        require(partitions[0]["uuid"] == metadata["boot_partuuid"], "Boot PARTUUID mismatch")
        bootfs = target / "boot.ext4"
        extract_partition(stream, partitions[0], bootfs)
        if metadata["flavour"] == "nvme-loader":
            require(len(partitions) == 1, "Carrier should have exactly one partition")
            verify_script(read_file(bootfs, "/boot.scr"))
        else:
            require(len(partitions) == 2, "OS image should have two partitions")
            root = partitions[1]
            require(root["uuid"] == metadata["root_partuuid"], "Root PARTUUID mismatch")
            config = read_file(bootfs, "/extlinux/extlinux.conf").decode()
            require(config == metadata["extlinux"], "Extlinux differs from image metadata")
            require(f"root=PARTUUID={root['uuid']}" in config, "Wrong extlinux root device")
            kernel = re.search(r"^    LINUX (\S+)$", config, re.MULTILINE).group(1)
            raw_kernel = read_file(bootfs, kernel)
            require(raw_kernel[56:60] == b"ARM\x64", "Kernel is not a raw ARM64 Image")
            require(hashlib.sha256(raw_kernel).hexdigest() == metadata["kernel_sha256"], "Kernel checksum mismatch")
            dtb = re.search(r"^    FDT (\S+)$", config, re.MULTILINE).group(1)
            require(read_file(bootfs, dtb)[:4] == b"\xd0\x0d\xfe\xed", "Bad device tree")
            rootfs = target / "root.ext4"
            extract_partition(stream, root, rootfs)
            fstab = read_file(rootfs, "/etc/fstab").decode()
            require(f"UUID={metadata['root_fs_uuid']} / ext4" in fstab, "Wrong root fstab UUID")
            require(f"UUID={metadata['boot_fs_uuid']} /boot ext4" in fstab, "Wrong boot fstab UUID")
            if metadata["flavour"] == "sd-emmc":
                data_entry = next((line.split() for line in fstab.splitlines()
                                   if line.startswith("LABEL=M1S_DATA ")), None)
                require(data_entry is not None and data_entry[1:3] == ["/mnt/nvme", "ext4"],
                        "Optional NVMe data mount missing")
                data_options = set(data_entry[3].split(","))
                require({"nofail", "x-systemd.automount", "x-systemd.device-timeout=5s"}
                        <= data_options, "Data mount can block boot")
                require("x-systemd.makefs" not in data_options and
                        "x-systemd.growfs" not in data_options,
                        "Data mount must never format or grow a filesystem")
                data_directory_stat = stat_file(rootfs, "/mnt/nvme")
                require("Type: directory" in data_directory_stat and
                        "User:     0" in data_directory_stat and
                        "0755" in data_directory_stat,
                        "Data mount directory must be root-owned with mode 0755")
            for fs, expected in ((rootfs, metadata["root_fs_uuid"]), (bootfs, metadata["boot_fs_uuid"])):
                with fs.open("rb") as file:
                    file.seek(1024 + 104)
                    require(str(uuid.UUID(bytes=file.read(16))) == expected, "ext4 UUID mismatch")
            require(b"User:     0" in stat_file(rootfs, "/usr/bin/sudo").encode(), "sudo has wrong target UID")
            require("4755" in stat_file(rootfs, "/usr/bin/sudo"), "sudo lost its setuid mode")
            require("User:  1000" in stat_file(rootfs, "/home/odroid"), "User home has wrong target UID")
            require(read_file(rootfs, "/etc/machine-id") == b"", "Cloned machine identity")
            require("Inode:" not in stat_file(rootfs, "/etc/pacman.d/gnupg"), "Private pacman keyring cloned")
            require("Inode:" not in stat_file(rootfs, "/etc/ssh/ssh_host_ed25519_key"), "SSH host key cloned")
            require("Inode:" not in stat_file(rootfs, "/overlay"), "Stray source overlay directory")
            shadow = read_file(rootfs, "/etc/shadow").decode()
            users = {line.split(":")[0]: line.split(":") for line in shadow.splitlines()}
            require(users["root"][1].startswith(("!", "*")), "Root password login is not locked")
            require(users["odroid"][1] == "*" and users["odroid"][2] != "0" and
                    not users["odroid"][4] and not users["odroid"][7],
                    "User must have a locked password without account/password expiry")
            require("Inode:" not in stat_file(rootfs, "/home/odroid/.ssh"), "Image contains a login key")
            require(b"PasswordAuthentication no" in read_file(rootfs, "/etc/ssh/sshd_config.d/20-odroid-m1s.conf"),
                    "Password authentication must be disabled")
            require(read_file(rootfs, "/etc/sudoers.d/10-wheel") == b"%wheel ALL=(ALL:ALL) NOPASSWD: ALL\n",
                    "Key-based login requires passwordless sudo")
            require(b"DisableSandbox" not in read_file(rootfs, "/etc/pacman.conf"), "Build-only sandbox bypass leaked")
    actual_hash = digest(image)
    if "image_sha256" in metadata:
        require(actual_hash == metadata["image_sha256"], "Raw image checksum mismatch")
    report = {"image": image.name, "sha256": actual_hash, "passed": True,
              "gpt_and_actual_filesystems_verified": True, "physical_board_boot_verified": False}
    (target / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    VERIFY.mkdir(parents=True, exist_ok=True)
    reports = [verify_image(image) for image in sorted(ARTIFACTS.glob("*.img"))]
    require(any(report["image"] == "odroid-m1s-archlinux-ports-sd-emmc.img"
                for report in reports), "Expected the main SD/eMMC image")
    (VERIFY / "report.json").write_text(json.dumps(reports, indent=2) + "\n")
    print(json.dumps(reports, indent=2))


if __name__ == "__main__":
    main()
