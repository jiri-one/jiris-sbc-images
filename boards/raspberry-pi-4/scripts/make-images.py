#!/usr/bin/env python3
"""Build the experimental RPi4 image using regular files, never block devices.

Run: podman unshare python3 scripts/make-images.py
The prepared rootfs and firmware must already exist under build/.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import uuid

PROJECT = Path(__file__).resolve().parent.parent
ROOT = PROJECT / "build/native/rootfs"
FIRMWARE = PROJECT / "build/firmware"
ARTIFACTS = PROJECT / "artifacts/native"
BUILD = PROJECT / "build/native/images"
MIB = 1024 * 1024
GIB = 1024 * MIB
IMAGE_BYTES = 8 * GIB
BOOT_START = MIB
BOOT_BYTES = GIB
ROOT_START = BOOT_START + BOOT_BYTES
ROOT_BYTES = IMAGE_BYTES - ROOT_START - MIB
ESP_TYPE = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
ROOT_ARM64 = "B921B045-1DF0-41C3-AF44-4C6F280D3FAE"
IMAGE_NAME = "rpi4-archlinux-ports-native-sd-usb.img"
BOOT_MODE = "native"


def build_directory():
    return PROJECT / ("build/native" if BOOT_MODE == "native" else "build")


def configure_mode(mode):
    global BOOT_MODE, ROOT, ARTIFACTS, BUILD, IMAGE_NAME
    if mode not in ("uefi", "native"):
        raise ValueError("Boot mode must be uefi or native")
    BOOT_MODE = mode
    ROOT = build_directory() / "rootfs"
    BUILD = build_directory() / "images"
    ARTIFACTS = PROJECT / ("artifacts/native" if mode == "native" else "artifacts")
    IMAGE_NAME = ("rpi4-archlinux-ports-native-sd-usb.img" if mode == "native"
                  else "rpi4-archlinux-ports-sd-usb.img")


def run(args, **kwargs):
    print("+", " ".join(str(arg) for arg in args), flush=True)
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def safe_path(path, base=None):
    """Reject symlink escapes, including an existing output symlink."""
    path, base = Path(path), Path(base) if base is not None else PROJECT
    if path.is_symlink():
        raise ValueError(f"Refusing symlink: {path}")
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError as error:
        raise ValueError(f"Path escapes build tree: {path}") from error
    return path


def regular_file(path, base=None):
    path = safe_path(path, base)
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"Expected a regular file: {path}")
    return path


def output_file(path):
    path = safe_path(path)
    safe_path(path.parent).mkdir(parents=True, exist_ok=True)
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"Refusing nonregular or hardlinked output: {path}")
    return path


def write_file(path, value, mode=0o644):
    path = output_file(path)
    with path.open("w") as stream:
        stream.write(value)
    path.chmod(mode)


def sparse_file(path, size):
    path = output_file(path)
    with path.open("wb") as stream:
        stream.truncate(size)


def checksum(path):
    with regular_file(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sparse_copy(source, destination, offset):
    source, destination = regular_file(source), regular_file(destination)
    if offset < 0 or offset + source.stat().st_size > destination.stat().st_size:
        raise ValueError("Partition copy exceeds the regular image file")
    if destination.stat().st_nlink != 1:
        raise ValueError("Refusing hardlinked output image")
    with source.open("rb") as incoming, destination.open("r+b") as outgoing:
        outgoing.seek(offset)
        while chunk := incoming.read(4 * MIB):
            if chunk.count(0) == len(chunk):
                outgoing.seek(len(chunk), os.SEEK_CUR)
            else:
                outgoing.write(chunk)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def require_user_namespace():
    rows = [tuple(map(int, line.split()))
            for line in Path("/proc/self/uid_map").read_text().splitlines()]
    if os.geteuid() != 0 or not rows or rows[0][0] != 0 or rows[0][1] == 0:
        raise RuntimeError("Run with rootless podman unshare, never as host root.")
    if any(outside == 0 for _, outside, _ in rows):
        raise RuntimeError("A namespace mapping host root is not allowed.")


def remove_generated(path):
    """Delete an owned generated path without following its leaf symlink."""
    safe_path(path.parent)
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        safe_path(path)
        if path.is_dir():
            if path.is_mount():
                raise ValueError(f"Refusing to remove mounted directory: {path}")
            # Do not traverse a mounted directory nested below a cleanup target.
            for directory, dirs, _ in os.walk(path, followlinks=False):
                if any((Path(directory) / name).is_mount() for name in dirs):
                    raise ValueError(f"Mount inside cleanup target: {path}")
            shutil.rmtree(path)
        else:
            mode = path.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode)):
                raise ValueError(f"Refusing unexpected device in cleanup tree: {path}")
            path.unlink()


def copy_regular(source, destination):
    regular_file(source)
    output_file(destination)
    shutil.copy2(source, destination)


def tree_hashes(tree):
    """FAT32 cannot represent symlinks or case-colliding Unix file names."""
    safe_path(tree)
    seen, files = set(), {}
    for path in sorted(tree.rglob("*")):
        safe_path(path, tree)
        relative = path.relative_to(tree).as_posix()
        if relative.casefold() in seen:
            raise ValueError(f"Case-colliding FAT path: {relative}")
        seen.add(relative.casefold())
        if path.is_file():
            files[relative] = checksum(path)
        elif not path.is_dir():
            raise ValueError(f"Unsupported boot-tree object: {path}")
    return files


def preserve_inputs():
    for source, destination, pattern in (
        (ROOT / "var/cache/pacman/pkg", PROJECT / "cache/packages", "*"),
        (ROOT / "var/lib/pacman/sync", PROJECT / "cache/ports-databases", "*.db*"),
    ):
        safe_path(source)
        safe_path(destination).mkdir(parents=True, exist_ok=True)
        for path in sorted(source.glob(pattern)):
            if path.is_file() and not path.is_symlink():
                copy_regular(path, destination / path.name)
    for pattern in ("*.pkg.tar.*", "*.part"):
        for path in (ROOT / "var/cache/pacman/pkg").glob(pattern):
            remove_generated(path)


def prepare_tree():
    safe_path(ROOT)
    regular_file(build_directory() / "package-manifest.txt")
    for relative in ("etc", "boot", "usr/local/sbin", "var/cache/pacman/pkg"):
        if not safe_path(ROOT / relative, ROOT).is_dir():
            raise RuntimeError(f"Missing prepared rootfs directory: {relative}")
    # Refresh locally maintained files after package setup.
    overlay = safe_path(PROJECT / "overlay")
    for source in sorted(overlay.rglob("*")):
        relative = source.relative_to(overlay)
        if "__pycache__" in relative.parts or source.suffix == ".pyc":
            continue
        safe_path(source, overlay)
        destination = ROOT / relative
        if source.is_dir():
            safe_path(destination, ROOT).mkdir(parents=True, exist_ok=True)
        else:
            copy_regular(source, destination)
    if BOOT_MODE == "native":
        copy_regular(PROJECT / "configs/native/mkinitcpio.conf", ROOT / "etc/mkinitcpio.conf")
        copy_regular(PROJECT / "configs/native/modules-load.conf",
                     ROOT / "etc/modules-load.d/rpi4-display.conf")
    marker = ROOT / "usr/share/rpi4/initramfs-boot-mode"
    if BOOT_MODE == "native" or marker.exists():
        if regular_file(marker).read_text().strip() != BOOT_MODE:
            raise ValueError("Prepared initramfs boot mode differs; rerun scripts/build-rootfs.sh")
    for path in (ROOT / "usr/local/sbin").iterdir():
        regular_file(path).chmod(0o755)
    regular_file(ROOT / "etc/sudoers.d/10-wheel").chmod(0o440)
    safe_path(ROOT / "etc/sudoers.d", ROOT).chmod(0o750)
    ROOT.chmod(0o755)
    preserve_inputs()
    # Keep the directories themselves: they may appear in package file lists.
    for relative in ("etc/pacman.d/gnupg", "root/.gnupg", "tmp", "var/tmp",
                     "var/log/journal", "var/lib/rpi4", "var/lib/systemd/rfkill"):
        directory = safe_path(ROOT / relative, ROOT)
        if directory.exists():
            for child in directory.iterdir():
                remove_generated(child)
    for key in (ROOT / "etc/ssh").glob("ssh_host_*"):
        remove_generated(key)
    for relative in ("home/rpi/.ssh", "root/.ssh"):
        remove_generated(ROOT / relative)
    for relative in ("etc/machine-id", "var/lib/dbus/machine-id",
                     "var/lib/systemd/random-seed", "var/lib/systemd/credential.secret",
                     "usr/local/lib/rpi4/__pycache__"):
        remove_generated(ROOT / relative)
    write_file(ROOT / "etc/machine-id", "")
    write_file(ROOT / "etc/hostname", "rpi4\n")
    safe_path(ROOT / "var/lib/dbus", ROOT).mkdir(parents=True, exist_ok=True)
    (ROOT / "var/lib/dbus/machine-id").symlink_to("/etc/machine-id")
    for user in ("rpi", "root"):
        run(["chroot", ROOT, "/usr/bin/usermod", "--password", "*", user])
    run(["chroot", ROOT, "/usr/bin/chage", "-d", "1", "-M", "-1", "-E", "-1", "rpi"])
    write_file(ROOT / "etc/issue",
               "Arch Linux Ports — Raspberry Pi 4 experimental image\n"
               "Login: rpi via SSH with the key configured before flashing.\n\n")
    destination = ROOT / "usr/share/rpi4/package-manifest.txt"
    safe_path(destination.parent, ROOT).mkdir(parents=True, exist_ok=True)
    copy_regular(build_directory() / "package-manifest.txt", destination)


def boot_metadata():
    volume_id = secrets.token_hex(4).upper()
    return {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "board": "Raspberry Pi 4 Model B",
        "architecture": "aarch64",
        "cpu_baseline": "ARMv8-A",
        "flavour": "sd-usb",
        "boot_mode": BOOT_MODE,
        "experimental": True,
        "physical_board_boot_verified": False,
        "disk_uuid": str(uuid.uuid4()),
        "boot_partuuid": str(uuid.uuid4()),
        "root_partuuid": str(uuid.uuid4()),
        "boot_volume_id": volume_id,
        "boot_fs_uuid": f"{volume_id[:4]}-{volume_id[4:]}",
        "root_fs_uuid": str(uuid.uuid4()),
        "image_bytes": IMAGE_BYTES,
        "boot_start_bytes": BOOT_START,
        "boot_size_bytes": BOOT_BYTES,
        "root_start_bytes": ROOT_START,
        "root_size_bytes": ROOT_BYTES,
        "firmware": ({"project": "raspberrypi/firmware"} if BOOT_MODE == "native"
                     else {"project": "pftf/RPi4", "version": "v1.53"}),
    }


def make_disk_table(path, metadata):
    sparse_file(path, IMAGE_BYTES)
    specification = (
        "label: gpt\nunit: sectors\n"
        f"label-id: {metadata['disk_uuid']}\n\n"
        f"start={BOOT_START // 512}, size={BOOT_BYTES // 512}, "
        f"type={ESP_TYPE}, uuid={metadata['boot_partuuid']}, name=RPI4_BOOT\n"
        f"start={ROOT_START // 512}, size={ROOT_BYTES // 512}, "
        f"type={ROOT_ARM64}, uuid={metadata['root_partuuid']}, name=RPI4_ROOT\n"
    )
    run(["sfdisk", "--no-reread", "--no-tell-kernel", path], input=specification, text=True)


def install_firmware(runtime_firmware):
    if set(runtime_firmware["files"]) != {"start4.elf", "fixup4.dat"}:
        raise ValueError("Runtime firmware provenance must contain the matched start4/fixup4 pair")
    for name in ("start4.elf", "fixup4.dat"):
        regular_file(FIRMWARE / name, FIRMWARE)
    for name, details in runtime_firmware["files"].items():
        if checksum(FIRMWARE / name) != details["sha256"]:
            raise ValueError(f"Staged runtime firmware changed: {name}; rerun scripts/fetch-inputs.sh")
    if BOOT_MODE == "native":
        return install_native_firmware(runtime_firmware)
    for name in ("config.txt", "RPI_EFI.fd"):
        regular_file(FIRMWARE / name, FIRMWARE)
    hashes = tree_hashes(FIRMWARE)
    # Configure only the installed copy; retain the verified upstream inputs.
    config = (FIRMWARE / "config.txt").read_bytes().splitlines(keepends=True)
    upstream = [index for index, line in enumerate(config)
                if line.strip() == b"dtoverlay=upstream-pi4"]
    if len(upstream) != 1:
        raise ValueError("Expected exactly one upstream-pi4 overlay in firmware config.txt")
    index = upstream[0]
    newline = b"\r\n" if config[index].endswith(b"\r\n") else b"\n"
    config[index] = newline.join((
        config[index].rstrip(b"\r\n"),
        b"dtoverlay=rpi4-usb-c-data-disable",
        b"max_framebuffer_width=4096",
        b"max_framebuffer_height=2160",
        b"",
    ))
    source = regular_file(PROJECT / "configs/overlays/rpi4-usb-c-data-disable-overlay.dts")
    overlay = output_file(PROJECT / "build/overlays/rpi4-usb-c-data-disable.dtbo")
    run(["dtc", "-@", "-I", "dts", "-O", "dtb", "-o", overlay, source])
    for path in sorted(FIRMWARE.rglob("*")):
        destination = ROOT / "boot" / path.relative_to(FIRMWARE)
        if path.is_dir():
            safe_path(destination, ROOT).mkdir(parents=True, exist_ok=True)
        else:
            copy_regular(path, destination)
    write_file(ROOT / "boot/config.txt", b"".join(config).decode("ascii"))
    installed_overlay = ROOT / "boot/overlays" / overlay.name
    copy_regular(overlay, installed_overlay)
    hashes["config.txt"] = checksum(ROOT / "boot/config.txt")
    hashes["overlays/" + overlay.name] = checksum(installed_overlay)
    return hashes


def install_native_firmware(runtime_firmware):
    config = regular_file(PROJECT / "configs/native/config.txt")
    license_file = regular_file(PROJECT / "overlay/usr/share/licenses/rpi4-uefi-firmware/RaspberryPi-LICENCE.broadcom")
    source = regular_file(PROJECT / "configs/overlays/rpi4-usb-c-data-disable-overlay.dts")
    overlay = output_file(build_directory() / "overlays/rpi4-usb-c-data-disable.dtbo")
    run(["dtc", "-@", "-I", "dts", "-O", "dtb", "-o", overlay, source])
    regular_file(ROOT / "boot/initramfs-linux.img")
    # A cloned rootfs may contain an old selector and device identifiers.
    # Keep only the freshly generated initramfs; the updater recreates the rest.
    for child in (ROOT / "boot").iterdir():
        if child.name != "initramfs-linux.img":
            remove_generated(child)
    for name in ("start4.elf", "fixup4.dat"):
        copy_regular(FIRMWARE / name, ROOT / "boot" / name)
    copy_regular(config, ROOT / "boot/config.txt")
    copy_regular(license_file, ROOT / "boot/LICENCE.broadcom")
    copy_regular(overlay, ROOT / "boot/overlays" / overlay.name)
    manifest = {"version": runtime_firmware["version"], "commit": runtime_firmware["commit"],
                "sha256": {name: details["sha256"] for name, details in runtime_firmware["files"].items()}}
    write_file(ROOT / "usr/share/rpi4/runtime-firmware.json", json.dumps(manifest, indent=2) + "\n")
    return {name: checksum(ROOT / "boot" / name) for name in
            ("start4.elf", "fixup4.dat", "config.txt", "LICENCE.broadcom",
             "overlays/rpi4-usb-c-data-disable.dtbo")}


def package_file_check():
    log = output_file(build_directory() / "logs/package-files.log")
    with log.open("w") as stream:
        run(["chroot", ROOT, "/usr/bin/pacman", "-Qk"],
            stdout=stream, stderr=subprocess.STDOUT)
    return log


def build_image():
    metadata = boot_metadata()
    inputs = json.loads(regular_file(ARTIFACTS / "input-manifest.json").read_text())
    runtime_firmware = inputs["runtime_firmware"]
    if BOOT_MODE == "native":
        metadata["firmware"]["version"] = runtime_firmware["version"]
    metadata["firmware"]["runtime_firmware"] = runtime_firmware
    metadata["firmware"]["files_sha256"] = install_firmware(runtime_firmware)
    write_file(ROOT / "etc/rpi4-image.conf",
               f"ROOT_PARTUUID={metadata['root_partuuid']}\n"
               f"BOOT_FS_UUID={metadata['boot_fs_uuid']}\n"
               "IMAGE_FLAVOUR=sd-usb\n"
               f"RPI4_BOOT_MODE={BOOT_MODE}\n")
    write_file(ROOT / "etc/fstab",
               f"UUID={metadata['root_fs_uuid']} / ext4 defaults,noatime 0 1\n"
               f"UUID={metadata['boot_fs_uuid']} /boot vfat defaults,noatime,umask=0077 0 2\n")
    run(["chroot", ROOT, "/usr/bin/env", "RPI4_IMAGE_BUILD=1",
         "/usr/local/sbin/rpi4-update-boot"])
    log = package_file_check()
    metadata["package_file_check"] = {"passed": True, "log_sha256": checksum(log)}
    metadata["package_manifest_sha256"] = checksum(build_directory() / "package-manifest.txt")
    metadata["input_manifest_sha256"] = checksum(ARTIFACTS / "input-manifest.json")
    metadata["tools_manifest_sha256"] = checksum(PROJECT / "build/logs/tools-package-manifest.txt")
    metadata["boot_files_sha256"] = tree_hashes(ROOT / "boot")
    required_files = (("native-current.txt", "config.txt", "start4.elf", "fixup4.dat")
                      if BOOT_MODE == "native" else
                      ("EFI/BOOT/BOOTAA64.EFI", "EFI/systemd/systemd-bootaa64.efi",
                       "loader/loader.conf", "loader/entries/rpi4.conf"))
    for required in required_files:
        if required not in metadata["boot_files_sha256"]:
            raise RuntimeError(f"Boot helper did not produce {required}")
    if BOOT_MODE == "native":
        metadata["native_selector"] = (ROOT / "boot/native-current.txt").read_text()
    else:
        metadata["loader_entry"] = (ROOT / "boot/loader/entries/rpi4.conf").read_text()
    boot = safe_path(BUILD / "sd-usb-boot-tree")
    if boot.exists():
        raise RuntimeError(f"Unrestored boot staging tree exists: {boot}")
    bootfs, rootfs = BUILD / "sd-usb-boot.fat32", BUILD / "sd-usb-root.ext4"
    image = ARTIFACTS / IMAGE_NAME
    (ROOT / "boot").rename(boot)
    try:
        (ROOT / "boot").mkdir(mode=0o755)
        sparse_file(bootfs, BOOT_BYTES)
        run(["mkfs.vfat", "-F", "32", "-S", "512", "-n", "RPI4_BOOT", "-i",
             metadata["boot_volume_id"], bootfs])
        for child in sorted(boot.iterdir()):
            run(["mcopy", "-s", "-i", bootfs, child, "::/"])
        sparse_file(rootfs, ROOT_BYTES)
        run(["mkfs.ext4", "-F", "-q", "-m", "1", "-L", "RPI4_ROOT",
             "-U", metadata["root_fs_uuid"], "-d", ROOT, rootfs])
        run(["fsck.fat", "-n", bootfs])
        run(["e2fsck", "-fn", rootfs])
        make_disk_table(image, metadata)
        sparse_copy(bootfs, image, BOOT_START)
        sparse_copy(rootfs, image, ROOT_START)
        metadata["image_sha256"] = checksum(image)
    finally:
        if (ROOT / "boot").exists():
            (ROOT / "boot").rmdir()
        boot.rename(ROOT / "boot")
    return image, metadata, log


def hash_list(directory):
    return "".join(f"{checksum(path)}  {path.name}\n"
                   for path in sorted(directory.iterdir()) if path.is_file())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boot-mode", choices=("uefi", "native"),
                        default=os.environ.get("RPI4_BOOT_MODE") or "native",
                        help="Boot profile (default: native; UEFI is deprecated)")
    parser.add_argument("--skip-compression", action="store_true",
                        help="Development only; normal deliverables include .img.zst")
    args = parser.parse_args()
    if args.boot_mode not in ("native", "uefi"):
        parser.error("RPI4_BOOT_MODE must be native or uefi")
    configure_mode(args.boot_mode)
    require_user_namespace()
    required = ["chroot", "sfdisk", "mkfs.vfat", "mcopy", "mkfs.ext4", "e2fsck", "fsck.fat", "dtc"]
    if not args.skip_compression:
        required.append("zstd")
    for tool in required:
        if not shutil.which(tool):
            raise RuntimeError(f"Missing host tool: {tool}")
    for path in (BUILD, ARTIFACTS):
        safe_path(path).mkdir(parents=True, exist_ok=True)
    # Recover a boot directory after an interrupted prior assembly before mutation.
    staged = safe_path(BUILD / "sd-usb-boot-tree")
    if staged.exists():
        boot = safe_path(ROOT / "boot", ROOT)
        if boot.exists() and any(boot.iterdir()):
            raise RuntimeError("Both rootfs/boot and boot staging contain files; inspect them first.")
        if boot.exists():
            boot.rmdir()
        staged.rename(boot)
    prepare_tree()
    image, metadata, log = build_image()
    files = [image]
    if not args.skip_compression:
        compressed = output_file(Path(f"{image}.zst"))
        run(["zstd", "-T4", "-10", "--force", image, "-o", compressed])
        metadata["compressed_sha256"] = checksum(compressed)
        files.append(compressed)
    files.append(regular_file(ARTIFACTS / "input-manifest.json"))
    for source, name in ((build_directory() / "package-manifest.txt", "package-manifest.txt"),
                         (PROJECT / "build/logs/tools-package-manifest.txt", "tools-package-manifest.txt"),
                         (log, "package-files.log")):
        destination = ARTIFACTS / name
        copy_regular(source, destination)
        files.append(destination)
    for directory, name in ((PROJECT / "cache/packages", "package-cache-SHA256SUMS"),
                            (PROJECT / "cache/ports-databases", "repository-db-SHA256SUMS")):
        output = ARTIFACTS / name
        write_file(output, hash_list(directory))
        files.append(output)
    firmware_hashes = ARTIFACTS / "firmware-SHA256SUMS"
    write_file(firmware_hashes, "".join(f"{value}  {name}\n" for name, value in
                                      metadata["firmware"]["files_sha256"].items()))
    files.append(firmware_hashes)
    metadata_path = image.with_suffix(".json")
    write_file(metadata_path, json.dumps(metadata, indent=2) + "\n")
    files.append(metadata_path)
    write_file(ARTIFACTS / "SHA256SUMS",
               "".join(f"{checksum(path)}  {path.name}\n" for path in files))
    print("Experimental image assembled; no physical Raspberry Pi boot was tested.")
    print("Configure your SSH public key with scripts/configure-image.py from the repository root before flashing.")


if __name__ == "__main__":
    main()
