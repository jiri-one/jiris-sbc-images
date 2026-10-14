#!/usr/bin/env bash
# Runs as namespace root in the dedicated rootless Podman container.
set -euo pipefail
cd /work
export QEMU_CPU=cortex-a72
ROOT=/work/build/rootfs
[[ -d $ROOT/usr && -f /work/cache/ports-public.key ]] || exit 1
cleanup() {
    chroot "$ROOT" /usr/bin/gpgconf --homedir /etc/pacman.d/gnupg --kill all >/dev/null 2>&1 || true
    rm -f "$ROOT/etc/pacman.d/hooks/21-systemd-tmpfiles.hook"
    for dir in run sys proc dev; do
        umount -R "$ROOT/$dir" 2>/dev/null || true
    done
    umount "$ROOT" 2>/dev/null || true
}
trap cleanup EXIT
mount --bind "$ROOT" "$ROOT"
mount --rbind /dev "$ROOT/dev"
mount --make-rslave "$ROOT/dev"
mount -t proc proc "$ROOT/proc"
mount --rbind /sys "$ROOT/sys"
mount --make-rslave "$ROOT/sys"
mount -t tmpfs tmpfs "$ROOT/run"
rm -f "$ROOT/etc/resolv.conf"
cp /etc/resolv.conf "$ROOT/etc/resolv.conf"
cp configs/pacman.conf "$ROOT/etc/pacman.conf"
cp cache/ports-public.key "$ROOT/tmp/ports-public.key"
mkdir -p "$ROOT/usr/share/rpi4"
cp cache/ports-public.key "$ROOT/usr/share/rpi4/ports-public.key"
mkdir -p "$ROOT/etc/pacman.d/hooks"
cp -a overlay/. "$ROOT/"
# The rootless container's device nodes belong to the host. Build-time tmpfiles
# must not try to change their ownership. The override is removed on exit.
cp "$ROOT/usr/share/libalpm/hooks/21-systemd-tmpfiles.hook" "$ROOT/etc/pacman.d/hooks/21-systemd-tmpfiles.hook"
sed -i 's|^Exec = .*|Exec = /usr/bin/systemd-tmpfiles --create --exclude-prefix=/dev|' \
    "$ROOT/etc/pacman.d/hooks/21-systemd-tmpfiles.hook"
chmod 440 "$ROOT/etc/sudoers.d/10-wheel"
chmod +x "$ROOT/usr/local/sbin/"* 2>/dev/null || true
chroot "$ROOT" /bin/bash -euo pipefail <<'CHROOT'
export LC_ALL=C
export RPI4_IMAGE_BUILD=1
if [[ ! -s /etc/pacman.d/gnupg/pubring.gpg ]]; then
    pacman-key --init
    pacman-key --populate archlinux
fi
pacman-key --add /tmp/ports-public.key
pacman-key --lsign-key C43E3B9B9CE3F35E97AEF93873556FFF735F691D
# QEMU user emulation does not implement the host's Landlock interface.
# This switch affects only this build invocation, never the shipped config.
pacman --disable-sandbox -Syu --noconfirm --needed base linux linux-dtbs linux-firmware-broadcom \
    mkinitcpio openssh sudo nano python zstd cloud-guest-utils dosfstools
printf 'rpi4\n' > /etc/hostname
printf 'en_US.UTF-8 UTF-8\ncs_CZ.UTF-8 UTF-8\n' > /etc/locale.gen
locale-gen
printf 'LANG=en_US.UTF-8\n' > /etc/locale.conf
printf 'KEYMAP=us\n' > /etc/vconsole.conf
ln -sf /usr/share/zoneinfo/Europe/Paris /etc/localtime
if ! id rpi >/dev/null 2>&1; then
    useradd --create-home --groups wheel --shell /bin/bash rpi
fi
passwd -l root
systemctl enable systemd-networkd systemd-resolved systemd-timesyncd sshd
systemctl enable rpi4-firstboot rpi4-firstboot.timer rpi4-grow-root
ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
rm -f /tmp/ports-public.key
mkinitcpio -P
kver=$(basename "$(dirname "$(pacman -Qql linux | sed -n '\|^/usr/lib/modules/[^/]*/vmlinuz$|p')")")
printf '%s\n' "$kver" > /usr/share/rpi4/initramfs-kernel
CHROOT
# Ensure the final image uses the sandbox settings in the shipped config.
cmp configs/pacman.conf "$ROOT/etc/pacman.conf"
chroot "$ROOT" /usr/bin/pacman -Q > build/package-manifest.txt
printf 'Root filesystem packages installed and configured.\n'
