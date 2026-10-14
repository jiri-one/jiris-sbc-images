#!/usr/bin/env bash
# Boot the released disk through an expendable qcow2 overlay on QEMU's virt board.
set -euo pipefail
cd "$(dirname "$(realpath "$0")")/.."
exec python3 - "$@" <<'PY'
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shlex
import socket
import subprocess
import tempfile
import time
import uuid

parser = argparse.ArgumentParser(description="Boot an M1S image on QEMU virt without modifying it.")
parser.add_argument("image", type=Path, help="uncompressed raw disk image")
parser.add_argument("--container", help="prepared rootless container; see README.md")
parser.add_argument("--timeout", type=int, default=600, help="boot and verification timeout in seconds")
parser.add_argument("--data-disk", choices=("absent", "present", "wrong-label"), default="absent",
                    help="optional synthetic NVMe data disk scenario (default: absent)")
args = parser.parse_args()
repo = Path.cwd()
source = args.image.resolve(strict=True)
if not source.is_file() or args.timeout < 30:
    raise SystemExit("Expected a regular raw image file and a timeout of at least 30 seconds.")
build = repo / "build"
build.mkdir(exist_ok=True)
run_dir = Path(tempfile.mkdtemp(prefix="qemu-test-", dir=build))
print(f"QEMU verification artifacts: {run_dir}", flush=True)

def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()

def tool_path(path, readonly=False):
    path = path.resolve()
    if not args.container:
        return str(path)
    if not readonly and path.is_relative_to(build):
        return str(Path("/testbuild") / path.relative_to(build))
    if path.is_relative_to(repo):
        return str(Path("/work") / path.relative_to(repo))
    raise ValueError("Container verification requires the image to be inside this repository.")

runner = ["podman", "exec", "-i", args.container] if args.container else []
before_hash = sha256(source)
layout = json.loads(subprocess.check_output(["sfdisk", "--json", str(source)]))["partitiontable"]
partitions = layout["partitions"]
if layout["label"] != "gpt" or len(partitions) != 2:
    raise SystemExit("Expected the image's two-partition GPT layout.")
sector_size = layout.get("sectorsize", 512)
boot_partition = partitions[0]
root_partuuid = partitions[1]["uuid"].lower()
original_root_bytes = partitions[1]["size"] * sector_size
bootfs = run_dir / "boot.ext4"
remaining = boot_partition["size"] * sector_size
with source.open("rb") as disk, bootfs.open("wb") as output:
    disk.seek(boot_partition["start"] * sector_size)
    while remaining:
        chunk = disk.read(min(1024 * 1024, remaining))
        if not chunk:
            raise SystemExit("Truncated boot partition.")
        output.write(chunk)
        remaining -= len(chunk)

def dump_boot_file(path, destination):
    if not re.fullmatch(r"/[A-Za-z0-9/_.+\-]+", path):
        raise ValueError(f"Unexpected boot file path: {path!r}")
    quoted_destination = '"' + str(destination).replace('\\', '\\\\').replace('"', '\\"') + '"'
    result = subprocess.run(
        ["debugfs", "-R", f"dump {path} {quoted_destination}", str(bootfs)],
        capture_output=True, text=True, check=True,
    )
    if not destination.is_file() or destination.stat().st_size == 0:
        raise ValueError(f"Unable to read {path} from image: {result.stderr}")

config_path = run_dir / "extlinux.conf"
dump_boot_file("/extlinux/extlinux.conf", config_path)
config = config_path.read_text()
kernel_match = re.search(r"^\s*LINUX\s+(\S+)\s*$", config, re.MULTILINE)
initrd_match = re.search(r"^\s*INITRD\s+(\S+)\s*$", config, re.MULTILINE)
root_match = re.search(r"\broot=PARTUUID=([a-fA-F0-9-]+)", config)
if not kernel_match or not initrd_match or not root_match or root_match[1].lower() != root_partuuid:
    raise SystemExit("Extlinux kernel/initramfs paths or root PARTUUID do not match the disk.")
kernel, initrd = run_dir / "Image", run_dir / "initramfs-linux.img"
dump_boot_file(kernel_match[1], kernel)
dump_boot_file(initrd_match[1], initrd)
version_file = run_dir / "kernel-version"
dump_boot_file(str(Path(kernel_match[1]).parent / "kernel-version"), version_file)
kernel_version = version_file.read_text().strip()
if not re.fullmatch(r"[0-9A-Za-z._+\-]+", kernel_version):
    raise SystemExit("Invalid kernel version in the image boot set.")
with kernel.open("rb") as stream:
    if stream.read(64)[56:60] != b"ARM\x64":
        raise SystemExit("Image boot set does not contain a raw ARM64 kernel.")

data_disk = None
data_snapshot = None
data_uuid = None
data_label = "M1S_DATA" if args.data_disk == "present" else "UNRELATED_DATA"
data_start = 2048
data_sectors = 262144  # 128 MiB partition on a 512 MiB disk, leaving room to detect accidental growth.
data_marker = None
data_original_hash = None
data_geometry_before = None

def disk_geometry(path):
    table = json.loads(subprocess.check_output(["sfdisk", "--json", str(path)]))["partitiontable"]
    return [{key: part.get(key) for key in ("start", "size", "type", "uuid")}
            for part in table["partitions"]]

if args.data_disk != "absent":
    # Every path here is a newly created regular file under this test run.
    # No physical block device is opened, formatted, or attached to the guest.
    data_uuid = str(uuid.uuid4())
    data_marker = f"preserve-existing-data:{data_uuid}"
    seed = run_dir / "data-seed"
    seed.mkdir()
    (seed / "preserved-marker.txt").write_text(data_marker + "\n")
    datafs = run_dir / "data.ext4"
    with datafs.open("xb") as stream:
        stream.truncate(data_sectors * 512)
    subprocess.run(["mke2fs", "-q", "-F", "-t", "ext4", "-b", "4096", "-L", data_label,
                    "-U", data_uuid, "-E", "lazy_itable_init=0,lazy_journal_init=0,root_owner=0:0",
                    "-d", str(seed), str(datafs)], check=True)
    data_disk = run_dir / "data-original.raw"
    with data_disk.open("xb") as stream:
        stream.truncate(512 * 1024**2)
    subprocess.run(["sfdisk", "--quiet", str(data_disk)], text=True,
                   input=f"label: gpt\nunit: sectors\nstart={data_start}, size={data_sectors}, type=L\n",
                   check=True, capture_output=True)
    with datafs.open("rb") as filesystem, data_disk.open("r+b") as disk:
        disk.seek(data_start * 512)
        while chunk := filesystem.read(1024 * 1024):
            disk.write(chunk)
    data_geometry_before = disk_geometry(data_disk)
    data_original_hash = sha256(data_disk)
    data_disk.chmod(0o444)
    data_snapshot = run_dir / "data-runtime.qcow2"
    subprocess.run(runner + ["qemu-img", "create", "-q", "-f", "qcow2", "-F", "raw",
                            "-b", tool_path(data_disk, readonly=True), tool_path(data_snapshot)], check=True)

snapshot = run_dir / "runtime.qcow2"
subprocess.run(runner + ["qemu-img", "create", "-q", "-f", "qcow2", "-F", "raw",
                       "-b", tool_path(source, readonly=True), tool_path(snapshot)], check=True)
snapshot_bytes = max(8 * 1024**3, source.stat().st_size + 2 * 1024**3)
subprocess.run(runner + ["qemu-img", "resize", "-q", tool_path(snapshot), str(snapshot_bytes)], check=True)
version = subprocess.check_output(runner + ["qemu-system-aarch64", "--version"], text=True).splitlines()[0]
# An NVMe device exercises the board's unchanged root growth service as well as
# its root filesystem. QEMU creates its own virt DTB; the Rockchip DTB is not used.
command_line = (
    f"root=PARTUUID={root_partuuid} rw rootwait console=ttyAMA0,115200n8 "
    "systemd.debug_shell=ttyAMA0 systemd.mask=serial-getty@ttyAMA0.service "
    "systemd.show_status=true"
)
command = runner + [
    "timeout", "--signal=TERM", "--kill-after=5", str(args.timeout + 30),
    "qemu-system-aarch64", "-machine", "virt,gic-version=3", "-cpu", "max",
    "-accel", "tcg,thread=multi", "-smp", "2", "-m", "2048",
    "-kernel", tool_path(kernel), "-initrd", tool_path(initrd), "-append", command_line,
    "-drive", f"if=none,id=rootdisk,format=qcow2,file={tool_path(snapshot)}",
    "-device", "nvme,drive=rootdisk,serial=arch-m1s-qemu",
    "-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0,romfile=",
    "-display", "none", "-monitor", "none", "-serial", "stdio", "-no-reboot",
    "-qmp", f"unix:{tool_path(run_dir / 'qmp.sock')},server=on,wait=off",
]
if data_snapshot is not None:
    command += ["-drive", f"if=none,id=datadisk,format=qcow2,file={tool_path(data_snapshot)}",
                "-device", "nvme,drive=datadisk,serial=arch-m1s-data-test"]
if "," in str(snapshot):
    raise SystemExit("QEMU verification paths must not contain commas.")

data_checks = """# Check the automount without looking up a child of /mnt/nvme.
systemctl is-active --quiet mnt-nvme.automount
if findmnt --nocanonicalize --noheadings --raw --types ext4 --mountpoint /mnt/nvme; then
    echo 'Data filesystem mounted before its first requested access.' >&2
    exit 1
fi
"""
if args.data_disk != "absent":
    data_checks += f"""data_device=$(readlink -f /dev/disk/by-uuid/{data_uuid})
[[ -b $data_device && $data_device != "$root_device" ]]
[[ $(lsblk -dn -o PKNAME "$data_device") != $(lsblk -dn -o PKNAME "$root_device") ]]
[[ $(blkid -s LABEL -o value "$data_device") == {data_label} ]]
[[ $(cat /sys/class/block/"$(basename "$data_device")"/start) == {data_start} ]]
[[ $(cat /sys/class/block/"$(basename "$data_device")"/size) == {data_sectors} ]]
"""
if args.data_disk == "present":
    data_checks += f"""[[ $(timeout 45 cat /mnt/nvme/preserved-marker.txt) == {shlex.quote(data_marker)} ]]
[[ $(findmnt --nocanonicalize -rn -o FSTYPE --types ext4 --mountpoint /mnt/nvme) == ext4 ]]
[[ $(findmnt --nocanonicalize -rn -o UUID --types ext4 --mountpoint /mnt/nvme) == {data_uuid} ]]
[[ $(findmnt --nocanonicalize -rn -o SOURCE --types ext4 --mountpoint /mnt/nvme) == "$data_device" ]]
printf '%s\\n' 'write-read-check:{data_uuid}' > /mnt/nvme/runtime-write.txt
[[ $(cat /mnt/nvme/runtime-write.txt) == 'write-read-check:{data_uuid}' ]]
[[ $(cat /mnt/nvme/preserved-marker.txt) == {shlex.quote(data_marker)} ]]
sync -f /mnt/nvme
printf 'M1S_QEMU_DATA_PRESENT device=%s uuid=%s marker_preserved=yes write_read=yes\\n' "$data_device" {data_uuid}
"""
else:
    data_checks += """[[ ! -e /dev/disk/by-label/M1S_DATA ]]
data_access_status=0
timeout 20 cat /mnt/nvme/preserved-marker.txt > /run/m1s-data-access.log 2>&1 || data_access_status=$?
(( data_access_status != 0 && data_access_status != 124 && data_access_status != 137 ))
if findmnt --nocanonicalize --noheadings --raw --types ext4 --mountpoint /mnt/nvme; then
    echo 'An absent or unrelated data filesystem was claimed.' >&2
    exit 1
fi
systemctl is-active --quiet mnt-nvme.automount
# A deliberately failed optional mount can leave systemd degraded. Core
# services must remain active; do not hide the optional failure by resetting it.
for unit in multi-user.target sshd.service systemd-networkd.service systemd-resolved.service; do
    systemctl is-active --quiet "$unit"
done
exec 4<>/dev/tcp/127.0.0.1/22
IFS= read -r -t 10 ssh_after_data_access <&4
[[ $ssh_after_data_access == SSH-2.0-* ]]
exec 4<&- 4>&-
"""
    data_checks += f"printf 'M1S_QEMU_DATA_UNAVAILABLE mode={args.data_disk} access_status=%s core_services=active ssh=responding\\n' \"$data_access_status\"\n"
if args.data_disk != "absent":
    data_checks += f"""[[ $(blkid -s UUID -o value "$data_device") == {data_uuid} ]]
[[ $(blkid -s LABEL -o value "$data_device") == {data_label} ]]
[[ $(cat /sys/class/block/"$(basename "$data_device")"/start) == {data_start} ]]
[[ $(cat /sys/class/block/"$(basename "$data_device")"/size) == {data_sectors} ]]
"""
data_checks += "printf 'M1S_QEMU_STATE_AFTER_DATA_ACCESS %s\\n' \"$(systemctl is-system-running || true)\"\n"
guest_checks = f"""#!/bin/bash
set -euo pipefail
finish() {{
    result=$?
    trap - EXIT
    if (( result == 0 )); then printf '\\nM1S_QEMU_CHECK_PASS\\n'; else printf '\\nM1S_QEMU_CHECK_FAIL\\n'; fi
    sync
    systemctl poweroff
}}
trap finish EXIT
state=$(systemctl is-system-running --wait) || true
[[ $state == running ]]
[[ $(uname -m) == aarch64 && $(uname -r) == {shlex.quote(kernel_version)} ]]
root_device=$(readlink -f /dev/disk/by-partuuid/{root_partuuid})
[[ $(findmnt -n -o SOURCE /) == "$root_device" ]]
[[ $(lsblk -dn -o PARTN "$root_device") == 2 ]]
root_partition_bytes=$(blockdev --getsize64 "$root_device")
root_filesystem_bytes=$(df -B1 --output=size / | tail -n 1 | tr -d ' ')
(( root_partition_bytes > {original_root_bytes} + 1024 * 1024 * 1024 ))
(( root_filesystem_bytes > {original_root_bytes} ))
printf 'M1S_QEMU_GROWTH partition_bytes=%s filesystem_bytes=%s\\n' "$root_partition_bytes" "$root_filesystem_bytes"
mountpoint -q /boot
for unit in multi-user.target sshd.service systemd-networkd.service systemd-resolved.service; do
    systemctl is-active --quiet "$unit"
done
[[ -s /etc/machine-id && -s /etc/ssh/ssh_host_ed25519_key ]]
# Firstboot may defer until timesyncd has synchronized. Data-disk absence does
# not disable QEMU networking; outbound NTP still depends on the host network.
# A relative timeout stays bounded across corrections of the guest wall clock.
if ! timeout {min(300, args.timeout)}s /bin/bash -c 'until [[ -f /var/lib/odroid-m1s/initialized ]]; do sleep 1; done'; then
    echo 'M1S_QEMU_FIRSTBOOT_TIMEOUT: keyring not initialized; check NTP reachability and service logs.' >&2
    systemctl status --no-pager odroid-m1s-firstboot.service systemd-timesyncd.service || true
    exit 1
fi
[[ -s /etc/pacman.d/gnupg/pubring.gpg ]]
[[ -f /var/lib/odroid-m1s/initialized && -f /var/lib/odroid-m1s/root-expanded ]]
gpg --batch --homedir /etc/pacman.d/gnupg --list-keys 9B2C213B21883BB65CE2FB900CF25682E6BA0751 >/dev/null
exec 3<>/dev/tcp/127.0.0.1/22
IFS= read -r -t 10 ssh_greeting <&3
[[ $ssh_greeting == SSH-2.0-* ]]
printf 'M1S_QEMU_SSH %s\\n' "$ssh_greeting"
exec 3<&- 3>&-
[[ $(passwd -S root | awk '{{print $2}}') == L ]]
[[ $(passwd -S odroid | awk '{{print $2}}') == L ]]
[[ $(getent shadow odroid | cut -d: -f3) != 0 ]]
[[ $(sshd -T | awk 'tolower($1) == "passwordauthentication" {{print $2}}') == no ]]
install -d -m 700 /run/m1s-qemu-ssh
ssh-keygen -q -t ed25519 -N '' -C m1s-disposable-runtime-test -f /run/m1s-qemu-ssh/key
install -d -m 700 -o odroid -g odroid /home/odroid/.ssh
install -m 600 -o odroid -g odroid /run/m1s-qemu-ssh/key.pub /home/odroid/.ssh/authorized_keys
ssh_result=$(ssh -F /dev/null -i /run/m1s-qemu-ssh/key -p 22 \\
    -o BatchMode=yes -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \\
    -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new \\
    -o UserKnownHostsFile=/run/m1s-qemu-ssh/known_hosts -o ConnectTimeout=10 \\
    odroid@127.0.0.1 'test "$(id -u)" = 1000 && test "$(sudo -n id -u)" = 0 && printf M1S_SSH_LOGIN_OK')
[[ $ssh_result == M1S_SSH_LOGIN_OK ]]
rm -rf /run/m1s-qemu-ssh /home/odroid/.ssh/authorized_keys
printf 'M1S_QEMU_SSH_LOGIN user=odroid method=publickey password_locked=yes sudo=yes\\n'
pacman -Q linux linux-dtbs mkinitcpio openssh
pacman -Qk > /run/m1s-package-files.log
printf 'M1S_QEMU_PACKAGE_FILES_OK packages=%s\\n' "$(wc -l < /run/m1s-package-files.log)"
{data_checks}
findmnt --target /
findmnt --target /boot
systemctl --failed --no-legend
printf 'M1S_QEMU_RUNTIME %s %s\\n' "$(uname -r)" "$(cat /etc/machine-id)"
"""
(run_dir / "guest-checks.sh").write_text(guest_checks)
encoded = base64.b64encode(guest_checks.encode()).decode()
injection_lines = ["stty -echo", ": > /run/m1s-qemu-checks.b64"]
for offset in range(0, len(encoded), 1000):
    injection_lines.append(f"printf '%s' '{encoded[offset:offset + 1000]}' >> /run/m1s-qemu-checks.b64")
injection_lines.append("base64 -d /run/m1s-qemu-checks.b64 > /run/m1s-qemu-checks.sh; bash /run/m1s-qemu-checks.sh")
injection = "\n".join(injection_lines) + "\n"
print(f"Starting {version}; kernel {kernel_version}; data disk {args.data_disk}; timeout {args.timeout}s", flush=True)
log_path = run_dir / "serial.log"
started = time.monotonic()
process = None
injected = False
passed = False
failure = None
exit_code = None
seen = ""
try:
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    with log_path.open("wb") as log:
        while time.monotonic() - started < args.timeout:
            ready, _, _ = select.select([process.stdout], [], [], 1)
            if ready:
                chunk = os.read(process.stdout.fileno(), 65536)
                if chunk:
                    log.write(chunk)
                    log.flush()
                    seen += chunk.decode("utf-8", errors="replace")
                    seen = seen[-200000:]
                    plain = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", seen).replace("\r", "")
                    if not injected and re.search(r"(?:sh|bash)-[0-9.]+#", plain):
                        process.stdin.write(injection.encode())
                        process.stdin.flush()
                        injected = True
                        print("Guest debug shell reached; waiting for services and running assertions.", flush=True)
                    if re.search(r"(?m)^M1S_QEMU_CHECK_PASS$", plain):
                        if not passed:
                            print("Guest runtime assertions passed; waiting for clean shutdown.", flush=True)
                        passed = True
                    if re.search(r"(?m)^M1S_QEMU_CHECK_FAIL$", plain):
                        failure = "Guest runtime assertions failed; inspect serial.log."
                elif process.poll() is not None:
                    break
            if process.poll() is not None:
                remaining_output = process.stdout.read()
                log.write(remaining_output)
                break
        if process.poll() is None:
            failure = "QEMU verification timed out."
        else:
            exit_code = process.returncode
            if exit_code != 0:
                failure = f"QEMU exited with status {exit_code}."
        if not injected and failure is None:
            failure = "Guest did not reach its debug shell."
        if not passed and failure is None:
            failure = "Guest did not report successful runtime assertions."
finally:
    if process is not None and process.poll() is None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as qmp:
                qmp.settimeout(5)
                qmp.connect(str(run_dir / "qmp.sock"))
                qmp.recv(8192)
                qmp.sendall(b'{"execute":"qmp_capabilities"}\n')
                qmp.recv(8192)
                qmp.sendall(b'{"execute":"quit"}\n')
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    after_hash = sha256(source)
    if before_hash != after_hash:
        failure = "The input disk hash changed during verification."
    data_result = {"mode": args.data_disk}
    if data_disk is not None:
        data_result.update({"filesystem_uuid": data_uuid, "label": data_label,
                            "original_sha256": data_original_hash,
                            "original_sha256_after": sha256(data_disk),
                            "partition_geometry_before": data_geometry_before})
        try:
            if data_result["original_sha256_after"] != data_original_hash:
                raise ValueError("Synthetic data backing file changed.")
            data_after = run_dir / "data-after.raw"
            subprocess.run(runner + ["qemu-img", "convert", "-q", "-f", "qcow2", "-O", "raw",
                                    tool_path(data_snapshot), tool_path(data_after)], check=True)
            geometry_after = disk_geometry(data_after)
            data_result["partition_geometry_after"] = geometry_after
            if geometry_after != data_geometry_before:
                raise ValueError("Root growth or guest startup changed the data disk partition boundary/identity.")
            if args.data_disk == "wrong-label":
                data_result["runtime_sha256"] = sha256(data_after)
                data_result["runtime_unchanged"] = data_result["runtime_sha256"] == data_original_hash
                if not data_result["runtime_unchanged"]:
                    raise ValueError("The unrelated data filesystem was modified.")
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            failure = f"Data disk verification failed: {error}"
    report = {
        "passed": passed and failure is None,
        "failure": failure,
        "image": str(source),
        "image_sha256_before": before_hash,
        "image_sha256_after": after_hash,
        "kernel_version": kernel_version,
        "kernel_sha256": sha256(kernel),
        "initramfs_sha256": sha256(initrd),
        "qemu_version": version,
        "platform": "QEMU virt with CPU max, TCG, and emulated NVMe",
        "original_root_partition_bytes": original_root_bytes,
        "snapshot_disk_bytes": snapshot_bytes,
        "data_disk": data_result,
        "qemu_exit_code": exit_code,
        "seconds": round(time.monotonic() - started, 2),
        "physical_board_boot_verified": False,
        "test_only_boot_arguments": command_line,
        "firstboot_wait_timeout_seconds": min(300, args.timeout),
        "clock_test_policy": "No guest clock or synchronization marker injected; outbound NTP depends on the host network.",
    }
    (run_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
if failure is not None:
    raise SystemExit(failure)
PY
