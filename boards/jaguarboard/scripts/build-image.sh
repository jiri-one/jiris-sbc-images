#!/usr/bin/env bash
set -euo pipefail
umask 077
src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ $# -le 1 ]] || { echo 'Usage: scripts/build-image.sh [NEW_OUTPUT_DIRECTORY]' >&2; exit 2; }
[[ $(uname -m) == x86_64 ]] || { echo 'Build on an x86-64 Linux host.' >&2; exit 1; }
out=${1:-$src/artifacts}
[[ ! -e $out ]] || { echo "Output already exists: $out" >&2; exit 1; }
mkdir -p "$out"
out=$(realpath "$out")
container="arch-jaguar-build-$(date +%s)-$$"
cleanup() { podman rm -f "$container" >/dev/null 2>&1 || true; }
trap cleanup EXIT
mounts=(-v "$src:/src:ro" -v "$out:/out:rw")
base=${JAGUAR_BASE_IMAGE:-docker.io/library/archlinux:base}
podman pull "$base"
podman image inspect "$base" --format '{{.Digest}}' > "$out/base-image-digest.txt"
podman run -d --name "$container" "${mounts[@]}" "$base" sleep infinity >/dev/null
podman exec "$container" bash /src/scripts/configure-root.sh
podman export -o "$out/rootfs.tar" "$container"
podman exec "$container" bash /src/scripts/pack-image.sh
printf '\nRelease image: %s\nConfigure your SSH public key with scripts/configure-image.py before flashing.\n' "$out"
