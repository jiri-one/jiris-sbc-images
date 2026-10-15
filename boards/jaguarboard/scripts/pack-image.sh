#!/usr/bin/env bash
set -euo pipefail
umask 022
root=/tmp/jaguar-root
mkdir "$root"
tar --numeric-owner -xpf /out/rootfs.tar -C "$root"
rm -f "$root/etc/resolv.conf" "$root/etc/hostname" "$root/etc/hosts"
ln -s /run/systemd/resolve/stub-resolv.conf "$root/etc/resolv.conf"
printf 'jaguarboard\n' > "$root/etc/hostname"
printf '127.0.0.1 localhost\n::1 localhost\n127.0.1.1 jaguarboard\n' > "$root/etc/hosts"
root_uuid=$(cat /out/root-uuid)
root_partuuid=$(cat /out/root-partuuid)
fat_id=$(cat /out/fat-id)
truncate -s 256M /out/esp.img
mkfs.fat -F 32 -i "$fat_id" -n JAGUAR_EFI /out/esp.img
mcopy -s -i /out/esp.img "$root/boot/efi/EFI" ::/
rm -rf "$root/boot/efi/EFI"
truncate -s 5886M /out/root.img
mkfs.ext4 -F -L JAGUAR_ROOT -U "$root_uuid" -m 1 \
    -O '^metadata_csum_seed,^orphan_file' -d "$root" /out/root.img
e2fsck -fn /out/root.img
fsck.fat -n /out/esp.img
truncate -s 6144M /out/arch-jaguar.img
sfdisk /out/arch-jaguar.img <<EOF
label: gpt
unit: sectors
start=2048, size=524288, type=U, name="JAGUAR_EFI"
start=526336, size=12054528, type=L, name="JAGUAR_ROOT", uuid=$root_partuuid
EOF
dd if=/out/esp.img of=/out/arch-jaguar.img bs=1M seek=1 conv=notrunc,sparse status=none
dd if=/out/root.img of=/out/arch-jaguar.img bs=1M seek=257 conv=notrunc,sparse status=none
cd /out
sha256sum arch-jaguar.img > SHA256SUMS
zstd -T2 -3 -f arch-jaguar.img
sha256sum arch-jaguar.img.zst >> SHA256SUMS
rm rootfs.tar esp.img root.img root-uuid root-partuuid fat-id
