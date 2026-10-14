#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(realpath "$0")")/.."
source configs/sources.env
mkdir -p cache build/logs build/firmware build/input-gnupg
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
fetch "$FIRMWARE_URL" "cache/$FIRMWARE_NAME"
printf '%s  %s\n' "$BOOTSTRAP_SHA256" "cache/$BOOTSTRAP_NAME" "$FIRMWARE_SHA256" "cache/$FIRMWARE_NAME" | sha256sum --check
# The published key URL contains a bundle. Import into an isolated keyring;
# export ONLY the selected public key. No ownertrust is assigned to the bundle.
gpg --homedir build/input-gnupg --batch --import-options import-minimal \
    --import cache/ben-key-bundle.asc > build/logs/input-key-import.log 2>&1
gpg --homedir build/input-gnupg --batch --armor --export "$PORTS_KEY_FINGERPRINT" > cache/ports-public.key
fingerprint=$(gpg --homedir build/input-gnupg --batch --with-colons --show-keys cache/ports-public.key | awk -F: '$1=="fpr" {print $10; exit}')
[[ $fingerprint == "$PORTS_KEY_FINGERPRINT" ]]
[[ $(gpg --homedir build/input-gnupg --batch --with-colons --show-keys cache/ports-public.key | awk -F: '$1=="pub" {n++} END {print n}') == 1 ]]
bsdtar -xf "cache/$FIRMWARE_NAME" -C build/firmware
[[ -s build/firmware/RPI_EFI.fd && -s build/firmware/config.txt ]]
# Record the values sourced above, after download hashes and the selected key
# have passed their checks. A clean checkout does not contain build artifacts.
export BOOTSTRAP_NAME BOOTSTRAP_BASE BOOTSTRAP_SHA256 PORTS_KEY_URL PORTS_KEY_FINGERPRINT
export FIRMWARE_NAME FIRMWARE_URL FIRMWARE_SHA256 TOOLS_BASE
python3 scripts/write-input-manifest.py --verified-fingerprint "$fingerprint"
printf 'Inputs verified by pinned SHA256; selected one package signer. Bootstrap has no detached signature.\n'
