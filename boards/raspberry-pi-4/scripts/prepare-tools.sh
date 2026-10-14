#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(realpath "$0")")/.."
source configs/sources.env
# Dedicated rootless tools container; privilege is scoped to its user namespace.
# Image tooling selects ordinary project files, never physical destination disks.
if ! podman container exists arch-rpi4-tools; then
    podman run -d --name arch-rpi4-tools --privileged --volume "$PWD:/work" "$TOOLS_BASE" sleep infinity
else
    mounted_project=$(podman inspect --format '{{range .Mounts}}{{if eq .Destination "/work"}}{{.Source}}{{end}}{{end}}' arch-rpi4-tools)
    [[ $mounted_project == "$PWD" ]] || { echo 'Existing arch-rpi4-tools belongs to a different project path.' >&2; exit 1; }
    expected_image_id=$(podman image inspect "$TOOLS_BASE" --format '{{.Id}}') || {
        printf 'Cannot inspect pinned tools image locally: %s\nMake it available with podman pull, then retry; the existing container was not changed.\n' "$TOOLS_BASE" >&2
        exit 1
    }
    container_image_id=$(podman inspect --format '{{.Image}}' arch-rpi4-tools)
    [[ -n $expected_image_id && ${container_image_id#sha256:} == "${expected_image_id#sha256:}" ]] || {
        echo 'Existing arch-rpi4-tools uses a different image than TOOLS_BASE. Rename it to preserve it, then rerun to create a container from the pinned base; no container was changed.' >&2
        exit 1
    }
    [[ $(podman inspect --format '{{.State.Running}}' arch-rpi4-tools) == true ]] || podman start arch-rpi4-tools
fi
podman exec arch-rpi4-tools bash -euc 'apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends mtools qemu-system-arm qemu-utils qemu-efi-aarch64 openssh-client python3 e2fsprogs fdisk dosfstools zstd ca-certificates'
mkdir -p build/tools build/logs
podman cp arch-rpi4-tools:/usr/bin/mtools build/tools/mtools
for tool in mcopy mdir mtype mmd; do ln -sf mtools "build/tools/$tool"; done
podman exec arch-rpi4-tools dpkg-query -W > build/logs/tools-package-manifest.txt
if ! build/tools/mcopy -V >/dev/null; then
    echo 'Copied mcopy cannot run on this host. Check build/tools/mtools runtime dependencies and use compatible host tooling before retrying.' >&2
    exit 1
fi
