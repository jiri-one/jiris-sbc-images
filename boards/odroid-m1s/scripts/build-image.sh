#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(realpath "$0")")/.."
scripts/build-uboot.sh --container
scripts/build-rootfs.sh
podman unshare python3 scripts/make-images.py 2>&1 | tee build/logs/image-assembly.log
