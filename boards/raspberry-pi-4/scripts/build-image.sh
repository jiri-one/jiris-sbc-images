#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(realpath "$0")")/.."
scripts/preflight.sh
scripts/prepare-tools.sh
scripts/build-rootfs.sh
export QEMU_CPU=cortex-a72
export PATH="$PWD/build/tools:$PATH"
podman unshare python3 scripts/make-images.py 2>&1 | tee build/logs/image-assembly.log
python3 scripts/verify-images.py 2>&1 | tee build/logs/image-verification.log
