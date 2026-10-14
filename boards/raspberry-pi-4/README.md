# Raspberry Pi 4

For the **Raspberry Pi 4 Model B**, using Ben Schneider's ARMv8-A Ports packages.
The 8 GiB image has a FAT32 boot partition and ext4 root. Root grows at first
boot. Use a **16 GB or larger** SD card or USB disk.

Physical boot, Ethernet and kernel updates on a Pi remain untested. The existing
checks used offline inspection and QEMU's generic ARM virtual machine.

## Build

Install the [host requirements](../../README.md#build-requirements), then run
from the repository root:

```sh
cd boards/raspberry-pi-4
scripts/build-image.sh
```

The driver assembles and verifies the image. Inputs are pinned in
`configs/sources.env`; the bootstrap uses HTTPS plus a pinned hash, while
installed packages require signatures. The tools container supplies mtools,
whose copied executable needs compatible host runtime libraries.

To repeat verification from this board directory:

```sh
PATH="$PWD/build/tools:$PATH" python3 scripts/verify-images.py
python3 scripts/verify-qemu.py --container arch-rpi4-tools
```

Keep the generated `artifacts/` and `cache/` files for offline verification.
QEMU does not test the Pi EEPROM, EDK2 firmware or physical peripherals.

## Install

Download the image and `SHA256SUMS` from [releases](https://github.com/jiri-one/jiris-sbc-images/releases),
or use your local build's `artifacts/`. In that directory, verify the image and
decompress it if needed:

```sh
sha256sum --ignore-missing -c SHA256SUMS
zstd -d --keep rpi4-archlinux-ports-sd-usb.img.zst
```

From the repository root, configure your public key (replace the image path):

```sh
python3 scripts/configure-image.py /path/to/rpi4-archlinux-ports-sd-usb.img \
  --ssh-key ~/.ssh/id_ed25519.pub --output /path/to/rpi4-configured.img
```

1. Write `rpi4-configured.img` to the **whole** SD card or USB disk
   using an image writer with readback verification. This erases that device.
2. The Pi EEPROM must support GPT and booting from the selected medium;
   follow the [Raspberry Pi bootloader instructions](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#bootloader_update_stable)
   if it needs updating. This image does not update EEPROM.
3. Connect Ethernet, boot, and find hostname `rpi4` in your DHCP leases.
   Log in with `ssh -i ~/.ssh/id_ed25519 rpi@BOARD_IP`.
   Use `sudo` for administration; it does not ask for a password.

EDK2 v1.53 defaults to ACPI and a **3 GiB RAM limit**. On a 4/8 GB board, after
a successful boot, change `Limit RAM to 3GB` in UEFI's Raspberry Pi configuration
and retest boot and storage. See [the firmware documentation](https://github.com/pftf/RPi4/blob/v1.53/Readme.md).
Wi-Fi, GPIO/HATs and graphics acceleration are unverified.

Do not boot with two copies of the same image attached: their disk identifiers match.

## First boot and updates

SSH host keys and machine identity are generated on the device. Pacman initialization
waits for NTP synchronization; SSH remains available while it waits. On the Pi:

```sh
systemctl --failed
findmnt --mountpoint /boot
test -f /var/lib/rpi4/initialized && sudo pacman -Syu
```

If the marker is absent, inspect `journalctl -u rpi4-firstboot -u systemd-timesyncd`.
Package hooks update the kernel and initramfs on the boot partition. If a hook
fails, fix the cause and rerun `sudo rpi4-update-boot` before rebooting.
After changing initramfs configuration, run `sudo mkinitcpio -P` followed by
`sudo rpi4-update-boot`.
Previous boot files are retained, but no previous menu entry is created automatically.
They do not provide a complete system rollback.
