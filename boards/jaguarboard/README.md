# JaguarBoard

For the original **Intel Atom Z3735G JaguarBoard**, with 1 GB RAM and 16 GB eMMC.
Arch Linux x86_64 with the current `linux` kernel, systemd-boot, Ethernet, SSH,
zram and a readable console font. The 6 GiB image fits an **8 GB USB drive**
and can install itself to eMMC.

Use **UEFI boot with Secure Boot disabled**. The same image works with **32-bit
or 64-bit UEFI**: USB and installed eMMC include both loaders, and firmware
selects the matching one. Linux is always 64-bit (`x86_64`), even with 32-bit
UEFI. Legacy BIOS/CSM boot is not supported.

An existing eMMC installation boots with systemd-boot and the current kernel
on v1.3 hardware. It passed a [72-hour stability test](#72-hour-stability-test)
with a Samsung USB charger. The new USB image still needs testing on hardware.

## Build

Use an x86-64 Linux host with rootless Podman and Bash. From the repository root:

```sh
cd boards/jaguarboard
scripts/build-image.sh
```

The output directory `artifacts/` must not exist; pass a different new directory
as the sole argument if needed. The builder uses the official Arch base container
and records its digest and package versions. `JAGUAR_BASE_IMAGE` can pin the
container by digest; package repositories remain rolling.

## Install

Download the [image](https://github.com/jiri-one/jiris-sbc-images/releases/download/2026.10.15.2/arch-jaguar.img.zst) and
[`SHA256SUMS`](https://github.com/jiri-one/jiris-sbc-images/releases/download/2026.10.15.2/SHA256SUMS) from the newest release, or use
your local build's `artifacts/`. In that directory, verify and decompress the
image if needed:

```sh
sha256sum --ignore-missing -c SHA256SUMS
zstd -d --keep arch-jaguar.img.zst
```

From the repository root, configure your SSH public key using the
[configuration tool and its host requirements](../../README.md#images-and-login):

```sh
python3 scripts/configure-image.py /path/to/arch-jaguar.img \
  --ssh-key ~/.ssh/id_ed25519.pub --output /path/to/jaguar-configured.img
```

Write `jaguar-configured.img` to the **whole USB drive**, erasing its contents.
Connect Ethernet, select the USB drive's **UEFI** entry in firmware setup and
find `jaguarboard` in your DHCP leases. Log in with `ssh jaguar@BOARD_IP`;
`sudo` needs no password.
Enable console login with `sudo passwd jaguar` over SSH. Defaults are a US
keyboard, UTC and a font sized for the active display. To override it, set `FONT`
in `/etc/vconsole.conf`, then rebuild the initramfs and boot files as described
under [Updates](#updates). Direct SD-slot boot is untested.

To install on eMMC, stop applications writing data and run:

```sh
sudo jaguar-install-emmc
```

The installer shows the detected disk and asks for an exact confirmation such
as `ERASE /dev/mmcblk1`. **All data on that disk is erased.** Device numbers can
change across boots. The running system disk and disks in use are rejected.
The copy fills the eMMC, gets new disk identifiers and preserves your login.

After installation, run `sudo poweroff`, remove USB and start the board.
If necessary, select **Arch Linux (eMMC)** or the eMMC's `EFI/BOOT/BOOTX64.EFI`
(`BOOTIA32.EFI` for 32-bit firmware) in firmware setup. Check `findmnt /` and
`findmnt /boot/efi` to confirm eMMC boot.
Firmware bitness is shown by `cat /sys/firmware/efi/fw_platform_size` (`32` or `64`).

## Updates

First boot initializes the pacman keyring after NTP synchronization. Before
updating, check `test -f /var/lib/jaguarboard/initialized`; if absent, inspect
`journalctl -u jaguar-firstboot -u systemd-timesyncd`. SSH remains available.
Keep the EFI partition mounted and writable at `/boot/efi`, then run:

```sh
sudo pacman -Syu
```

Package hooks regenerate the initramfs and refresh both EFI loaders and the
kernel files on the EFI partition. Boot-package changes are blocked if that
partition is unmounted or read-only. After changing initramfs settings, run
`sudo mkinitcpio -P` followed by `sudo jaguar-update-boot`. Resolve errors before
rebooting; updates need free EFI space to stage a new kernel and initramfs.

For an optional alternative kernel, `sudo pacman -S linux-lts` adds its boot
entries. To make it the default, set `JAGUAR_BOOT_KERNEL=linux-lts` in
`/etc/default/jaguar-boot` and run `sudo jaguar-update-boot`.

Intel graphics loads in the initramfs, before the login prompt. For a
monitor-specific mode, set e.g. `JAGUAR_BOOT_OPTIONS='video=HDMI-A-1:1920x1080@60'`
in `/etc/default/jaguar-boot` and run `sudo jaguar-update-boot`. Use a mode listed
in `/sys/class/drm/card*-HDMI-A-1/modes`; the image leaves mode selection automatic.

## Stability

### 72-hour stability test

**Passed, 17–20 October 2026**, on the existing eMMC installation with Arch Linux
x86_64 and kernel `7.2.9-arch1-1`. After reading
[community reports of power-supply problems](https://web.archive.org/web/20170503022535/http://www.jaguarboard.org/index.php/forum/troubleshooting/607-debian-crashing.html?start=12),
we tested with a **Samsung EP-TA200 USB charger**, rated **5 V / 2 A**.

- **RAM:** 36 memtester passes, each checking 368–384 MiB of locked memory.
- **CPU and RAM:** 36 hours of verified stress-ng load on all four cores,
  plus 360 load/idle cycles.
- **Result:** no reboot, detected memory or hardware errors, OOM or thermal
  throttling. Peak temperature: **67 °C**.

Deep C7/C7S idle states were used during the test;
`intel_idle.max_cstate=1` was not used.

### Diagnostics and workarounds

For boards affected by [Bay Trail idle-state issues](https://web.archive.org/web/20170503022532/http://www.jaguarboard.org/index.php/forum/troubleshooting/414-possible-explanation-for-freezing.html),
an optional workaround is available:

Normal boot is first and the default. In `/etc/default/jaguar-boot`, set
`JAGUAR_BOOT_DEFAULT=1` to try `intel_idle.max_cstate=1`, or `2` for `nomodeset`,
then run `sudo jaguar-update-boot`; `0` restores normal boot. The C-state workaround
may increase power use.

Automatic probes for nonresponding tablet audio, battery and touch devices on
the tested v1.3 board are disabled in `/etc/modprobe.d/jaguar-tablet-devices.conf`.
USB and HDMI audio remain enabled. Some firmware and USB audio warnings remain;
normal boot keeps them off the console with `loglevel=3`, while the full kernel
log remains in `journalctl -b -k`. Basic display boot is verbose; to keep native
graphics and show more messages, set `JAGUAR_BOOT_OPTIONS='loglevel=7'` and update
the boot files.

Optional minute-by-minute memory, temperature and OOM logging:

```sh
sudo systemctl enable --now jaguar-health.timer
journalctl -u jaguar-health.service
journalctl -b -1 -k
```

Persistent journals are limited to 48 MiB. Stop sampling with
`sudo systemctl disable --now jaguar-health.timer`.
