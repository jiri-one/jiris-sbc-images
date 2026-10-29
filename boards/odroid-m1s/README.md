# ODROID-M1S

Arch Linux Ports from drzee.net for the M1S's Cortex-A55, booted by U-Boot and
extlinux. The default **6 GiB** image contains the bootloader, kernel and ext4
system for SD or built-in eMMC. Use an **8 GB or larger** target.

Earlier SD and personalized eMMC installations booted on hardware with Ethernet
and SSH. The 2026.10.29 image passed offline checks and four generic QEMU scenarios,
including SSH login, sudo and first-boot initialization. Its kernel, U-Boot and
boot updater match the board in production. This image has not been flashed to
hardware. See the [license and source NOTICE](../../notices/2026.10.29-m1s/NOTICE.md).
This board is not the ODROID-M1.

## Build

Install the [host requirements](../../README.md#build-requirements), then run
from the repository root:

```sh
cd boards/odroid-m1s
scripts/build-image.sh
python3 scripts/verify-images.py
```

Package sources are in `configs/sources.env` and `configs/pacman.conf`.
Bootstrap signatures, its pinned hash and package signatures are checked.
U-Boot and Rockchip firmware versions are pinned in `scripts/build-uboot.sh`.
Keep `build/uboot/` and the generated artifact metadata for offline verification.
If the default U-Boot builder belongs to another checkout, set
`M1S_UBOOT_CONTAINER` to a different container name before building.

Optional QEMU boot check from this board directory; install `qemu-system-aarch64`,
`qemu-img` and `timeout` on the host first:

```sh
scripts/verify-qemu.sh artifacts/odroid-m1s-archlinux-ports-sd-emmc.img
```

This uses a generic virtual board, not M1S hardware or U-Boot. The guest needs
working NTP for the full first-boot check. For `--container`, the prepared
container needs QEMU, this board directory at `/work` read-only, and `build/`
at `/testbuild` writable.

## Install

Download the [image](https://github.com/jiri-one/jiris-sbc-images/releases/download/2026.10.29/odroid-m1s-archlinux-ports-sd-emmc.img.zst) and
[`SHA256SUMS`](https://github.com/jiri-one/jiris-sbc-images/releases/download/2026.10.29/SHA256SUMS) from the newest release,
or use your local build's `artifacts/`. In that directory, verify the image and
decompress it if needed:

```sh
sha256sum --ignore-missing -c SHA256SUMS
zstd -d --keep odroid-m1s-archlinux-ports-sd-emmc.img.zst
```

From the repository root, configure your public key (replace the image path):

```sh
python3 scripts/configure-image.py /path/to/odroid-m1s-archlinux-ports-sd-emmc.img \
  --ssh-key ~/.ssh/id_ed25519.pub --output /path/to/m1s-configured.img
```

Write `m1s-configured.img` to the **whole** SD card using an image writer with
readback verification. This erases the selected device.

For eMMC, [Hardkernel's EMMC2UMS utility](https://dn.odroid.com/RK3566/ODROID-M1S/Installer/ODROID-M1S_EMMC2UMS.img)
can expose it to a PC as a USB disk:

1. Write the utility to a temporary SD card and boot the M1S from it.
2. Use the board's normal power supply and connect its **Micro USB OTG** port
   to the PC with a data cable. Identify the exposed eMMC disk.
3. Write the same `m1s-configured.img` to that disk and verify the write.
4. Safely eject it, power down, remove the utility SD and disconnect OTG.
   Connect Ethernet and power on to boot from eMMC.

This USB route is [documented by the HAOS maintainers](https://developers.home-assistant.io/docs/operating-system/boards/odroid-m1s/#emmc)
but was not physically tested here; the tested eMMC install was written from
a running SD system. The utility may no longer boot after replacing the vendor
bootloader, so keep a working Linux SD card for recovery.

Find the board in your DHCP leases and log in with
`ssh -i ~/.ssh/id_ed25519 odroid@BOARD_IP`. Use `sudo` for administration;
it does not ask for a password. Do not boot with two copies of
the same image attached: their identifiers match. For local builds,
`scripts/prepare-install-image.py` can create a copy with different identifiers;
run it before configuring the SSH key and see its `--help` for required metadata.

## NVMe data and updates

The default system stays on SD/eMMC. An ext4 data partition labelled `M1S_DATA`
automounts at `/mnt/nvme`; boot also works without it. Existing data can instead
be mounted by its UUID in `/etc/fstab`. The image does not format the NVMe drive.

First boot grows root and creates SSH host keys and the local pacman keyring. Keyring
initialization waits for NTP. On the M1S:

```sh
systemctl --failed
findmnt --mountpoint /boot
test -f /var/lib/odroid-m1s/initialized && sudo pacman -Syu
```

If the marker is absent, inspect `journalctl -u odroid-m1s-firstboot -u systemd-timesyncd`.
Package hooks update the kernel, DTB and initramfs; U-Boot is not reflashed by
pacman. After changing initramfs configuration, run `sudo mkinitcpio -P` and
`sudo odroid-m1s-update-boot`. Resolve hook errors before rebooting. The retained
previous boot entry is a limited rescue option, not a complete system rollback.
