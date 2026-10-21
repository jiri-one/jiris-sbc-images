#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(realpath "$0")")/.."
source configs/sources.env
source scripts/build-paths.sh "$@"
runtime_cache="cache/raspberrypi-firmware-$RUNTIME_FIRMWARE_COMMIT"
wifi_cache="cache/raspberrypi-wifi-firmware-$WIFI_FIRMWARE_COMMIT"
mkdir -p cache "$runtime_cache" "$wifi_cache" build/logs build/firmware build/wifi-firmware build/input-gnupg
chmod 700 build/input-gnupg
fetch() {
    local url=$1 output=$2
    if [[ ! -s $output ]]; then
        curl --fail --location --retry 3 --connect-timeout 20 --output "$output.partial" "$url"
        mv "$output.partial" "$output"
    fi
}
fetch "$BOOTSTRAP_BASE/$BOOTSTRAP_NAME" "cache/$BOOTSTRAP_NAME"
fetch "$PORTS_KEY_URL" cache/ben-key-bundle.asc
if [[ $RPI4_BOOT_MODE == uefi ]]; then
    fetch "$FIRMWARE_URL" "cache/$FIRMWARE_NAME"
fi
fetch "$RUNTIME_FIRMWARE_BASE/start4.elf" "$runtime_cache/start4.elf"
fetch "$RUNTIME_FIRMWARE_BASE/fixup4.dat" "$runtime_cache/fixup4.dat"
fetch "$WIFI_FIRMWARE_BASE/cypress/cyfmac43455-sdio-standard.bin" "$wifi_cache/cyfmac43455-sdio-standard.bin"
fetch "$WIFI_FIRMWARE_BASE/cypress/cyfmac43455-sdio.clm_blob" "$wifi_cache/cyfmac43455-sdio.clm_blob"
fetch "$WIFI_FIRMWARE_BASE/brcm/brcmfmac43455-sdio.txt" "$wifi_cache/brcmfmac43455-sdio.txt"
printf '%s  %s\n' \
    "$BOOTSTRAP_SHA256" "cache/$BOOTSTRAP_NAME" \
    "$RUNTIME_START4_SHA256" "$runtime_cache/start4.elf" \
    "$RUNTIME_FIXUP4_SHA256" "$runtime_cache/fixup4.dat" \
    "$WIFI_BIN_SHA256" "$wifi_cache/cyfmac43455-sdio-standard.bin" \
    "$WIFI_CLM_SHA256" "$wifi_cache/cyfmac43455-sdio.clm_blob" \
    "$WIFI_NVRAM_SHA256" "$wifi_cache/brcmfmac43455-sdio.txt" | sha256sum --check
if [[ $RPI4_BOOT_MODE == uefi ]]; then
    printf '%s  %s\n' "$FIRMWARE_SHA256" "cache/$FIRMWARE_NAME" | sha256sum --check
fi
# The published key URL contains a bundle. Import into an isolated keyring;
# export ONLY the selected public key. No ownertrust is assigned to the bundle.
gpg --homedir build/input-gnupg --batch --import-options import-minimal \
    --import cache/ben-key-bundle.asc > build/logs/input-key-import.log 2>&1
gpg --homedir build/input-gnupg --batch --armor --export "$PORTS_KEY_FINGERPRINT" > cache/ports-public.key
fingerprint=$(gpg --homedir build/input-gnupg --batch --with-colons --show-keys cache/ports-public.key | awk -F: '$1=="fpr" {print $10; exit}')
[[ $fingerprint == "$PORTS_KEY_FINGERPRINT" ]]
[[ $(gpg --homedir build/input-gnupg --batch --with-colons --show-keys cache/ports-public.key | awk -F: '$1=="pub" {n++} END {print n}') == 1 ]]
if [[ $RPI4_BOOT_MODE == uefi ]]; then
    bsdtar -xf "cache/$FIRMWARE_NAME" -C build/firmware
    [[ -s build/firmware/RPI_EFI.fd && -s build/firmware/config.txt ]]
fi
# Both modes use this matched pair. Native needs no UEFI archive or bundled DTBs.
install -m 0644 "$runtime_cache/start4.elf" "$runtime_cache/fixup4.dat" build/firmware/
# The driver requests these board-specific names before the generic ones.
board=brcmfmac43455-sdio.raspberrypi,4-model-b
install -m 0644 "$wifi_cache/cyfmac43455-sdio-standard.bin" "build/wifi-firmware/$board.bin"
install -m 0644 "$wifi_cache/cyfmac43455-sdio.clm_blob" "build/wifi-firmware/$board.clm_blob"
install -m 0644 "$wifi_cache/brcmfmac43455-sdio.txt" "build/wifi-firmware/$board.txt"
# Record the values sourced above, after download hashes and the selected key
# have passed their checks. A clean checkout does not contain build artifacts.
export BOOTSTRAP_NAME BOOTSTRAP_BASE BOOTSTRAP_SHA256 PORTS_KEY_URL PORTS_KEY_FINGERPRINT
export FIRMWARE_VERSION FIRMWARE_NAME FIRMWARE_URL FIRMWARE_SHA256 TOOLS_BASE
export RUNTIME_FIRMWARE_VERSION RUNTIME_FIRMWARE_COMMIT RUNTIME_FIRMWARE_BASE
export RUNTIME_START4_SHA256 RUNTIME_FIXUP4_SHA256
export WIFI_FIRMWARE_COMMIT WIFI_FIRMWARE_BASE WIFI_BIN_SHA256 WIFI_CLM_SHA256 WIFI_NVRAM_SHA256
python3 scripts/write-input-manifest.py --verified-fingerprint "$fingerprint" \
    --boot-mode "$RPI4_BOOT_MODE" \
    --output "$RPI4_ARTIFACTS_DIR/input-manifest.json"
printf 'Inputs verified by pinned SHA256; selected one package signer. Bootstrap has no detached signature.\n'
