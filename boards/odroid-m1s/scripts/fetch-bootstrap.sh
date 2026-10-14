#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(realpath "$0")")/.."
# shellcheck source=configs/sources.env
source configs/sources.env
mkdir -p cache build/logs
fetch() {
    local url=$1 output=$2
    if [[ ! -s $output ]]; then
        curl --fail --location --retry 3 --connect-timeout 20 \
            --output "$output.partial" "$url"
        mv "$output.partial" "$output"
    fi
}
fetch "$BOOTSTRAP_BASE/$BOOTSTRAP_NAME" "cache/$BOOTSTRAP_NAME"
fetch "$BOOTSTRAP_BASE/$BOOTSTRAP_NAME.sig" "cache/$BOOTSTRAP_NAME.sig"
fetch "$PORTS_KEY_URL" cache/drzee-public.key
printf '%s  %s\n' "$BOOTSTRAP_SHA256" "cache/$BOOTSTRAP_NAME" | sha256sum --check
mkdir -p build/bootstrap-gnupg
chmod 700 build/bootstrap-gnupg
fingerprint=$(gpg --homedir build/bootstrap-gnupg --batch --with-colons \
    --show-keys cache/drzee-public.key | awk -F: '$1 == "fpr" {print $10; exit}')
[[ $fingerprint == "$PORTS_KEY_FINGERPRINT" ]] || {
    printf 'Unexpected Ports signing key: %s\n' "$fingerprint" >&2
    exit 1
}
gpg --homedir build/bootstrap-gnupg --batch --import cache/drzee-public.key
gpg --homedir build/bootstrap-gnupg --batch --status-fd 1 \
    --verify "cache/$BOOTSTRAP_NAME.sig" "cache/$BOOTSTRAP_NAME" \
    > build/logs/bootstrap-signature.log 2>&1
printf 'Bootstrap SHA256 and detached signature verified.\n'
