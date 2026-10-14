#!/usr/bin/env python3
"""Boot the image's EFI loader on QEMU virt using disposable disk/firmware copies.

This exercises the AArch64 Cortex-A72 CPU baseline, not Raspberry Pi hardware
or its pftf firmware. The input disk is never attached writable to QEMU.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import select
import shlex
import socket
import subprocess
import tempfile
import time
import uuid


REPO = Path(__file__).resolve().parents[1]
ENTRY = "/loader/entries/rpi4.conf"
PORTS_KEY = "C43E3B9B9CE3F35E97AEF93873556FFF735F691D"
EFI_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
ROOT_ARM64_TYPE = "b921b045-1df0-41c3-af44-4c6f280d3fae"
TEST_OPTIONS = [
    "console=ttyAMA0,115200n8",
    "systemd.debug_shell=ttyAMA0",
    "systemd.mask=serial-getty@ttyAMA0.service",
    "systemd.show_status=true",
]


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def parse_layout(table, disk_bytes):
    """Reject unexpected geometry before addressing a FAT partition by offset."""
    if table.get("label") != "gpt" or table.get("sectorsize", 512) != 512:
        raise ValueError("Expected GPT with 512-byte sectors.")
    parts = table.get("partitions", [])
    if len(parts) != 2:
        raise ValueError("Expected exactly two partitions: FAT ESP and ext4 root.")
    previous_end = 34
    for part, expected_type in zip(parts, (EFI_TYPE, ROOT_ARM64_TYPE)):
        if str(part.get("type", "")).lower() != expected_type:
            raise ValueError("Unexpected partition type.")
        start, size = part.get("start"), part.get("size")
        if not isinstance(start, int) or not isinstance(size, int) or size <= 0:
            raise ValueError("Invalid partition size or start sector.")
        if start < previous_end or (start + size) * 512 > disk_bytes:
            raise ValueError("Overlapping partition or partition beyond disk end.")
        uuid.UUID(part["uuid"])
        previous_end = start + size
    return parts


def read_entry(config, root_partuuid):
    fields = {}
    for line in config.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = (line.split(None, 1) + [""])[:2]
        if key in ("linux", "initrd", "options"):
            if key in fields:
                raise ValueError(f"Ambiguous repeated {key} field in EFI entry.")
            fields[key] = value.strip()
    if set(fields) != {"linux", "initrd", "options"}:
        raise ValueError("EFI entry must name a Linux kernel, initramfs and options.")
    for key in ("linux", "initrd"):
        path = fields[key]
        if not re.fullmatch(r"/[A-Za-z0-9/_.+\-]+", path) or ".." in path.split("/"):
            raise ValueError(f"Unsafe {key} path in EFI entry.")
    if PurePosixPath(fields["linux"]).parent != PurePosixPath(fields["initrd"]).parent:
        raise ValueError("Kernel and initramfs must belong to the same boot set.")
    options = shlex.split(fields["options"])
    root = [value for value in options if value.startswith("root=")]
    if root != [f"root=PARTUUID={root_partuuid}"]:
        raise ValueError("EFI root PARTUUID does not match the second partition.")
    # The release must never include the debug access enabled by this harness.
    if any(value.startswith(("systemd.debug_shell", "init=", "rdinit=")) for value in options):
        raise ValueError("Release EFI entry contains a debug shell or replacement init.")
    return fields


def test_entry(config, fields):
    options = [word for word in shlex.split(fields["options"]) if not word.startswith("console=")]
    options.extend(TEST_OPTIONS)
    lines = config.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^\s*options\s+", line):
            lines[index] = "options " + " ".join(options)
    return "\n".join(lines) + "\n", " ".join(options)


def guest_script(kernel_version, root_partuuid, boot_partuuid, original_root_bytes, timeout):
    """Assertions run only in the disposable qcow2 guest via its debug console."""
    return f"""#!/bin/bash
set -euo pipefail
export LC_ALL=C
finish() {{
    result=$?
    trap - EXIT
    if (( result == 0 )); then
        printf '\\nRPI4_QEMU_CHECK_PASS\\n'
    else
        printf '\\nRPI4_QEMU_CHECK_FAIL status=%s\\n' "$result"
        systemctl --failed --no-pager || true
        journalctl -b --no-pager -n 80 -u rpi4-grow-root -u rpi4-firstboot -u systemd-timesyncd -u sshd || true
    fi
    sync
    systemctl poweroff
}}
trap finish EXIT
trap 'printf "RPI4_QEMU_ASSERTION_FAILED line=%s\\n" "$LINENO" >&2' ERR
state=$(systemctl is-system-running --wait) || true
[[ $state == running ]]
[[ -d /sys/firmware/efi ]]
[[ $(uname -m) == aarch64 && $(uname -r) == {shlex.quote(kernel_version)} ]]
root_device=$(readlink -f /dev/disk/by-partuuid/{root_partuuid})
[[ $(findmnt -n -o SOURCE /) == "$root_device" ]]
[[ $(findmnt -n -o FSTYPE /) == ext4 ]]
[[ $(lsblk -dn -o PARTN "$root_device" | tr -d ' ') == 2 ]]
root_partition_bytes=$(blockdev --getsize64 "$root_device")
root_filesystem_bytes=$(df -B1 --output=size / | tail -n 1 | tr -d ' ')
(( root_partition_bytes > {original_root_bytes} + 1024 * 1024 * 1024 ))
(( root_filesystem_bytes > {original_root_bytes} ))
[[ -f /var/lib/rpi4/root-expanded ]]
printf 'RPI4_QEMU_GROWTH partition_bytes=%s filesystem_bytes=%s\\n' "$root_partition_bytes" "$root_filesystem_bytes"
# Re-running the shipped helper must safely accept an already expanded root.
/usr/local/sbin/rpi4-grow-root
[[ $(blockdev --getsize64 "$root_device") == "$root_partition_bytes" ]]
mountpoint -q /boot
[[ $(findmnt -n -o FSTYPE /boot) == vfat ]]
boot_device=$(readlink -f /dev/disk/by-partuuid/{boot_partuuid})
[[ $(findmnt -n -o SOURCE /boot) == "$boot_device" ]]
findmnt --verify --tab-file /etc/fstab
for unit in multi-user.target sshd.service systemd-networkd.service systemd-resolved.service systemd-timesyncd.service; do
    systemctl is-active --quiet "$unit"
done
[[ $(ip -4 route show default | wc -l) -ge 1 ]]
getent ahostsv4 arch.bens.haus > /run/rpi4-qemu-dns.txt
printf 'RPI4_QEMU_NETWORK dhcp_default_route=yes dns=yes\\n'
[[ $(cat /etc/machine-id) =~ ^[0-9a-f]{{32}}$ ]]
[[ -s /etc/ssh/ssh_host_ed25519_key && -s /etc/ssh/ssh_host_ed25519_key.pub ]]
[[ $(id -u rpi) == 1000 ]]
id -nG rpi | tr ' ' '\\n' | grep -qx wheel
[[ $(passwd -S root | awk '{{print $2}}') == L ]]
[[ $(passwd -S rpi | awk '{{print $2}}') == L ]]
printf 'RPI4_QEMU_IDENTITIES machine_id=generated ssh_host_key=generated root_password=locked user=rpi\\n'
# This waits for actual network time, without fabricating a synchronization marker.
if ! timeout {min(300, timeout)}s /bin/bash -c 'until [[ -f /var/lib/rpi4/initialized ]]; do sleep 1; done'; then
    echo 'RPI4_QEMU_FIRSTBOOT_TIMEOUT: check outbound NTP and firstboot logs.' >&2
    exit 1
fi
[[ -e /run/systemd/timesync/synchronized ]]
[[ $(timedatectl show -p NTPSynchronized --value) == yes ]]
[[ -s /etc/pacman.d/gnupg/pubring.gpg ]]
gpg --batch --homedir /etc/pacman.d/gnupg --list-keys {PORTS_KEY} > /dev/null
printf 'RPI4_QEMU_FIRSTBOOT initialized=yes real_time_sync=yes ports_key=present\\n'
exec 3<>/dev/tcp/127.0.0.1/22
IFS= read -r -t 10 ssh_greeting <&3
[[ $ssh_greeting == SSH-2.0-* ]]
printf 'RPI4_QEMU_SSH_BANNER %s\\n' "$ssh_greeting"
exec 3<&- 3>&-
# A locked, nonexpired password must still allow the configured SSH key.
[[ $(sshd -T | awk 'tolower($1) == "passwordauthentication" {{print $2}}') == no ]]
password_age=$(getent shadow rpi | cut -d: -f3)
[[ $password_age != 0 ]]
install -d -m 700 /run/rpi4-qemu-ssh
ssh-keygen -q -t ed25519 -N '' -C rpi4-disposable-runtime-test -f /run/rpi4-qemu-ssh/key
install -d -m 700 -o rpi -g rpi /home/rpi/.ssh
install -m 600 -o rpi -g rpi /run/rpi4-qemu-ssh/key.pub /home/rpi/.ssh/authorized_keys
ssh_result=$(ssh -F /dev/null -i /run/rpi4-qemu-ssh/key -p 22 \\
    -o BatchMode=yes -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \\
    -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new \\
    -o UserKnownHostsFile=/run/rpi4-qemu-ssh/known_hosts -o ConnectTimeout=10 \\
    rpi@127.0.0.1 'test "$(id -u)" = 1000 && test "$(sudo -n id -u)" = 0 && printf RPI4_SSH_LOGIN_OK')
[[ $ssh_result == RPI4_SSH_LOGIN_OK ]]
rm -rf /run/rpi4-qemu-ssh /home/rpi/.ssh/authorized_keys
printf 'RPI4_QEMU_SSH_LOGIN user=rpi method=publickey password_locked=yes sudo=yes\\n'
pacman -Q linux linux-dtbs mkinitcpio openssh
pacman -Qk > /run/rpi4-qemu-package-files.log
printf 'RPI4_QEMU_PACKAGE_FILES_OK packages=%s\\n' "$(wc -l < /run/rpi4-qemu-package-files.log)"
bootctl status --no-pager
findmnt --target /
findmnt --target /boot
[[ $(systemctl is-system-running) == running ]]
printf 'RPI4_QEMU_RUNTIME kernel=%s firmware=generic_AAVMF board=QEMU_virt\\n' "$(uname -r)"
"""


def inject_script(script):
    encoded = base64.b64encode(script.encode()).decode()
    lines = ["stty -echo", ": > /run/rpi4-qemu-checks.b64"]
    for offset in range(0, len(encoded), 1000):
        lines.append(f"printf '%s' '{encoded[offset:offset + 1000]}' >> /run/rpi4-qemu-checks.b64")
    lines.append("base64 -d /run/rpi4-qemu-checks.b64 > /run/rpi4-qemu-checks.sh; bash /run/rpi4-qemu-checks.sh")
    return ("\n".join(lines) + "\n").encode()


def console_text(output):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", output).replace("\r", "")


def stop_qemu(process, socket_path):
    if process is None or process.poll() is not None:
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as qmp:
            qmp.settimeout(5)
            qmp.connect(str(socket_path))
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


def run_guest(command, run_dir, injection, timeout):
    process = None
    injected = passed = False
    failure = None
    seen = ""
    started = time.monotonic()
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with (run_dir / "serial.log").open("wb") as log:
            while time.monotonic() - started < timeout:
                ready, _, _ = select.select([process.stdout], [], [], 1)
                if ready:
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    log.write(chunk)
                    log.flush()
                    seen = (seen + chunk.decode("utf-8", errors="replace"))[-200000:]
                    plain = console_text(seen)
                    if not injected and re.search(r"(?:sh|bash)-[0-9.]+#", plain):
                        process.stdin.write(injection)
                        process.stdin.flush()
                        injected = True
                        print("EFI boot reached the debug shell; running guest assertions.", flush=True)
                    if re.search(r"(?m)^RPI4_QEMU_CHECK_PASS$", plain):
                        if not passed:
                            print("Guest assertions passed; waiting for clean shutdown.", flush=True)
                        passed = True
                    if re.search(r"(?m)^RPI4_QEMU_CHECK_FAIL\b", plain):
                        failure = "Guest runtime assertions failed; inspect serial.log and guest-checks.sh."
                if process.poll() is not None:
                    # Drain any buffered output before assessing the pass marker.
                    continue
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                failure = failure or "QEMU verification timed out."
            if process.poll() is not None and process.returncode != 0:
                failure = failure or f"QEMU exited with status {process.returncode}."
            if not injected:
                failure = failure or "EFI boot did not reach the guest debug shell."
            if not passed:
                failure = failure or "Guest did not report successful runtime assertions."
    finally:
        stop_qemu(process, run_dir / "qmp.sock")
    return {
        "passed": passed and failure is None,
        "failure": failure,
        "qemu_exit_code": process.returncode,
        "guest_debug_shell_reached": injected,
        "seconds": round(time.monotonic() - started, 2),
        "guest_evidence": re.findall(r"(?m)^RPI4_QEMU_[^\r\n]+", console_text(seen)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", type=Path, default=REPO / "artifacts/rpi4-archlinux-ports-sd-usb.img")
    parser.add_argument("--container", help="prepared rootless tools container, normally arch-rpi4-tools")
    parser.add_argument("--timeout", type=int, default=900, help="boot and assertions timeout in seconds")
    parser.add_argument("--firmware-code", default="/usr/share/AAVMF/AAVMF_CODE.fd", help="AAVMF code path in the tool environment")
    parser.add_argument("--firmware-vars", default="/usr/share/AAVMF/AAVMF_VARS.fd", help="AAVMF variable template in the tool environment")
    args = parser.parse_args()
    source = args.image.resolve(strict=True)
    if not source.is_file() or args.timeout < 60:
        parser.error("Expected a regular raw image file and a timeout of at least 60 seconds.")
    if "," in str(source) or "," in str(REPO):
        parser.error("QEMU verification paths must not contain commas.")
    if args.container and not source.is_relative_to(REPO):
        parser.error("Container verification requires the image to be inside this repository.")
    runner = ["podman", "exec", "-i", args.container] if args.container else []

    def tool_path(path):
        path = path.resolve()
        return str(Path("/work") / path.relative_to(REPO)) if args.container else str(path)

    def run(*command, **kwargs):
        return subprocess.run(runner + list(command), check=True, **kwargs)

    (REPO / "build").mkdir(exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="qemu-test-", dir=REPO / "build"))
    print(f"QEMU verification artifacts: {run_dir}", flush=True)
    before_hash = sha256(source)
    report = {
        "passed": False,
        "failure": "Verification did not complete.",
        "image": str(source),
        "image_sha256_before": before_hash,
        "platform": "QEMU virt, Cortex-A72, TCG, virtio block/network/RNG, generic AAVMF",
        "physical_board_boot_verified": False,
        "pi_firmware_verified": False,
        "clock_test_policy": "Real guest NTP synchronization required; no clock or synchronization marker injected.",
        "ssh_test_policy": "Disposable in-guest key; locked nonexpired account, SSH key authentication and passwordless sudo checked only in qcow2.",
    }
    try:
        table = json.loads(run("sfdisk", "--json", tool_path(source), capture_output=True, text=True).stdout)["partitiontable"]
        boot, root = parse_layout(table, source.stat().st_size)
        root_partuuid, boot_partuuid = root["uuid"].lower(), boot["uuid"].lower()
        original_root_bytes = root["size"] * 512
        # Editing a FAT image requires a raw regular file. Clone the entire disk
        # sparsely, edit only that copy, then use it as a read-only qcow2 backing.
        backing = run_dir / "test-only-backing.raw"
        subprocess.run(["cp", "--reflink=auto", "--sparse=always", "--", str(source), str(backing)], check=True)
        esp = f"{tool_path(backing)}@@{boot['start'] * 512}"
        config = run("mtype", "-i", esp, f"::{ENTRY}", capture_output=True, text=True).stdout
        fields = read_entry(config, root_partuuid)
        files = {"kernel": fields["linux"], "initramfs": fields["initrd"], "efi_loader": "/EFI/BOOT/BOOTAA64.EFI"}
        for name, boot_path in files.items():
            destination = run_dir / name
            run("mcopy", "-i", esp, f"::{boot_path}", tool_path(destination))
            report[f"{name}_sha256"] = sha256(destination)
        with (run_dir / "kernel").open("rb") as kernel:
            if kernel.read(2) != b"MZ":
                raise ValueError("EFI entry does not point to a PE/COFF EFI kernel.")
        version_path = str(PurePosixPath(fields["linux"]).parent / "kernel-version")
        kernel_version = run("mtype", "-i", esp, f"::{version_path}", capture_output=True, text=True).stdout.strip()
        if not re.fullmatch(r"[0-9A-Za-z._+\-]+", kernel_version):
            raise ValueError("Invalid kernel version in the EFI boot set.")
        patched, command_line = test_entry(config, fields)
        config_path = run_dir / "test-only-entry.conf"
        config_path.write_text(patched)
        run("mcopy", "-o", "-i", esp, tool_path(config_path), f"::{ENTRY}")
        backing.chmod(0o444)
        snapshot = run_dir / "runtime.qcow2"
        run("qemu-img", "create", "-q", "-f", "qcow2", "-F", "raw", "-b", tool_path(backing), tool_path(snapshot))
        snapshot_bytes = max(10 * 1024**3, source.stat().st_size + 2 * 1024**3)
        run("qemu-img", "resize", "-q", tool_path(snapshot), str(snapshot_bytes))
        variables = run_dir / "AAVMF_VARS.fd"
        run("cp", "--sparse=always", "--", args.firmware_vars, tool_path(variables))
        firmware_hash = run("sha256sum", args.firmware_code, capture_output=True, text=True).stdout.split()[0]
        version = run("qemu-system-aarch64", "--version", capture_output=True, text=True).stdout.splitlines()[0]
        checks = guest_script(kernel_version, root_partuuid, boot_partuuid, original_root_bytes, args.timeout)
        (run_dir / "guest-checks.sh").write_text(checks)
        command = runner + [
            "timeout", "--signal=TERM", "--kill-after=5", str(args.timeout + 30),
            "qemu-system-aarch64", "-machine", "virt,gic-version=3", "-cpu", "cortex-a72",
            "-accel", "tcg,thread=multi", "-smp", "2", "-m", "2048",
            "-drive", f"if=pflash,format=raw,readonly=on,file={args.firmware_code}",
            "-drive", f"if=pflash,format=raw,file={tool_path(variables)}",
            "-drive", f"if=none,id=rootdisk,format=qcow2,file={tool_path(snapshot)}",
            "-device", "virtio-blk-pci,drive=rootdisk,bootindex=1",
            "-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0,romfile=",
            "-object", "rng-random,filename=/dev/urandom,id=rng0", "-device", "virtio-rng-pci,rng=rng0",
            "-display", "none", "-monitor", "none", "-serial", "stdio", "-no-reboot",
            "-qmp", f"unix:{tool_path(run_dir / 'qmp.sock')},server=on,wait=off",
        ]
        report.update({
            "kernel_version": kernel_version,
            "qemu_version": version,
            "firmware_code": args.firmware_code,
            "firmware_code_sha256": firmware_hash,
            "efi_entry": ENTRY,
            "original_root_partition_bytes": original_root_bytes,
            "snapshot_disk_bytes": snapshot_bytes,
            "test_only_boot_arguments": command_line,
            "firstboot_wait_timeout_seconds": min(300, args.timeout),
        })
        print(f"Starting {version}; EFI kernel {kernel_version}; Cortex-A72; timeout {args.timeout}s", flush=True)
        report.update(run_guest(command, run_dir, inject_script(checks), args.timeout))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        report["passed"] = False
        report["failure"] = str(error)
    finally:
        after_hash = sha256(source)
        report["image_sha256_after"] = after_hash
        report["release_image_unchanged"] = before_hash == after_hash
        if before_hash != after_hash:
            report["passed"] = False
            report["failure"] = "The input disk hash changed during verification."
        (run_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
    if not report["passed"]:
        raise SystemExit(report["failure"])


if __name__ == "__main__":
    main()
