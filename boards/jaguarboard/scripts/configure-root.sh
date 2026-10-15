#!/usr/bin/env bash
set -euo pipefail
umask 022
username=jaguar
# Restore a normal package configuration instead of the container's NoExtract rules.
cat > /etc/pacman.conf <<'EOF'
[options]
HoldPkg = pacman glibc
Architecture = auto
SigLevel = Required DatabaseOptional
LocalFileSigLevel = Optional
ParallelDownloads = 3
[core]
Include = /etc/pacman.d/mirrorlist
[extra]
Include = /etc/pacman.d/mirrorlist
EOF
pacman -Syu --noconfirm base glibc linux mkinitcpio efibootmgr intel-ucode \
    linux-firmware-intel linux-firmware-realtek openssh sudo zram-generator \
    nano htop usbutils pciutils ethtool memtester rsync e2fsprogs dosfstools mtools wireless-regdb fbset
# The container base does not include the generated hardware database.
systemd-hwdb update
# The world domain avoids a failing regulatory helper before a country is chosen.
printf 'WIRELESS_REGDOM="00"\n' > /etc/conf.d/wireless-regdom
install -m 644 /src/configs/mkinitcpio.conf /etc/mkinitcpio.conf
tar -C /src/overlay --mode='u=rwX,go=rX' -cf - . |
    tar -C / -xf - --no-same-owner --no-overwrite-dir
# A single generic initramfs is sufficient; its drivers are not host-specific.
cat > /etc/mkinitcpio.d/linux.preset <<'EOF'
ALL_config="/etc/mkinitcpio.conf"
ALL_kver="/boot/vmlinuz-linux"
PRESETS=('default')
default_image="/boot/initramfs-linux.img"
EOF
kernel_dirs=(/usr/lib/modules/*)
[[ ${#kernel_dirs[@]} == 1 && -f ${kernel_dirs[0]}/vmlinuz ]]
install -m 644 "${kernel_dirs[0]}/vmlinuz" /boot/vmlinuz-linux
printf 'KEYMAP=us\n' > /etc/vconsole.conf
mkinitcpio -P
rm -f /boot/initramfs-linux-fallback.img
ln -sf /usr/share/zoneinfo/UTC /etc/localtime
printf 'en_US.UTF-8 UTF-8\n' > /etc/locale.gen
locale-gen
printf 'LANG=en_US.UTF-8\n' > /etc/locale.conf
mkdir -p /etc/systemd/network /etc/systemd/journald.conf.d /etc/ssh/sshd_config.d
cat > /etc/systemd/network/20-wired.network <<'EOF'
[Match]
Type=ether
[Network]
DHCP=yes
[DHCPv4]
RouteMetric=100
UseHostname=no
[DHCPv6]
UseHostname=no
EOF
cat > /etc/systemd/zram-generator.conf <<'EOF'
[zram0]
zram-size = min(ram, 1024)
compression-algorithm = lz4
swap-priority = 100
EOF
cat > /etc/systemd/journald.conf.d/20-jaguar.conf <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=48M
RuntimeMaxUse=16M
EOF
mkdir -p /etc/sysctl.d
printf 'vm.swappiness=100\n' > /etc/sysctl.d/90-jaguar.conf
cat > /etc/ssh/sshd_config.d/20-jaguar.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
useradd -m -U -u 1000 -G wheel -s /bin/bash "$username"
passwd -l "$username"
passwd -l root
printf '%%wheel ALL=(ALL:ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/10-wheel
chmod 440 /etc/sudoers.d/10-wheel
visudo -cf /etc/sudoers
systemctl enable sshd getty@tty1.service systemd-networkd systemd-resolved systemd-timesyncd \
    jaguar-firstboot.service jaguar-firstboot.timer
# Allow the stock PAM stack to activate homed on demand.
ln -sfn /usr/lib/systemd/system/systemd-homed.service /etc/systemd/system/dbus-org.freedesktop.home1.service
ln -sfn /usr/lib/systemd/system/getty@.service /etc/systemd/system/autovt@.service
systemctl set-default multi-user.target
root_uuid=$(cat /proc/sys/kernel/random/uuid)
root_partuuid=$(cat /proc/sys/kernel/random/uuid)
fat_id=$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n' | tr 'a-f' 'A-F')
printf '%s\n' "$root_uuid" > /out/root-uuid
printf '%s\n' "$root_partuuid" > /out/root-partuuid
printf '%s\n' "$fat_id" > /out/fat-id
cat > /etc/fstab <<EOF
UUID=$root_uuid / ext4 defaults,noatime 0 1
UUID=${fat_id:0:4}-${fat_id:4:4} /boot/efi vfat defaults,umask=0077 0 2
EOF
cat > /etc/jaguarboard-image.conf <<EOF
ROOT_PARTUUID=$root_partuuid
IMAGE_FLAVOUR=usb-emmc
EOF
jaguar-update-boot "$root_uuid"
cat > /etc/motd <<'EOF'
Arch Linux for JaguarBoard
Install this system onto internal eMMC: sudo jaguar-install-emmc
Inspect memory: free -h; zramctl
Collect diagnostics: sudo jaguar-health-snapshot
The installer erases its selected target only after explicit confirmation.
EOF
pacman -Q > /out/packages.txt
find /var/cache/pacman/pkg -mindepth 1 -delete
# Every installed board creates its own local package-keyring key after NTP sync.
rm -rf /etc/pacman.d/gnupg
install -d -m 755 /var/lib/systemd/timesync
touch /var/lib/systemd/timesync/clock
rm -f /etc/ssh/ssh_host_* /var/lib/systemd/random-seed
: > /etc/machine-id
rm -f /var/lib/dbus/machine-id
ln -s /etc/machine-id /var/lib/dbus/machine-id
