#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(realpath "$0")")/.."
compression=()
while (($#)); do
    case $1 in
        --boot-mode) [[ $# -ge 2 ]] || exit 2; export RPI4_BOOT_MODE=$2; shift 2 ;;
        --reuse-rootfs) [[ $# -ge 2 ]] || exit 2; export RPI4_ROOTFS_SNAPSHOT=$2; shift 2 ;;
        --skip-compression) compression=(--skip-compression); shift ;;
        *) printf 'Usage: %s [--boot-mode native|uefi] [--reuse-rootfs PATH] [--skip-compression]\nDefault: native. UEFI is deprecated.\n' "$0" >&2; exit 2 ;;
    esac
done
source scripts/build-paths.sh
scripts/preflight.sh
scripts/prepare-tools.sh
scripts/build-rootfs.sh
export QEMU_CPU=cortex-a72
export PATH="$PWD/build/tools:$PATH"
podman unshare python3 scripts/make-images.py --boot-mode "$RPI4_BOOT_MODE" "${compression[@]}" \
    2>&1 | tee "$RPI4_BUILD_DIR/logs/image-assembly.log"
verification=()
[[ ${#compression[@]} == 0 ]] || verification=(--allow-uncompressed)
python3 scripts/verify-images.py --boot-mode "$RPI4_BOOT_MODE" "${verification[@]}" \
    2>&1 | tee "$RPI4_BUILD_DIR/logs/image-verification.log"
