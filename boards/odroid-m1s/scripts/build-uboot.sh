#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Build upstream ODROID-M1S U-Boot, including its Rockchip blobs.
# The only board-config addition is CMD_SYSBOOT for the NVMe carrier card.
# Usage: scripts/build-uboot.sh [--container|--native]
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
UBOOT_COMMIT=127a42c7257a6ffbbd1575ed1cbaa8f5408a44b3
RKBIN_COMMIT=ef49d0c2852b16ce0d72d7ee4849638cb5de27b7
UBOOT_SOURCE="$ROOT/sources/u-boot-v2026.01"
RKBIN_SOURCE="$ROOT/sources/rkbin-ef49d0c"
BUILD="$ROOT/build/uboot"
CONTAINER=${M1S_UBOOT_CONTAINER:-arch-m1s-uboot-build}
CONTAINER_IMAGE=docker.io/library/archlinux@sha256:601140151f85e9f33ac42ebdd671061b109ad3ed83d794e2399bbfa6ccf30543
JOBS=${JOBS:-8}
MODE=${1:---container}

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

case "$MODE" in
    --container|--native|--inside) ;;
    *) die 'Usage: scripts/build-uboot.sh [--container|--native]' ;;
esac
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || die 'JOBS must be a positive integer'
mkdir -p "$ROOT/sources" "$BUILD" "$ROOT/cache"

if [[ "$MODE" != --inside ]]; then
    command -v git >/dev/null || die 'git is required on the host'
    if [[ ! -e "$UBOOT_SOURCE" ]]; then
        git clone --depth 1 --branch v2026.01 \
            https://source.denx.de/u-boot/u-boot.git "$UBOOT_SOURCE"
    fi
    if [[ ! -e "$RKBIN_SOURCE" ]]; then
        git init "$RKBIN_SOURCE"
        git -C "$RKBIN_SOURCE" remote add origin https://github.com/rockchip-linux/rkbin.git
        git -C "$RKBIN_SOURCE" config remote.origin.promisor true
        git -C "$RKBIN_SOURCE" config remote.origin.partialclonefilter blob:none
        git -C "$RKBIN_SOURCE" sparse-checkout init --no-cone
        git -C "$RKBIN_SOURCE" sparse-checkout set --no-cone \
            /LICENSE /README \
            /bin/rk35/rk3566_ddr_1056MHz_v1.23.bin \
            /bin/rk35/rk3568_bl31_v1.45.elf \
            /RKBOOT/RK3566MINIALL.ini /RKTRUST/RK3568TRUST.ini
        git -C "$RKBIN_SOURCE" fetch --depth 1 --filter=blob:none origin "$RKBIN_COMMIT"
        git -C "$RKBIN_SOURCE" checkout --detach FETCH_HEAD
    fi
fi

for item in "$UBOOT_SOURCE:$UBOOT_COMMIT" "$RKBIN_SOURCE:$RKBIN_COMMIT"; do
    source_dir=${item%:*}
    expected=${item##*:}
    [[ $(git -C "$source_dir" rev-parse HEAD) == "$expected" ]] ||
        die "Unexpected source commit in $source_dir"
    [[ -z $(git -C "$source_dir" status --porcelain --untracked-files=normal) ]] ||
        die "Source checkout is modified: $source_dir"
done

(
    cd "$RKBIN_SOURCE"
    sha256sum --check <<'HASHES'
20e4bb076847bd019fcdeb7bdc15bd249890f07ecc76e9937101f22e50950982  bin/rk35/rk3566_ddr_1056MHz_v1.23.bin
76634f10e535bbe981fb9132fd6815a71586cc1b96aae1159bec6797579e5b9f  bin/rk35/rk3568_bl31_v1.45.elf
d2765124a2c24d71aba3cbc6cdd6975a5a297821264eb6c26378a4c16cd16e92  LICENSE
HASHES
)

if [[ "$MODE" == --container ]]; then
    command -v podman >/dev/null || die 'rootless Podman is required (or use --native)'
    if podman container exists "$CONTAINER"; then
        # Reuse only this task's container and its exact workspace mapping.
        expected_image_id=$(podman image inspect "$CONTAINER_IMAGE" --format '{{.Id}}')
        podman inspect "$CONTAINER" | python3 -c '
import json,sys
c=json.load(sys.stdin)[0]
assert c["Image"].removeprefix("sha256:") == sys.argv[2].removeprefix("sha256:"), "Unexpected builder image"
assert any(m.get("Source")==sys.argv[1] and m.get("Destination")=="/workspace" for m in c["Mounts"]), "Unexpected builder workspace"
' "$ROOT" "$expected_image_id"
        podman start "$CONTAINER" >/dev/null
    else
        podman run -d --name "$CONTAINER" --volume "$ROOT:/workspace:rw" \
            --workdir /workspace "$CONTAINER_IMAGE" sleep infinity
    fi
    if ! podman exec "$CONTAINER" test -e /etc/arch-m1s-uboot-snapshot-20260106; then
        podman exec -i "$CONTAINER" bash -se 2>&1 <<'SETUP' | tee "$BUILD/container-setup.log"
printf '%s\n' 'Server = https://archive.archlinux.org/repos/2026/01/06/$repo/os/$arch' > /etc/pacman.d/mirrorlist
pacman -Syyuu --noconfirm --needed base-devel aarch64-linux-gnu-gcc dtc python python-setuptools python-pyelftools swig openssl gnutls git
touch /etc/arch-m1s-uboot-snapshot-20260106
SETUP
    fi
    podman image inspect "$CONTAINER_IMAGE" > "$BUILD/container-image.json"
    podman exec "$CONTAINER" pacman -Q > "$BUILD/container-packages.txt"
    podman exec --env "JOBS=$JOBS" "$CONTAINER" \
        bash /workspace/scripts/build-uboot.sh --inside
    exit
fi

for tool in aarch64-linux-gnu-gcc make gcc dtc python3 bison flex git sha256sum; do
    command -v "$tool" >/dev/null || die "Missing build tool: $tool"
done
python3 -c 'import libfdt, elftools'

export SOURCE_DATE_EPOCH=1767646162 LC_ALL=C TZ=UTC
export CROSS_COMPILE=aarch64-linux-gnu-
export ROCKCHIP_TPL="$RKBIN_SOURCE/bin/rk35/rk3566_ddr_1056MHz_v1.23.bin"
export BL31="$RKBIN_SOURCE/bin/rk35/rk3568_bl31_v1.45.elf"
build_args=(-C "$UBOOT_SOURCE" "O=$BUILD/out")

# Use the bundled dtc and matching pylibfdt together. Clear earlier objects
# so an interrupted build cannot supply stale tools or Python bindings.
if [[ -f "$BUILD/out/Makefile" ]]; then
    make "${build_args[@]}" clean > "$BUILD/clean.log" 2>&1
fi
{
    printf 'U-Boot commit: %s\nrkbin commit: %s\n' "$UBOOT_COMMIT" "$RKBIN_COMMIT"
    printf 'SOURCE_DATE_EPOCH=%s\n' "$SOURCE_DATE_EPOCH"
    aarch64-linux-gnu-gcc --version
    make --version
    python3 --version
    dtc --version
    make "${build_args[@]}" odroid-m1s-rk3566_defconfig
    "$UBOOT_SOURCE/scripts/config" --file "$BUILD/out/.config" --enable CMD_SYSBOOT
    make "${build_args[@]}" olddefconfig
    make "${build_args[@]}" "-j$JOBS"
    make "${build_args[@]}" savedefconfig
} 2>&1 | tee "$BUILD/build.log"

cp "$BUILD/out/u-boot-rockchip.bin" "$BUILD/u-boot-rockchip.bin"
cp "$BUILD/out/.config" "$BUILD/uboot.config"
printf '%s\n' 'CONFIG_CMD_SYSBOOT=y' > "$BUILD/config-overlay.txt"
diff -u --label upstream/odroid-m1s-rk3566_defconfig --label build/defconfig \
    "$UBOOT_SOURCE/configs/odroid-m1s-rk3566_defconfig" \
    "$BUILD/out/defconfig" > "$BUILD/config-overlay.diff" || [[ $? -eq 1 ]]
mkdir -p "$BUILD/licenses/u-boot" "$BUILD/licenses/rkbin"
cp -a "$UBOOT_SOURCE/Licenses/." "$BUILD/licenses/u-boot/"
cp "$RKBIN_SOURCE/LICENSE" "$RKBIN_SOURCE/README" "$BUILD/licenses/rkbin/"

if [[ -f "$ROOT/configs/nvme-loader.cmd" ]]; then
    "$BUILD/out/tools/mkimage" -A arm64 -O linux -T script -C none \
        -n 'ODROID-M1S NVMe boot' -d "$ROOT/configs/nvme-loader.cmd" \
        "$BUILD/nvme-boot.scr" > "$BUILD/nvme-script-description.txt"
fi

# Keep the exact corresponding U-Boot source beside the result for handoff.
git -C "$UBOOT_SOURCE" archive --format=tar --prefix=u-boot-v2026.01/ "$UBOOT_COMMIT" \
    | gzip -n > "$ROOT/cache/uboot-v2026.01-source.tar.gz"
(
    cd "$ROOT"
    sha256sum build/uboot/u-boot-rockchip.bin build/uboot/uboot.config \
        cache/uboot-v2026.01-source.tar.gz \
        sources/rkbin-ef49d0c/bin/rk35/rk3566_ddr_1056MHz_v1.23.bin \
        sources/rkbin-ef49d0c/bin/rk35/rk3568_bl31_v1.45.elf \
        sources/rkbin-ef49d0c/LICENSE > build/uboot/SHA256SUMS
    if [[ -f build/uboot/nvme-boot.scr ]]; then
        sha256sum build/uboot/nvme-boot.scr >> build/uboot/SHA256SUMS
    fi
)
"$BUILD/out/tools/mkimage" -l "$BUILD/out/u-boot.itb" > "$BUILD/fit-description.txt"
python3 - "$BUILD" <<'PY'
from pathlib import Path
import sys
b = Path(sys.argv[1])
image = (b / 'u-boot-rockchip.bin').read_bytes()
assert 0 < len(image) <= (32768 - 64) * 512, 'U-Boot overlaps the first partition'
assert image[0:4] == b'RKNS', 'Unexpected RK356x image header'
config = (b / 'uboot.config').read_text()
for value in ('CONFIG_TARGET_ODROID_M1S_RK3566=y', 'CONFIG_NVME_PCI=y',
              'CONFIG_PCIE_DW_ROCKCHIP=y', 'CONFIG_BOOTMETH_EXTLINUX=y',
              'CONFIG_BOOTMETH_SCRIPT=y', 'CONFIG_CMD_SYSBOOT=y'):
    assert value in config, f'Missing expected capability: {value}'
idb = (b / 'out/idbloader.img').read_bytes()
fit = (b / 'out/u-boot.itb').read_bytes()
assert image.startswith(idb), 'Combined image DDR/SPL differs from idbloader'
assert image[0x7f8000:] == fit, 'Combined image FIT offset/content is incorrect'
print(f'Validated {len(image):,} bytes; fits before sector 32768 when written at sector 64.')
PY
printf 'Built %s\n' "$BUILD/u-boot-rockchip.bin"
