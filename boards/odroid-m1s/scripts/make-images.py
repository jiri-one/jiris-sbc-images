#!/usr/bin/env python3
"""Assemble GPT/ext4 images from the prepared tree, without loop devices.

Run with `podman unshare python3 scripts/make-images.py` so filesystem owners
have target UIDs rather than the host's subordinate UID numbers.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

PROJECT = Path(__file__).resolve().parent.parent
ROOT = PROJECT / "build/rootfs"
ARTIFACTS = PROJECT / "artifacts"
BUILD = PROJECT / "build/images"
MIB = 1024 * 1024
GIB = 1024 * MIB
IMAGE_BYTES = 6 * GIB
BOOT_START = 16 * MIB
BOOT_BYTES = GIB
ROOT_START = BOOT_START + BOOT_BYTES
ROOT_BYTES = IMAGE_BYTES - ROOT_START - MIB
UBOOT_OFFSET = 64 * 512
LINUX_FS = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
ROOT_ARM64 = "B921B045-1DF0-41C3-AF44-4C6F280D3FAE"
DATA_MOUNT_OPTIONS = (
    "defaults,noatime,nofail,x-systemd.automount,"
    "x-systemd.device-timeout=5s,x-systemd.mount-timeout=30s"
)


def run(args, **kwargs):
    print("+", " ".join(str(x) for x in args), flush=True)
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def write_file(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def sparse_copy(source, destination, offset):
    with source.open("rb") as incoming, destination.open("r+b") as outgoing:
        outgoing.seek(offset)
        while chunk := incoming.read(4 * MIB):
            if chunk.count(0) == len(chunk):
                outgoing.seek(len(chunk), os.SEEK_CUR)
            else:
                outgoing.write(chunk)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare_tree():
    if not (PROJECT / "build/package-manifest.txt").is_file():
        raise RuntimeError("Build the root filesystem successfully first.")
    # Refresh the locally owned integration scripts after package installation.
    run(["cp", "-a", str(PROJECT / "overlay") + "/.", ROOT])
    for path in (ROOT / "usr/local/sbin").iterdir():
        path.chmod(0o755)
    (ROOT / "etc/sudoers.d/10-wheel").chmod(0o440)
    (ROOT / "etc/sudoers.d").chmod(0o750)
    ROOT.chmod(0o755)
    # Preserve authenticated inputs for inspection and replay, outside the image.
    cache = PROJECT / "cache/packages"
    cache.mkdir(parents=True, exist_ok=True)
    for path in (ROOT / "var/cache/pacman/pkg").iterdir():
        if path.is_file():
            shutil.copy2(path, cache / path.name)
    dbs = PROJECT / "cache/ports-databases"
    dbs.mkdir(parents=True, exist_ok=True)
    for path in (ROOT / "var/lib/pacman/sync").glob("*.db*"):
        shutil.copy2(path, dbs / path.name)
    for pattern in ("*.pkg.tar.*", "*.part"):
        for path in (ROOT / "var/cache/pacman/pkg").glob(pattern):
            path.unlink()
    # Never clone private pacman master keys or device identities.
    shutil.rmtree(ROOT / "etc/pacman.d/gnupg", ignore_errors=True)
    shutil.rmtree(ROOT / "root/.gnupg", ignore_errors=True)
    for pattern in ("ssh_host_*",):
        for path in (ROOT / "etc/ssh").glob(pattern):
            path.unlink()
    write_file(ROOT / "etc/machine-id", "")
    for relative in (
        "var/lib/dbus/machine-id", "var/lib/systemd/random-seed",
        "var/lib/odroid-m1s/initialized", "var/lib/odroid-m1s/root-expanded",
    ):
        (ROOT / relative).unlink(missing_ok=True)
    (ROOT / "var/lib/dbus/machine-id").symlink_to("/etc/machine-id")
    for parent in (ROOT / "tmp", ROOT / "var/tmp"):
        for path in parent.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    # Only remove generated caches of our own integration script. Distribution
    # Python caches are package-owned files and must remain intact.
    shutil.rmtree(ROOT / "usr/local/lib/odroid-m1s/__pycache__", ignore_errors=True)
    for relative in ("home/odroid/.ssh", "root/.ssh"):
        path = ROOT / relative
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    for user in ("odroid", "root"):
        run(["chroot", ROOT, "/usr/bin/usermod", "--password", "*", user])
    run(["chroot", ROOT, "/usr/bin/chage", "-d", "1", "-M", "-1", "-E", "-1", "odroid"])
    write_file(ROOT / "etc/issue",
               "Arch Linux Ports — ODROID-M1S\n"
               "Login: odroid via SSH with the key configured before flashing.\n\n")
    shutil.copy2(PROJECT / "build/package-manifest.txt",
                 ROOT / "usr/share/odroid-m1s/package-manifest.txt")
    shutil.copytree(PROJECT / "build/uboot/licenses", ROOT / "usr/share/licenses/uboot-m1s",
                    dirs_exist_ok=True)
    # Catch accidental removal of package-owned files during image cleanup.
    with (PROJECT / "build/logs/package-files.log").open("w") as log:
        run(["chroot", ROOT, "/usr/bin/pacman", "-Qk"], stdout=log, stderr=subprocess.STDOUT)
    print("All installed package files are present.", flush=True)


def uuid_string():
    return str(uuid.uuid4())


def make_disk_table(path, byte_size, metadata, boot_bytes=BOOT_BYTES, root_bytes=ROOT_BYTES):
    with path.open("wb") as stream:
        stream.truncate(byte_size)
    spec = (
        "label: gpt\nunit: sectors\n"
        f"label-id: {metadata['disk_uuid']}\n\n"
        f"start={BOOT_START // 512}, size={boot_bytes // 512}, "
        f"type={LINUX_FS}, uuid={metadata['boot_partuuid']}, name=M1S_BOOT, attrs=LegacyBIOSBootable\n"
    )
    if root_bytes:
        spec += (
            f"start={ROOT_START // 512}, size={root_bytes // 512}, "
            f"type={ROOT_ARM64}, uuid={metadata['root_partuuid']}, name=M1S_ROOT\n"
        )
    run(["sfdisk", path], input=spec, text=True)


def boot_metadata(flavour):
    return {
        "flavour": flavour,
        "disk_uuid": uuid_string(),
        "boot_partuuid": uuid_string(),
        "root_partuuid": uuid_string(),
        "boot_fs_uuid": uuid_string(),
        "root_fs_uuid": uuid_string(),
        "image_bytes": IMAGE_BYTES,
        "boot_start_bytes": BOOT_START,
        "boot_size_bytes": BOOT_BYTES,
        "root_start_bytes": ROOT_START,
        "root_size_bytes": ROOT_BYTES,
        "uboot_start_bytes": UBOOT_OFFSET,
    }


def build_flavour(flavour, uboot):
    metadata = boot_metadata(flavour)
    metadata["uboot_sha256"] = checksum(uboot)
    configuration = (
        f"ROOT_PARTUUID={metadata['root_partuuid']}\n"
        f"BOOT_FS_UUID={metadata['boot_fs_uuid']}\n"
        f"IMAGE_FLAVOUR={flavour}\n"
    )
    write_file(ROOT / "etc/odroid-m1s-image.conf", configuration)
    fstab = (
        f"UUID={metadata['root_fs_uuid']} / ext4 defaults,noatime 0 1\n"
        f"UUID={metadata['boot_fs_uuid']} /boot ext4 defaults,noatime 0 2\n"
    )
    if flavour == "sd-emmc":
        # Data storage is opt-in by filesystem label. Never discover, format or
        # grow a second disk automatically; absent data must not block boot.
        data_directory = ROOT / "mnt/nvme"
        data_directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        data_directory.chmod(0o755)
        fstab += (
            "# Optional NVMe data partition; prepare/identify it separately.\n"
            f"LABEL=M1S_DATA /mnt/nvme ext4 {DATA_MOUNT_OPTIONS} 0 2\n"
        )
        metadata["data_mount"] = {
            "source": "LABEL=M1S_DATA", "mountpoint": "/mnt/nvme",
            "filesystem": "ext4", "options": DATA_MOUNT_OPTIONS,
            "automatic_formatting": False,
        }
    write_file(ROOT / "etc/fstab", fstab)
    run(["chroot", ROOT, "/usr/bin/env", "M1S_IMAGE_BUILD=1",
         "/usr/local/sbin/odroid-m1s-update-boot"])
    boot = BUILD / f"{flavour}-boot-tree"
    if boot.exists():
        shutil.rmtree(boot)
    shutil.move(ROOT / "boot", boot)
    (ROOT / "boot").mkdir(mode=0o755)
    # A new image has no old kernel to retain; remove old unused release sets.
    config = (boot / "extlinux/extlinux.conf").read_text()
    metadata["extlinux"] = config
    kernel_path = re.search(r"^    LINUX (\S+)$", config, re.MULTILINE).group(1)
    metadata["kernel_sha256"] = checksum(boot / kernel_path.lstrip("/"))
    boot_fs = BUILD / f"{flavour}-boot.ext4"
    root_fs = BUILD / f"{flavour}-root.ext4"
    try:
        with boot_fs.open("wb") as stream:
            stream.truncate(BOOT_BYTES)
        with root_fs.open("wb") as stream:
            stream.truncate(ROOT_BYTES)
        run(["mkfs.ext4", "-F", "-q", "-m", "0", "-L", "M1S_BOOT",
             "-U", metadata["boot_fs_uuid"], "-O", "^64bit,^metadata_csum,^orphan_file",
             "-d", boot, boot_fs])
        run(["mkfs.ext4", "-F", "-q", "-m", "1", "-L", "M1S_ROOT",
             "-U", metadata["root_fs_uuid"], "-d", ROOT, root_fs])
        run(["e2fsck", "-fn", boot_fs])
        run(["e2fsck", "-fn", root_fs])
        image = ARTIFACTS / f"odroid-m1s-archlinux-ports-{flavour}.img"
        make_disk_table(image, IMAGE_BYTES, metadata)
        sparse_copy(uboot, image, UBOOT_OFFSET)
        sparse_copy(boot_fs, image, BOOT_START)
        sparse_copy(root_fs, image, ROOT_START)
        metadata["image_sha256"] = checksum(image)
        write_file(ARTIFACTS / f"{image.stem}.json", json.dumps(metadata, indent=2) + "\n")
        return image, metadata, boot
    finally:
        (ROOT / "boot").rmdir()
        shutil.move(boot, ROOT / "boot")


def build_nvme_carrier(uboot):
    # The same whole-disk image can reside on eMMC with the SD card removed.
    # SD is scanned before eMMC. Its boot.scr explicitly boots NVMe, so an
    # existing eMMC operating system cannot win after our SD U-Boot starts.
    byte_size = 64 * MIB
    boot_bytes = 32 * MIB
    script = PROJECT / "build/uboot/nvme-boot.scr"
    if not script.is_file():
        raise RuntimeError("Build the NVMe carrier boot.scr before assembling images.")
    tree = BUILD / "nvme-loader-boot-tree"
    tree.mkdir(exist_ok=True)
    shutil.copy2(script, tree / "boot.scr")
    metadata = boot_metadata("nvme-loader")
    metadata.update({"image_bytes": byte_size, "boot_size_bytes": boot_bytes,
                     "root_size_bytes": 0, "uboot_sha256": checksum(uboot),
                     "description": "U-Boot SD/eMMC carrier with boot.scr explicitly loading NVMe partition 1; NVMe OS image required."})
    for key in ("root_partuuid", "root_fs_uuid", "root_start_bytes"):
        metadata.pop(key)
    filesystem = BUILD / "nvme-loader-boot.ext4"
    with filesystem.open("wb") as stream:
        stream.truncate(boot_bytes)
    run(["mkfs.ext4", "-F", "-q", "-m", "0", "-L", "M1S_NVME_LOADER",
         "-U", metadata["boot_fs_uuid"], "-O", "^64bit,^metadata_csum,^orphan_file",
         "-d", tree, filesystem])
    run(["e2fsck", "-fn", filesystem])
    image = ARTIFACTS / "odroid-m1s-nvme-loader-sd.img"
    make_disk_table(image, byte_size, metadata, boot_bytes=boot_bytes, root_bytes=0)
    sparse_copy(uboot, image, UBOOT_OFFSET)
    sparse_copy(filesystem, image, BOOT_START)
    metadata["image_sha256"] = checksum(image)
    write_file(ARTIFACTS / f"{image.stem}.json", json.dumps(metadata, indent=2) + "\n")
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-compression", action="store_true")
    parser.add_argument("--flavour", choices=("sd-emmc", "nvme", "all"),
                        default="sd-emmc", help="default: OS on SD/eMMC with optional NVMe data")
    args = parser.parse_args()
    if os.geteuid() != 0 or Path("/proc/self/uid_map").read_text().split()[1] == "0":
        raise SystemExit("Run in rootless Podman's user namespace, not as host root.")
    for tool in ("chroot", "sfdisk", "mkfs.ext4", "e2fsck", "zstd"):
        if not shutil.which(tool):
            raise SystemExit(f"Missing tool: {tool}")
    uboot = PROJECT / "build/uboot/u-boot-rockchip.bin"
    if not uboot.is_file() or not 0 < uboot.stat().st_size <= BOOT_START - UBOOT_OFFSET:
        raise SystemExit("A valid, nonoverlapping U-Boot binary is required.")
    BUILD.mkdir(parents=True, exist_ok=True)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    prepare_tree()
    images = []
    flavours = ("sd-emmc", "nvme") if args.flavour == "all" else (args.flavour,)
    for flavour in flavours:
        image, _, _ = build_flavour(flavour, uboot)
        images.append(image)
    if args.flavour in ("nvme", "all"):
        images.append(build_nvme_carrier(uboot))
    shutil.copy2(PROJECT / "build/package-manifest.txt", ARTIFACTS / "package-manifest.txt")
    files = []
    for image in images:
        files.append(image)
        if not args.skip_compression:
            run(["zstd", "-T4", "-10", "--force", image, "-o", f"{image}.zst"])
            files.append(Path(f"{image}.zst"))
    write_file(ARTIFACTS / "SHA256SUMS",
               "".join(f"{checksum(p)}  {p.name}\n" for p in files))
    write_file(ARTIFACTS / "package-cache-SHA256SUMS",
               "".join(f"{checksum(p)}  {p.name}\n" for p in sorted((PROJECT / "cache/packages").iterdir()) if p.is_file()))
    print("Configure your SSH public key with scripts/configure-image.py from the repository root before flashing.")


if __name__ == "__main__":
    main()
