#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -ne 0 ]] || { echo 'Run the driver as a normal user with rootless Podman, not host root.' >&2; exit 1; }
for tool in podman qemu-aarch64-static curl bsdtar gpg python3 sfdisk mkfs.vfat mkfs.ext4 e2fsck fsck.fat debugfs zstd chroot; do
    command -v "$tool" >/dev/null || { echo "Missing host tool: $tool" >&2; exit 1; }
done
python3 - <<'PY'
from pathlib import Path
p = Path('/proc/sys/fs/binfmt_misc/qemu-aarch64')
if not p.is_file():
    raise SystemExit('An enabled qemu-aarch64 binfmt entry with the F flag is required.')
lines = p.read_text().splitlines()
flags = next((line.partition(':')[2].strip() for line in lines if line.startswith('flags:')), '')
if 'enabled' not in lines or 'F' not in flags:
    raise SystemExit('qemu-aarch64 binfmt must be enabled and use the F flag for chroot execution.')
PY
[[ $(podman unshare id -u) == 0 ]]
printf 'Host tools, registered AArch64 emulation and rootless namespace are available.\n'
