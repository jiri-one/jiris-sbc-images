#!/usr/bin/env python3
"""Inspect actual GPT/FAT32/ext4 image bytes without mounting or booting them."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import stat
import struct
import subprocess
import uuid
import zlib

PROJECT = Path(__file__).resolve().parent.parent
ARTIFACTS = PROJECT / "artifacts"
VERIFY = PROJECT / "build/native/image-verification"
MIB = 1024 * 1024
GIB = 1024 * MIB
UEFI_IMAGE_NAME = "rpi4-archlinux-ports-sd-usb.img"
ESP_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
ROOT_ARM64 = "b921b045-1df0-41c3-af44-4c6f280d3fae"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def regular_file(path):
    require(not path.is_symlink() and stat.S_ISREG(path.stat().st_mode),
            f"Expected a regular file, not a symlink or device: {path}")
    return path


def output_path(path):
    require(not path.is_symlink(), f"Refusing symlink output: {path}")
    require(path.resolve().is_relative_to(PROJECT.resolve()), "Output escapes project")
    if path.exists():
        require(stat.S_ISREG(path.stat().st_mode) and path.stat().st_nlink == 1,
                f"Refusing nonregular or hardlinked output: {path}")
    return path


def digest(path):
    with regular_file(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def gpt_header(stream, lba, disk_size):
    require(disk_size % 512 == 0 and 0 < lba < disk_size // 512, "GPT location invalid")
    stream.seek(lba * 512)
    block = stream.read(512)
    require(len(block) == 512 and block[:8] == b"EFI PART", "GPT signature missing")
    revision, size, saved_crc, reserved = struct.unpack_from("<IIII", block, 8)
    require(revision == 0x10000 and reserved == 0 and 92 <= size <= 512,
            "Invalid GPT version/header size/reserved field")
    header = bytearray(block[:size])
    header[16:20] = b"\0" * 4
    require(zlib.crc32(header) == saved_crc, "GPT header CRC mismatch")
    current, backup, first, last = struct.unpack_from("<QQQQ", block, 24)
    require(current == lba and 0 < backup < disk_size // 512 and backup != current,
            "GPT header LBA invalid")
    require(2 < first <= last < disk_size // 512 - 1, "GPT usable range invalid")
    entry_lba, count, entry_size, entries_crc = struct.unpack_from("<QIII", block, 72)
    require(entry_size >= 128 and entry_size % 128 == 0 and 0 < count <= 4096,
            "GPT entry size/count invalid")
    table_size = count * entry_size
    require(table_size <= MIB and 0 < entry_lba * 512 < disk_size and
            entry_lba * 512 + table_size <= disk_size, "GPT entries outside disk")
    table_end = (entry_lba * 512 + table_size + 511) // 512 - 1
    require(not entry_lba <= lba <= table_end and
            (table_end < first or entry_lba > last), "GPT table overlaps usable area/header")
    stream.seek(entry_lba * 512)
    entries = stream.read(table_size)
    require(len(entries) == table_size and zlib.crc32(entries) == entries_crc,
            "GPT partition-table CRC mismatch")
    parsed = []
    for position in range(count):
        entry = entries[position * entry_size:(position + 1) * entry_size]
        if entry[:16] == b"\0" * 16:
            continue
        start, end, flags = struct.unpack_from("<QQQ", entry, 32)
        require(first <= start <= end <= last, "Partition outside usable GPT range")
        parsed.append({
            "type": str(uuid.UUID(bytes_le=entry[:16])),
            "uuid": str(uuid.UUID(bytes_le=entry[16:32])),
            "start": start, "sectors": end - start + 1, "flags": flags,
            "name": entry[56:128].decode("utf-16-le").rstrip("\0"),
        })
    ordered = sorted(parsed, key=lambda part: part["start"])
    require(all(a["start"] + a["sectors"] <= b["start"]
                for a, b in zip(ordered, ordered[1:])), "GPT partitions overlap")
    require(len({part["uuid"] for part in parsed}) == len(parsed), "Duplicate PARTUUID")
    return {"entries": entries, "partitions": parsed, "backup": backup,
            "first": first, "last": last, "disk_uuid": str(uuid.UUID(bytes_le=block[56:72]))}


def verify_gpt(stream, disk_size, metadata):
    stream.seek(0)
    mbr = stream.read(512)
    require(len(mbr) == 512 and mbr[510:512] == b"\x55\xaa", "Protective MBR missing")
    require(mbr[450] == 0xEE and struct.unpack_from("<I", mbr, 454)[0] == 1,
            "Protective MBR partition invalid")
    require(struct.unpack_from("<I", mbr, 458)[0] == min(disk_size // 512 - 1, 0xFFFFFFFF),
            "Protective MBR size invalid")
    require(mbr[462:510] == b"\0" * 48, "Unexpected hybrid MBR entries")
    primary = gpt_header(stream, 1, disk_size)
    require(primary["backup"] == disk_size // 512 - 1, "Backup GPT is not at disk end")
    backup = gpt_header(stream, primary["backup"], disk_size)
    require(backup["backup"] == 1, "Backup GPT does not refer to primary")
    for field in ("entries", "partitions", "disk_uuid", "first", "last"):
        require(primary[field] == backup[field], f"Primary/backup GPT disagree: {field}")
    require(primary["disk_uuid"] == metadata["disk_uuid"], "Disk UUID mismatch")
    parts = primary["partitions"]
    require(len(parts) == 2, "Exactly two GPT partitions are required")
    for part, prefix, kind, label in ((parts[0], "boot", ESP_TYPE, "RPI4_BOOT"),
                                      (parts[1], "root", ROOT_ARM64, "RPI4_ROOT")):
        require(part["type"] == kind and part["name"] == label, "GPT type/name mismatch")
        require(part["uuid"] == metadata[f"{prefix}_partuuid"], "PARTUUID mismatch")
        require(part["start"] * 512 == metadata[f"{prefix}_start_bytes"] and
                part["sectors"] * 512 == metadata[f"{prefix}_size_bytes"], "Partition size/offset mismatch")
        require(part["flags"] == 0, "Unexpected partition attributes")
    require(disk_size == 8 * GIB and parts[0]["start"] * 512 == MIB and
            parts[0]["sectors"] * 512 == GIB and
            parts[1]["start"] * 512 == GIB + MIB and
            (parts[1]["start"] + parts[1]["sectors"]) * 512 == disk_size - MIB,
            "Image does not match the 8GiB Pi4 partition layout")
    return parts


def extract_partition(stream, partition, output, fs_type):
    size = partition["sectors"] * 512
    stream.seek(partition["start"] * 512)
    with output_path(output).open("wb") as target:
        target.truncate(size)
        remaining = size
        while remaining:
            chunk = stream.read(min(4 * MIB, remaining))
            require(bool(chunk), "Truncated partition")
            if chunk.count(0) == len(chunk):
                target.seek(len(chunk), os.SEEK_CUR)
            else:
                target.write(chunk)
            remaining -= len(chunk)
    args = ["fsck.fat", "-n"] if fs_type == "fat32" else ["e2fsck", "-fn"]
    result = subprocess.run([*args, str(output)], capture_output=True, text=True)
    output_path(output.with_suffix(output.suffix + ".fsck.log")).write_text(result.stdout + result.stderr)
    require(result.returncode == 0, f"Read-only filesystem check failed: {output.name}")


def image_path(path):
    require(isinstance(path, str) and path.startswith("/") and
            not any(part in ("..", ".") for part in path.split("/")) and
            not any(char in path for char in ('"', "\\", "\n", "\r", "\0")),
            "Unsafe path in filesystem/loader metadata")
    return path


def read_file(filesystem, path):
    path = image_path(path)
    result = subprocess.run(["debugfs", "-R", f'cat "{path}"', str(filesystem)], capture_output=True)
    require(result.returncode == 0 and not re.search(
        rb"File not found|not a regular file|not found by ext2_lookup|Invalid|Filesystem not open",
        result.stderr, re.IGNORECASE), f"Unable to read ext4 file: {path}")
    return result.stdout


def stat_file(filesystem, path):
    path = image_path(path)
    result = subprocess.run(["debugfs", "-R", f'stat "{path}"', str(filesystem)],
                            capture_output=True, text=True)
    require(result.returncode == 0, f"Unable to inspect ext4 inode: {path}")
    match = re.search(r"Type:\s+(\S+)\s+Mode:\s+(\d+)", result.stdout)
    if not match:
        return None
    owners = re.search(r"User:\s+(\d+)\s+Group:\s+(\d+)", result.stdout)
    require(owners is not None, f"Missing inode owners: {path}")
    return {"type": match.group(1), "mode": int(match.group(2), 8),
            "uid": int(owners.group(1)), "gid": int(owners.group(2)), "text": result.stdout}


def list_directory(filesystem, path):
    path = image_path(path)
    result = subprocess.run(["debugfs", "-R", f'ls -p "{path}"', str(filesystem)],
                            capture_output=True, text=True)
    require(result.returncode == 0, f"Cannot list ext4 directory: {path}")
    entries = []
    for line in result.stdout.splitlines():
        fields = line.split("/")
        if len(fields) >= 7 and fields[1].isdigit() and fields[5] not in (".", ".."):
            entries.append(fields[5])
    return entries


def fat_file(filesystem, path):
    path = image_path(path)
    result = subprocess.run(["mtype", "-i", str(filesystem), f"::{path}"], capture_output=True)
    require(result.returncode == 0, f"Cannot read FAT file: {path}")
    return result.stdout


def verify_pe(data, description):
    require(len(data) >= 64 and data[:2] == b"MZ", f"{description}: missing DOS/EFI header")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    require(64 <= pe <= len(data) - 24 and data[pe:pe + 4] == b"PE\0\0",
            f"{description}: missing PE signature")
    machine, sections, _, _, _, optional_size, characteristics = struct.unpack_from("<HHIIIHH", data, pe + 4)
    optional = pe + 24
    require(machine == 0xAA64, f"{description}: PE machine is not AArch64")
    require(0 < sections <= 96 and characteristics & 2 and optional_size >= 70 and
            optional + optional_size + 40 * sections <= len(data), f"{description}: malformed PE layout")
    require(struct.unpack_from("<H", data, optional)[0] == 0x20B, f"{description}: not PE32+")
    require(struct.unpack_from("<H", data, optional + 68)[0] == 10,
            f"{description}: not an EFI application")
    for index in range(sections):
        position = optional + optional_size + 40 * index
        raw_size, raw_offset = struct.unpack_from("<II", data, position + 16)
        require(raw_offset + raw_size <= len(data), f"{description}: PE section exceeds file")


def verify_fat_header(filesystem, metadata):
    with filesystem.open("rb") as stream:
        block = stream.read(512)
    require(len(block) == 512 and block[510:512] == b"\x55\xaa", "FAT boot signature missing")
    require(struct.unpack_from("<H", block, 11)[0] == 512 and
            struct.unpack_from("<H", block, 17)[0] == 0 and
            struct.unpack_from("<H", block, 22)[0] == 0 and
            struct.unpack_from("<I", block, 36)[0] > 0 and block[82:90] == b"FAT32   ",
            "Boot filesystem is not FAT32")
    volume_id = f"{struct.unpack_from('<I', block, 67)[0]:08X}"
    require(f"{volume_id[:4]}-{volume_id[4:]}" == metadata["boot_fs_uuid"], "FAT UUID mismatch")
    require(volume_id == metadata["boot_volume_id"] and block[71:82].rstrip() == b"RPI4_BOOT",
            "FAT volume ID/label mismatch")
    cluster_sectors = block[13]
    track_sectors = struct.unpack_from("<H", block, 24)[0]
    require(cluster_sectors in (1, 2, 4, 8, 16, 32, 64, 128),
            "Invalid FAT cluster geometry")
    require(0 <= track_sectors <= 63, "Invalid FAT track geometry")
    filesystem_bytes = struct.unpack_from("<I", block, 32)[0] * 512
    partition_bytes = metadata["boot_size_bytes"]
    padding_bytes = partition_bytes - filesystem_bytes
    # mkfs.vfat can round the filesystem down to a track/cluster boundary.
    # The partition remains its declared size; bound padding to less than one
    # alignment unit (at most 64KiB), rather than accepting arbitrary slack.
    alignment_bytes = max(cluster_sectors, track_sectors) * 512
    require(0 < filesystem_bytes <= partition_bytes and
            0 <= padding_bytes < alignment_bytes, "FAT filesystem size mismatch")
    return {"filesystem_bytes": filesystem_bytes, "partition_bytes": partition_bytes,
            "padding_bytes": padding_bytes, "sectors_per_cluster": cluster_sectors,
            "sectors_per_track": track_sectors}


def loader_fields(text):
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition(" ")
        require(separator and value.strip(), "Malformed loader entry")
        fields.setdefault(key.lower(), []).append(value.strip())
    return fields


def native_fields(text):
    """Accept one complete, content-addressed native firmware selection."""
    fields = {}
    names = {"kernel": "kernel8.img", "device_tree": "bcm2711-rpi-4-b.dtb",
             "initramfs": "initramfs-linux.img", "cmdline": "cmdline.txt"}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("initramfs "):
            parts = line.split()
            require(len(parts) == 3 and parts[2] == "followkernel", "Invalid native initramfs directive")
            key, value = parts[:2]
        else:
            key, separator, value = line.partition("=")
            require(separator and key in names, "Unexpected native selector directive")
        require(key not in fields, "Duplicate native selector directive")
        require(re.fullmatch(r"native/[0-9a-f]{24}/" + re.escape(names[key]), value) is not None,
                "Unsafe native boot-set path")
        fields[key] = value
    require(set(fields) == set(names), "Incomplete native selector")
    require(len({str(PurePosixPath(value).parent) for value in fields.values()}) == 1,
            "Native selector mixes boot sets")
    return fields


def native_backend():
    return runpy.run_path(str(PROJECT / "overlay/usr/local/lib/rpi4/update-native-boot.py"))


def verify_native_boot(filesystem, metadata, boot_hashes):
    require(not any(path == "RPI_EFI.fd" or path.startswith(("EFI/", "loader/", "rpi4/"))
                    for path in boot_hashes), "UEFI boot files leaked into native image")
    text = fat_file(filesystem, "/native-current.txt").decode()
    require(text == metadata["native_selector"], "Native selector differs from metadata")
    fields = native_fields(text)
    config = fat_file(filesystem, "/config.txt").decode()
    settings = [line.strip() for line in config.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    expected = {"[all]", "arm_64bit=1", "enable_gic=1", "kernel_address=0x200000",
                "start_file=start4.elf", "fixup_file=fixup4.dat", "arm_boost=1",
                "disable_fw_kms_setup=1", "camera_auto_detect=0", "display_auto_detect=0",
                "dtoverlay=rpi4-usb-c-data-disable", "include native-current.txt"}
    require(len(settings) == len(expected) and set(settings) == expected and
            settings[-1] == "include native-current.txt", "Unexpected native firmware configuration")
    release = str(PurePosixPath(fields["kernel"]).parent)
    manifest = fat_file(filesystem, "/" + release + "/SHA256SUMS")
    require(PurePosixPath(release).name == hashlib.sha256(manifest).hexdigest()[:24],
            "Native boot-set content-address mismatch")
    payloads = {}
    for line in manifest.decode().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        require(match is not None and match[2] not in payloads, "Invalid native checksum manifest")
        payloads[match[2]] = match[1]
        require(boot_hashes.get(release + "/" + match[2]) == match[1], "Native payload checksum mismatch")
    require(set(payloads) == {"kernel8.img", "bcm2711-rpi-4-b.dtb", "initramfs-linux.img",
                              "kernel-version", "cmdline.txt"}, "Native manifest is incomplete")
    version = fat_file(filesystem, "/" + release + "/kernel-version").decode().strip()
    require(re.fullmatch(r"[0-9A-Za-z._+-]+", version) is not None and version not in (".", ".."),
            "Unsafe native kernel version")
    backend = native_backend()
    kernel = fat_file(filesystem, "/" + fields["kernel"])
    require(kernel.startswith(b"\x1f\x8b"), "Native kernel is not a compressed ARM64 Image")
    raw = backend["raw_image"](kernel)
    backend["validate_dtb"](fat_file(filesystem, "/" + fields["device_tree"]))
    require(len(fat_file(filesystem, "/" + fields["initramfs"])) > 1024, "Native initramfs is implausibly small")
    cmdline = fat_file(filesystem, "/" + fields["cmdline"]).decode()
    require(len(cmdline.splitlines()) == 1, "Native command line must be one line")
    options = cmdline.split()
    require([word for word in options if word.startswith("root=")] ==
            [f"root=PARTUUID={metadata['root_partuuid']}"], "Native root PARTUUID mismatch")
    require({"rw", "rootwait", "rootfstype=ext4", "cma=256M@0-1G", "module_blacklist=simpledrm"}
            <= set(options), "Native storage, low CMA or display policy is missing")
    require(not any(word.startswith(("init=", "rdinit=", "systemd.debug_shell")) for word in options),
            "Debug access in native command line")
    return {"boot_mode": "native", "kernel_version": version,
            "kernel_sha256": hashlib.sha256(raw).hexdigest(),
            "dtb_sha256": payloads["bcm2711-rpi-4-b.dtb"]}


def verify_boot(filesystem, metadata):
    boot_hashes = metadata["boot_files_sha256"]
    require(isinstance(boot_hashes, dict) and len(boot_hashes) > 5, "Boot-file manifest missing")
    for path, expected in boot_hashes.items():
        require(hashlib.sha256(fat_file(filesystem, "/" + path)).hexdigest() == expected,
                f"Boot-file checksum mismatch: {path}")
    for path, expected in metadata["firmware"]["files_sha256"].items():
        require(boot_hashes.get(path) == expected, f"Pinned firmware changed: {path}")
    if metadata.get("boot_mode", "uefi") == "native":
        return verify_native_boot(filesystem, metadata, boot_hashes)
    for name in ("config.txt", "RPI_EFI.fd"):
        require(name in boot_hashes and len(fat_file(filesystem, "/" + name)) > 0,
                f"Missing Pi firmware: {name}")
    for name in ("EFI/BOOT/BOOTAA64.EFI", "EFI/systemd/systemd-bootaa64.efi"):
        verify_pe(fat_file(filesystem, "/" + name), name)
    require(fat_file(filesystem, "/EFI/BOOT/BOOTAA64.EFI") ==
            fat_file(filesystem, "/EFI/systemd/systemd-bootaa64.efi"), "EFI fallback differs from systemd-boot")
    text = fat_file(filesystem, "/loader/entries/rpi4.conf").decode()
    require(text == metadata["loader_entry"], "Loader entry differs from metadata")
    fields = loader_fields(text)
    require(len(fields.get("linux", [])) == 1 and len(fields.get("initrd", [])) == 1 and
            len(fields.get("options", [])) == 1, "Expected one kernel, initramfs and options line")
    kernel, initrd = image_path(fields["linux"][0]), image_path(fields["initrd"][0])
    require(PurePosixPath(kernel).parent == PurePosixPath(initrd).parent and
            kernel.startswith("/rpi4/") and kernel.endswith("/linux.efi") and
            initrd.endswith("/initramfs-linux.img"), "Loader paths do not select one versioned kernel set")
    require(kernel.lstrip("/") in boot_hashes and initrd.lstrip("/") in boot_hashes,
            "Loader references files absent from boot manifest")
    kernel_data = fat_file(filesystem, kernel)
    verify_pe(kernel_data, "Linux EFI kernel")
    # The packaged kernel is an EFI zboot executable, whose header differs from
    # an uncompressed ARM64 Image. PE Machine/SubSystem identify the executable.
    require(len(fat_file(filesystem, initrd)) > 1024, "Initramfs is implausibly small")
    options = fields["options"][0].split()
    require([value for value in options if value.startswith("root=")] ==
            [f"root=PARTUUID={metadata['root_partuuid']}"], "Loader root PARTUUID mismatch")
    require("rootwait" in options, "Loader lacks rootwait")
    loader = loader_fields(fat_file(filesystem, "/loader/loader.conf").decode())
    require(loader.get("default") == ["rpi4.conf"], "Wrong systemd-boot default entry")
    # In this experiment EDK2 supplies its fixed-up DT; no stale explicit DT override.
    require("devicetree" not in fields, "Unexpected loader device-tree override")
    release = str(PurePosixPath(kernel).parent)
    manifest = fat_file(filesystem, release + "/SHA256SUMS")
    kernel_version = fat_file(filesystem, release + "/kernel-version").decode().strip()
    require(re.fullmatch(r"[0-9A-Za-z._+-]+", kernel_version) is not None and
            kernel_version not in (".", ".."), "Unsafe kernel version")
    expected_name = kernel_version + "-" + hashlib.sha256(manifest).hexdigest()[:32]
    require(PurePosixPath(release).name == expected_name, "Boot-set content-address mismatch")
    payloads = {}
    for line in manifest.decode().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        require(match is not None and match[2] not in payloads, "Invalid boot-set checksum manifest")
        payloads[match[2]] = match[1]
        require(boot_hashes.get((release + "/" + match[2]).lstrip("/")) == match[1],
                "Boot-set manifest checksum differs from its payload")
    require(set(payloads) == {"linux.efi", "bcm2711-rpi-4-b.dtb", "initramfs-linux.img", "kernel-version"},
            "Boot-set manifest is incomplete")
    dtb = fat_file(filesystem, release + "/bcm2711-rpi-4-b.dtb")
    require(dtb[:4] == b"\xd0\x0d\xfe\xed", "Packaged Pi4 device tree is invalid")
    return {"kernel_version": kernel_version, "kernel_sha256": payloads["linux.efi"],
            "dtb_sha256": payloads["bcm2711-rpi-4-b.dtb"],
            "bootloader_sha256": boot_hashes["EFI/BOOT/BOOTAA64.EFI"]}


def require_inode(filesystem, path, kind, uid, mode=None, gid=None):
    info = stat_file(filesystem, path)
    require(info is not None and info["type"] == kind and info["uid"] == uid and
            (mode is None or info["mode"] == mode) and (gid is None or info["gid"] == gid),
            f"Wrong owner/type/permissions: {path}")
    return info


def verify_root(filesystem, metadata, boot_info):
    with filesystem.open("rb") as stream:
        stream.seek(1024)
        superblock = stream.read(1024)
    require(superblock[56:58] == b"\x53\xef", "Root is not ext4")
    require(str(uuid.UUID(bytes=superblock[104:120])) == metadata["root_fs_uuid"], "ext4 UUID mismatch")
    require(superblock[120:136].rstrip(b"\0") == b"RPI4_ROOT", "ext4 label mismatch")
    fstab = [line.split() for line in read_file(filesystem, "/etc/fstab").decode().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    require(len(fstab) == 2 and all(len(line) == 6 for line in fstab), "Unexpected fstab entries")
    for target, source, fs, check in (("/", metadata["root_fs_uuid"], "ext4", "1"),
                                      ("/boot", metadata["boot_fs_uuid"], "vfat", "2")):
        lines = [line for line in fstab if line[1] == target]
        require(len(lines) == 1 and lines[0][0] == f"UUID={source}" and lines[0][2] == fs and
                lines[0][4:] == ["0", check], f"Wrong fstab {target} filesystem/UUID/check order")
    image_conf = dict(line.split("=", 1) for line in
                      read_file(filesystem, "/etc/rpi4-image.conf").decode().splitlines() if "=" in line)
    expected_config = {"ROOT_PARTUUID": metadata["root_partuuid"],
                       "BOOT_FS_UUID": metadata["boot_fs_uuid"], "IMAGE_FLAVOUR": "sd-usb"}
    if "boot_mode" in metadata:
        expected_config["RPI4_BOOT_MODE"] = metadata["boot_mode"]
    require(image_conf == expected_config,
            "Installed image configuration mismatch")
    require(read_file(filesystem, "/etc/hostname") == b"rpi4\n", "Prototype hostname leaked into image")
    passwd = {line.split(":")[0]: line.split(":")
              for line in read_file(filesystem, "/etc/passwd").decode().splitlines()}
    require("rpi" in passwd and passwd["rpi"][2] == "1000" and passwd["rpi"][5] == "/home/rpi",
            "Expected local rpi user missing")
    require([name for name, fields in passwd.items() if fields[2] == "0"] == ["root"],
            "Unexpected UID 0 account")
    groups = {line.split(":")[0]: line.split(":")
              for line in read_file(filesystem, "/etc/group").decode().splitlines()}
    require("wheel" in groups and "rpi" in groups["wheel"][3].split(","), "rpi is not in wheel")
    shadow = {line.split(":")[0]: line.split(":")
              for line in read_file(filesystem, "/etc/shadow").decode().splitlines()}
    require(shadow["root"][1].startswith(("!", "*")), "Root password login is not locked")
    require(shadow["rpi"][1] == "*" and shadow["rpi"][2] != "0" and
            not shadow["rpi"][4] and not shadow["rpi"][7],
            "User must have a locked password without account/password expiry")
    require(stat_file(filesystem, "/home/rpi/.ssh") is None, "Image contains a login key")
    require(b"PasswordAuthentication no" in read_file(filesystem, "/etc/ssh/sshd_config.d/20-rpi4.conf"),
            "Password authentication must be disabled")
    require(read_file(filesystem, "/etc/sudoers.d/10-wheel") == b"%wheel ALL=(ALL:ALL) NOPASSWD: ALL\n",
            "Key-based login requires passwordless sudo")
    require_inode(filesystem, "/usr/bin/sudo", "regular", 0, 0o4755, 0)
    require_inode(filesystem, "/home/rpi", "directory", 1000, gid=int(passwd["rpi"][3]))
    require_inode(filesystem, "/etc/sudoers.d/10-wheel", "regular", 0, 0o440, 0)
    for name in ("/etc/shadow", "/etc/gshadow"):
        info = require_inode(filesystem, name, "regular", 0)
        require(info["mode"] & 0o037 == 0, f"Exposed shadow permissions: {name}")
    require_inode(filesystem, "/boot", "directory", 0, 0o755, 0)
    require(not list_directory(filesystem, "/boot"), "Root partition contains unmounted boot files")
    require(read_file(filesystem, "/etc/machine-id") == b"", "Cloned machine-id")
    dbus = require_inode(filesystem, "/var/lib/dbus/machine-id", "symlink", 0)
    require('Fast link dest: "/etc/machine-id"' in dbus["text"], "Wrong D-Bus machine-id link")
    for directory in ("/etc/pacman.d/gnupg", "/root/.gnupg"):
        if stat_file(filesystem, directory):
            require(not list_directory(filesystem, directory), "Cloned private GPG keyring")
    require(not any(name.startswith("ssh_host_") for name in list_directory(filesystem, "/etc/ssh")),
            "Cloned SSH host key")
    for path in ("/var/lib/systemd/random-seed", "/var/lib/systemd/credential.secret",
                 "/var/lib/rpi4/initialized", "/var/lib/rpi4/root-expanded", "/overlay"):
        require(stat_file(filesystem, path) is None, f"Cloned/generated state present: {path}")
    for path in ("/usr/local/sbin/rpi4-update-boot", "/usr/local/sbin/rpi4-firstboot",
                 "/usr/local/sbin/rpi4-grow-root"):
        require_inode(filesystem, path, "regular", 0, 0o755, 0)
    for path in ("/etc/systemd/system/multi-user.target.wants/rpi4-firstboot.service",
                 "/etc/systemd/system/multi-user.target.wants/rpi4-grow-root.service",
                 "/etc/systemd/system/timers.target.wants/rpi4-firstboot.timer"):
        require_inode(filesystem, path, "symlink", 0)
    require(b"DisableSandbox" not in read_file(filesystem, "/etc/pacman.conf"),
            "Build-only pacman sandbox bypass leaked")
    require(hashlib.sha256(read_file(filesystem, "/usr/share/rpi4/package-manifest.txt")).hexdigest()
            == metadata["package_manifest_sha256"], "Installed package manifest mismatch")
    kver = boot_info["kernel_version"]
    package_paths = [(f"/usr/lib/modules/{kver}/vmlinuz", "kernel_sha256"),
                     (f"/usr/lib/modules/{kver}/dtb/broadcom/bcm2711-rpi-4-b.dtb", "dtb_sha256")]
    if metadata.get("boot_mode", "uefi") == "uefi":
        package_paths.append(("/usr/lib/systemd/boot/efi/systemd-bootaa64.efi", "bootloader_sha256"))
    for path, key in package_paths:
        data = read_file(filesystem, path)
        if key == "kernel_sha256" and metadata.get("boot_mode", "uefi") == "native":
            data = native_backend()["raw_image"](data)
        require(hashlib.sha256(data).hexdigest() == boot_info[key],
                f"ESP payload differs from installed package: {path}")
    if metadata.get("boot_mode", "uefi") == "native":
        require(read_file(filesystem, "/usr/share/rpi4/initramfs-boot-mode") == b"native\n",
                "Native initramfs profile marker missing")
        require(read_file(filesystem, "/etc/modules-load.d/rpi4-display.conf").splitlines()[-2:] == [b"vc4", b"v3d"],
                "Native display modules are not selected")
        runtime = json.loads(read_file(filesystem, "/usr/share/rpi4/runtime-firmware.json"))
        expected_runtime = metadata["firmware"]["runtime_firmware"]
        require(runtime["version"] == expected_runtime["version"] and runtime["commit"] == expected_runtime["commit"] and
                runtime["sha256"] == {name: item["sha256"] for name, item in expected_runtime["files"].items()},
                "Installed runtime firmware pin differs from build provenance")
    require(read_file(filesystem, "/usr/share/rpi4/initramfs-kernel").decode().strip() == kver,
            "Initramfs build marker does not match selected kernel")


def verify_checksums(image, metadata, allow_uncompressed):
    sums_path = regular_file(image.parent / "SHA256SUMS")
    sums = {}
    for line in sums_path.read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        require(match is not None and match[2] not in sums, "Malformed/duplicate SHA256SUMS entry")
        sums[match[2]] = match[1]
    expected = {image.name, image.with_suffix(".json").name, "package-manifest.txt",
                "package-files.log", "package-cache-SHA256SUMS", "repository-db-SHA256SUMS",
                "firmware-SHA256SUMS", "input-manifest.json", "tools-package-manifest.txt"}
    compressed = Path(f"{image}.zst")
    if not allow_uncompressed or "compressed_sha256" in metadata:
        expected.add(compressed.name)
    # Release preparation may add these attachments after the image build.
    # They are optional, but their hashes must be checked whenever listed.
    attachments = {"arch-rpi4-source.tar.gz", "offline-verification.json", "qemu-verification.json"}
    require(expected <= set(sums) <= expected | attachments,
            "Deliverable checksum list is incomplete or contains unexpected files")
    for name, expected_hash in sums.items():
        require(digest(image.parent / name) == expected_hash, f"Checksum mismatch: {name}")
    require(sums[image.name] == metadata["image_sha256"], "Raw image metadata checksum mismatch")
    require(digest(image.parent / "package-manifest.txt") == metadata["package_manifest_sha256"],
            "Package-manifest metadata checksum mismatch")
    require(digest(image.parent / "input-manifest.json") == metadata["input_manifest_sha256"],
            "Input-manifest metadata checksum mismatch")
    require(digest(image.parent / "tools-package-manifest.txt") == metadata["tools_manifest_sha256"],
            "Tools-manifest metadata checksum mismatch")
    require(metadata["package_file_check"]["passed"] is True and
            digest(image.parent / "package-files.log") == metadata["package_file_check"]["log_sha256"],
            "Successful pacman -Qk build log missing or changed")
    if compressed.name in sums:
        require(sums[compressed.name] == metadata["compressed_sha256"], "Compressed metadata checksum mismatch")
        with subprocess.Popen(["zstd", "-dc", str(regular_file(compressed))],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            hashed = hashlib.sha256()
            size = 0
            while chunk := process.stdout.read(4 * MIB):
                hashed.update(chunk)
                size += len(chunk)
            errors = process.stderr.read()
            require(process.wait() == 0, f"Compressed image failed decoding: {errors.decode(errors='replace')}")
        require(size == metadata["image_bytes"] and hashed.hexdigest() == metadata["image_sha256"],
                "Compressed image does not reproduce the raw image")
    return {"files": sorted(sums), "compressed_stream_verified": compressed.name in sums,
            "sha256": sums[image.name]}


def verify_retained_inputs(metadata, artifacts=None):
    artifacts = ARTIFACTS if artifacts is None else artifacts
    counts = {}
    for name, directory in (("package-cache-SHA256SUMS", PROJECT / "cache/packages"),
                             ("repository-db-SHA256SUMS", PROJECT / "cache/ports-databases")):
        lines = regular_file(artifacts / name).read_text().splitlines()
        require(lines, f"Empty retained-input checksum manifest: {name}")
        seen = set()
        for line in lines:
            match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\\x00\r\n]+)", line)
            require(match is not None and match[2] not in (".", "..") and match[2] not in seen,
                    f"Invalid retained-input checksum manifest: {name}")
            seen.add(match[2])
            require(digest(directory / match[2]) == match[1], f"Retained input checksum mismatch: {match[2]}")
        counts[directory.name] = len(seen)
    firmware_text = "".join(f"{value}  {name}\n" for name, value in
                             metadata["firmware"]["files_sha256"].items())
    require(regular_file(artifacts / "firmware-SHA256SUMS").read_text() == firmware_text,
            "Firmware checksum manifest differs from metadata")
    return counts


def verify_image(image, allow_uncompressed=False):
    regular_file(image)
    metadata = json.loads(regular_file(image.with_suffix(".json")).read_text())
    target = VERIFY / image.stem
    require(not target.is_symlink() and target.resolve().is_relative_to(PROJECT.resolve()),
            "Verification output escapes project")
    target.mkdir(parents=True, exist_ok=True)
    report = {"image": image.name, "passed": False, "checks": [],
              "physical_board_boot_verified": False, "emulated_boot_verified": False,
              "scope": "Offline regular-file inspection only; no hardware boot was tested."}

    def check(name, action):
        result = {"name": name, "passed": False}
        report["checks"].append(result)
        value = action()
        result["passed"] = True
        return value

    try:
        require(metadata["experimental"] is True and metadata["physical_board_boot_verified"] is False,
                "Image metadata incorrectly claims hardware validation")
        require(metadata.get("boot_mode", "uefi") in ("uefi", "native"), "Unsupported image boot mode")
        require(image.stat().st_size == metadata["image_bytes"], "Image size mismatch")
        checksums = check("artifact_checksums_and_compressed_stream",
                          lambda: verify_checksums(image, metadata, allow_uncompressed))
        report.update(checksums)
        report["retained_input_files"] = check("retained_package_and_repository_input_checksums",
                                               lambda: verify_retained_inputs(metadata, image.parent))
        with image.open("rb") as stream:
            parts = check("protective_mbr_primary_and_backup_gpt_crcs_layout",
                          lambda: verify_gpt(stream, image.stat().st_size, metadata))
            boot, root = target / "boot.fat32", target / "root.ext4"
            check("fat32_read_only_fsck", lambda: extract_partition(stream, parts[0], boot, "fat32"))
            check("ext4_read_only_fsck", lambda: extract_partition(stream, parts[1], root, "ext4"))
        report["fat32"] = check("fat32_volume_identity", lambda: verify_fat_header(boot, metadata))
        boot_info = check("firmware_boot_hashes_kernel_format_paths_and_root",
                          lambda: verify_boot(boot, metadata))
        check("root_uuid_fstab_users_permissions_identities_services_and_manifest",
              lambda: verify_root(root, metadata, boot_info))
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        output_path(target / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    global VERIFY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", type=Path)
    parser.add_argument("--boot-mode", choices=("uefi", "native"),
                        default=os.environ.get("RPI4_BOOT_MODE") or "native",
                        help="Boot profile (default: native; UEFI is deprecated)")
    parser.add_argument("--allow-uncompressed", action="store_true",
                        help="Accept development output built using --skip-compression")
    args = parser.parse_args()
    if args.boot_mode not in ("native", "uefi"):
        parser.error("RPI4_BOOT_MODE must be native or uefi")
    VERIFY = PROJECT / ("build/native/image-verification" if args.boot_mode == "native"
                        else "build/image-verification")
    for tool in ("debugfs", "e2fsck", "fsck.fat", "mtype", "zstd"):
        require(shutil.which(tool), f"Missing host verification tool: {tool}")
    require(not VERIFY.is_symlink() and VERIFY.resolve().is_relative_to(PROJECT.resolve()),
            "Verification directory escapes project")
    VERIFY.mkdir(parents=True, exist_ok=True)
    image = args.image or (ARTIFACTS / "native/rpi4-archlinux-ports-native-sd-usb.img"
                           if args.boot_mode == "native" else ARTIFACTS / UEFI_IMAGE_NAME)
    report = verify_image(image, args.allow_uncompressed)
    output_path(VERIFY / "report.json").write_text(json.dumps([report], indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
