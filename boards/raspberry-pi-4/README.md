# Raspberry Pi 4

**Native Raspberry Pi boot is the default and the maintained boot path.** These
images use community **Arch Linux Ports AArch64**, specifically Ben Schneider's
ARMv8-A packages and the mainline Ports kernel. They do not use Arch Linux ARM.
The 8 GiB image has a FAT32 boot partition and ext4 root, which grows on first
boot. Use a **16 GB or larger** SD card or USB disk.

This is a personal image project, shared for others who may find it useful.
Maintenance follows the hardware and configurations I actually use; this is
not a commitment to maintain parallel variants. The former
[UEFI + systemd-boot variant](docs/UEFI-LEGACY.md) is **deprecated**. There are
no planned UEFI builds, updates or further tuning unless a concrete need arises.
Its existing release may be the last, and retained source may eventually be removed.

## Why native boot

On the tested Pi 4, native boot was noticeably faster and reached a readable
login prompt without the long black-screen interval seen with UEFI. It selects
the monitor's native resolution through VC4 and uses large, adaptive console
fonts. No boot or console problems were observed in normal use on this setup.
UEFI's fullscreen display was fixed, but its slow menu response remains unresolved.

| Observation on the same Pi 4B 8 GB | Native | Earlier UEFI setup |
| --- | --- | --- |
| Startup | About 37 s from reboot request to SSH; Linux itself reported 7.8 s | Firmware/loader/Linux reported about 63 s, including about 46 s in the loader |
| Settled idle draw at the wall | 2.7–2.9 W | 3.3–3.4 W |
| Console | Automatic 3840 × 2160 VC4 output, readable font | Readable console after tuning; UEFI menus still responded slowly |
| CPU performance | Four cores, 600 MHz idle to 1800 MHz under load | Device Tree mode also allowed 600–1800 MHz |

These are observations, not a controlled benchmark: the cards differed, and the
startup timers have different boundaries. Native's timer includes shutdown and
SSH polling; UEFI's reported startup does not. Power was measured with a socket
wattmeter and the original Raspberry Pi PSU, Ethernet, HDMI and a USB keyboard.
The lower native reading preceded PowerTOP installation and cannot be attributed
to that tool or to one isolated setting.

Physical SD boot, normal reboot, full RAM, four-core load, Ethernet, SSH and the
4K console have been checked. Memory and SD/network transfers passed with
4.5 GiB allocated; a later [48-hour test](#48-hour-stability-test) covered
6 GiB memory passes and Wi-Fi with additional firmware and configuration.
A kernel upgrade from 7.2.9 to 7.2.11 and the [kernel fallback](#kernel-fallback)
were checked afterwards. Physical USB-root boot, unattended recovery, Bluetooth,
GPIO/HATs and graphics acceleration remain unverified. Repeated keyboard reconnection across USB 2
and USB 3 ports, plus HDMI unplug/replug, passed on this setup without a reboot.
The final generic image passed offline and generic QEMU checks;
the physical tests used the maintained installation, rather than flashing that
final generic image. The image builder now installs the tested Wi-Fi firmware
and driver settings, but not the test's keepalive; see [Wi-Fi](#wi-fi).

Existing release downloads predate these native-boot, console and power changes.
Build from current source to include them until a corresponding native release
is published.

## 48-hour stability test

**CPU/RAM: PASS**, completed on 20 October 2026 at 22:19 UTC
(21 October at 00:19 CEST), after 48 uninterrupted hours. The tested Pi 4B 8 GB
used native SD boot, the original Raspberry Pi PSU and Arch Linux Ports
mainline kernel **7.2.9-arch1-1**, running headless with Ethernet for management.
The board used a **passively cooled Joy-Pi Armor case, without a fan**; the
temperature result below applies to this cooling setup.

| Check | Result |
| --- | --- |
| Complete blocks | 12/12, including every workload and idle phase |
| Memtester | 12 full passes, 216 subtests; 6144 MiB locked per pass |
| Combined CPU/VM | 12 one-hour runs with four CPU workers and 4096 MiB locked VM memory |
| Load/idle cycles | 120 cycles of 10 s CPU load and 20 s idle; 20 minutes of additional CPU load |
| Maximum temperature | 70.1 °C |
| Errors | No reboot, RAM errors, undervoltage, OOM or new kernel errors; all workload exit codes zero |

All 144 workload logs and the system journal were checked. Repeated 6 GiB
allocations do not establish coverage of every physical address in the board's
8 GB RAM. This result applies to this cooling setup, PSU, SD and system
configuration, including the Wi-Fi additions below, rather than every future
build or storage setup.

**Wi-Fi:** the same mainline kernel used Raspberry Pi Wi-Fi firmware **7.45.265**
with its CLM and board NVRAM files, `brcmfmac` options
`roamoff=1 feature_disable=0x282000`, power saving **off**, and a gateway
keepalive once per minute. These were manual additions to the maintained
installation. The image builder now installs the firmware and driver settings;
Wi-Fi stays blocked until enabled. See [Wi-Fi](#wi-fi), including why the
keepalive is not part of the image.

The observer sent three gateway pings over `wlan0` each minute:

| Phase | Sent / received | Highest RTT |
| --- | --- | --- |
| CPU/VM | 2139 / 2139 | 19.618 ms |
| Memtester | 3168 / 3168 | 8.421 ms |
| Idle | 3243 / 3243 | 6.732 ms |
| Short CPU cycles | 54 / 54 | 6.086 ms |
| Probes spanning phase transitions | 15 / 15 | 9.090 ms |
| **Total** | **8619 / 8619; zero loss** | **19.618 ms** |

No disconnect/reconnect events or missing probes were recorded after the
observer started; all 2873 observations reported power saving off. The first
468 seconds had no observer probes. Keepalive and probes kept the link active,
so this does not test complete Wi-Fi inactivity or establish continuous hosting
availability; brief disruptions between probes can be missed. Router-access
settings were changed during the run, with no outage observed; the exact
settings and time were not recorded.

## Build

Install the [host requirements](../../README.md#build-requirements) and `dtc`
(the device-tree compiler), then run from the repository root:

```sh
cd boards/raspberry-pi-4
scripts/build-image.sh
```

This builds native boot. The image is
`artifacts/native/rpi4-archlinux-ports-native-sd-usb.img`; compression, manifests
and checksums are in the same directory. `--boot-mode native` is an optional
explicit spelling. Historical UEFI builds require `--boot-mode uefi` and keep
their separate `artifacts/` output.

To reuse an already prepared package set without upgrades:

```sh
scripts/build-image.sh --reuse-rootfs /absolute/prepared/rootfs
```

This clones the supplied tree into a new `build/native/rootfs`, refreshes the
native boot configuration and initramfs, and clears per-device identities and
SSH keys. The destination must not already exist. The supplied tree must contain
all current required packages, including `powertop` and `iw`; reuse mode does
not install missing packages.

The driver assembles and verifies the image. Inputs are pinned in
`configs/sources.env`; the bootstrap uses HTTPS plus a pinned hash, while
installed packages require signatures. The tools container supplies mtools,
whose copied executable needs compatible host runtime libraries.

To repeat verification from this board directory:

```sh
PATH="$PWD/build/tools:$PATH" python3 scripts/verify-images.py
python3 scripts/verify-qemu.py --container arch-rpi4-tools
```

Both default to the native image. QEMU boots its selected kernel and initramfs
on generic `virt` hardware, checking userspace, storage growth and SSH. It does
not emulate the Pi EEPROM, Raspberry Pi firmware, VC4 display or physical
peripherals. Keep generated `artifacts/` and `cache/` files for offline verification.

## Boot and firmware

The boot chain is:

```text
Pi EEPROM → Raspberry Pi runtime firmware → mainline ARM64 kernel + Device Tree
           → initramfs → Arch Linux Ports
```

The matched `start4.elf` and `fixup4.dat` pair comes from the official
[Raspberry Pi firmware 1.20260915 release](https://github.com/raspberrypi/firmware/releases/tag/1.20260915),
pinned by commit and SHA256. Native boot uses the matching Pi 4 Device Tree from
`linux-dtbs`. The updater validates and extracts the ARM64 kernel from the Ports
package's EFI zboot executable, prepares a compressed `kernel8.img`, and selects
a complete kernel/DTB/initramfs set through `/boot/native-current.txt`. The native
boot partition contains neither EDK2 nor systemd-boot and has no EDK2 RAM limit.

For an intentional runtime-firmware update, update the verified release/commit
and both hashes in `configs/sources.env`, then rebuild. On an installed native
card, replacing `start4.elf` and `fixup4.dat` also requires updating the version,
commit and verified hashes in `/usr/share/rpi4/runtime-firmware.json`. Keep the
pair matched; the boot updater rejects files that differ from this manifest.
Back up the current pair and manifest, then successfully run `rpi4-update-boot`
after the change and before the next kernel upgrade or reboot. This does not
update the separate EEPROM firmware.

The lean initramfs explicitly includes Pi 4 SD/USB storage, ext4/FAT, firmware
and CPU-frequency providers, VC4, and QEMU virtio devices. It retains keyboard,
console-font, systemd and filesystem-check hooks while omitting the broad
`block` and `filesystems` hooks. The full installed module tree is available
after mounting root. Revisit `/etc/mkinitcpio.conf` if changing storage or
filesystem, then rebuild the boot files as described below.

USB-C is configured for **power input only**. The local
`rpi4-usb-c-data-disable` overlay disables its data controller; rear USB-A ports
and Ethernet remain enabled. To enable USB-C data, remove its overlay line from
`/boot/config.txt` and use a kernel providing DWC2 support,
`CONFIG_RASPBERRYPI_FIRMWARE=y` and `CONFIG_RASPBERRYPI_POWER=y`. Ports 7.2.9
lacks this power-domain driver, which otherwise causes a probe timeout. USB
gadget use also requires a device role and gadget function. See the
[kernel power-domain requirements](https://github.com/gregkh/linux/blob/v7.2.9/drivers/pmdomain/bcm/Kconfig).

EEPROM is separate from those SD-card runtime files. The tested board was
updated to the official 2026-09-23 EEPROM with its boot configuration preserved.
**Building or booting this image does not update EEPROM.** Use the
[Raspberry Pi bootloader instructions](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#bootloader_update_stable)
if your board needs GPT or USB boot support.

## Install

Use your local build's `artifacts/native/`, or a matching native image and
`SHA256SUMS` once available from
[releases](https://github.com/jiri-one/jiris-sbc-images/releases). In that directory:

```sh
sha256sum --ignore-missing -c SHA256SUMS
```

If the uncompressed `.img` is not already present, decompress it:

```sh
zstd -d --keep rpi4-archlinux-ports-native-sd-usb.img.zst
```

From the repository root, configure your public key (replace the image path):

```sh
python3 scripts/configure-image.py /path/to/rpi4-archlinux-ports-native-sd-usb.img \
  --ssh-key ~/.ssh/id_ed25519.pub --output /path/to/rpi4-configured.img
```

1. Write `rpi4-configured.img` to the **whole** SD card or USB disk using an image
   writer with readback verification. This erases that device.
2. Connect Ethernet, boot, and find hostname `rpi4` in your DHCP leases.
3. Log in with `ssh -i ~/.ssh/id_ed25519 rpi@BOARD_IP`. Use `sudo` for
   administration; it does not ask for a password.

Password login is disabled. Configure your key before flashing; without it,
neither SSH nor the local console accepts a login. Do not boot with two copies
of the same image attached, because their disk identifiers match.

## First boot and updates

SSH host keys and machine identity are generated on the device. Pacman
initialization waits for NTP synchronization; SSH remains available while it
waits. On the Pi:

```sh
systemctl --failed
findmnt --mountpoint /boot
test -f /var/lib/rpi4/initialized && sudo pacman -Syu
```

If the marker is absent, inspect `journalctl -u rpi4-firstboot -u systemd-timesyncd`.
Package hooks update the kernel and initramfs on the boot partition. If a hook
fails, fix the cause and rerun `sudo rpi4-update-boot` before rebooting.
If `/etc/rpi4-image.conf` is missing, restore it from that card's backup or build
record, preserving its root PARTUUID, boot filesystem UUID, image flavour and
original boot mode; then run the updater and retry the package update.
After changing initramfs configuration, run `sudo rpi4-update-boot`; it rebuilds
the selected initramfs itself. Updates to `kbd`, `fbset` and `terminus-font` also
refresh the selected boot files through the package hooks. The boot set the Pi
is running stays available as a [fallback](#kernel-fallback). Recovery from power
loss during a FAT update is not guaranteed.

The build records `RPI4_BOOT_MODE=native` in `/etc/rpi4-image.conf`; package hooks
retain that boot path. Existing UEFI installations, including older ones without
this setting, retain UEFI. Changing the build default does not convert old cards.
Choose the boot path when building; editing that setting is not a migration procedure.

### Kernel fallback

The Raspberry Pi firmware cannot detect a failed boot, and the mainline kernel
cannot request a one-time trial boot from it. The updater therefore keeps a
second complete boot set. Whenever the boot files change, the set the Pi is
running becomes the fallback, selected by `/boot/native-fallback.txt`, if it is
intact and its kernel modules are present; otherwise the earlier fallback stays.
Pacman hooks keep that kernel's modules after its package is replaced; the
updater removes them once no boot set needs them. On a new card, both selectors
point to the same set until the first kernel or initramfs change.

If a new kernel does not boot:

1. Power off and put the card into any computer. Its boot partition is FAT.
2. In `config.txt` on that partition, replace `include native-current.txt`
   with `include native-fallback.txt`.
3. Boot the Pi. It starts the previous kernel with its modules.

This was checked on the maintained installation on 21 October 2026. It used
the migration steps below, then a full upgrade from 7.2.9 to 7.2.11. Selecting
the fallback booted 7.2.9 with its kept modules and working Wi-Fi; restoring the
line booted 7.2.11 again. A kernel that actually fails to boot was not tested.

While the fallback is selected, package updates still refresh
`native-current.txt` but leave the fallback unchanged. To try the current kernel
again, for example after a fixed package arrives, restore
`include native-current.txt` in `/boot/config.txt` and reboot; repeat the steps
above if it fails again. Installing the fallback's kernel version again, for
example as a downgrade with `pacman -U`, conflicts with its kept modules;
staying on the fallback until a fixed package arrives avoids that.

To add the fallback to a card built earlier, copy
`usr/local/lib/rpi4/update-native-boot.py`,
`usr/local/lib/rpi4/keep-running-modules` and the two
`etc/pacman.d/hooks/10-rpi4-*-running-modules.hook` files from `overlay/` to
the same paths, owned by root. Then copy `/boot/native-current.txt` to
`/boot/native-fallback.txt`, run `sudo rpi4-update-boot` and reboot. The copied
selector keeps the old set as the fallback until the first update after that
reboot.

## HDMI console

VC4 selects a mode from the connected display; the tested UHD monitor
used 3840 × 2160 automatically. Console fonts adapt to the framebuffer,
preserving at least 80 columns and 30 rows where possible. Keep `FONT` unset in
`/etc/vconsole.conf` for automatic sizing; an explicit font takes precedence.
At 720p, bold Terminus provides 11 × 22 pixel characters (116 columns and
32 rows). At 1080p and 4K, `latarcyrheb-sun32` is used at normal and doubled
size respectively, providing 120 columns and 33 rows on the tested 4K display.

Headless startup has not been timed. Font setup can wait about two seconds
per invocation for a framebuffer to become available.

The cursor remains visible but does not blink. To restore blinking, remove
`/etc/tmpfiles.d/rpi4-cursor.conf` and write `1` to
`/sys/class/graphics/fbcon/cursor_blink`.

## Wi-Fi

The image installs the Wi-Fi configuration that passed the
[48-hour test](#48-hour-stability-test):

- Raspberry Pi's own firmware for the on-board CYW43455, **7.45.265** with its
  CLM and board NVRAM files. They come from
  [RPi-Distro/firmware-nonfree](https://github.com/RPi-Distro/firmware-nonfree),
  pinned by commit and SHA256 in `configs/sources.env`, and are installed in
  `/usr/lib/firmware/updates/brcm/`, which the kernel searches before the Ports
  firmware. Their Cypress licence is in `/usr/share/licenses/rpi4-wifi-firmware/`
  and their provenance in `/usr/share/rpi4/wifi-firmware.json`.
- `brcmfmac` options `roamoff=1 feature_disable=0x282000`, as in Raspberry Pi
  OS, in `/etc/modprobe.d/rpi4-wifi.conf`.
- Power saving off for this chip, set by a udev rule with `iw`.

In short controlled runs on the tested Pi, the Ports firmware **7.45.286** lost
0.6–2.4 % of gateway pings at idle and 6–10 % under CPU and memory load, both
with the earlier `feature_disable=0x200000` alone and with these options. The
Raspberry Pi firmware lost none of 3000; switching back to the Ports firmware
brought the loss back.

Wi-Fi starts soft-blocked, and the image includes `wireless-regdb` but no
supplicant. To use Wi-Fi, install and configure a supplicant such as
`wpa_supplicant` or `iwd` over Ethernet, then run `sudo rfkill unblock wifi`.
Set the country where the Pi operates, for example `WIRELESS_REGDOM="CZ"` in
`/etc/conf.d/wireless-regdom` or `country=CZ` in the supplicant's configuration.

The 48-hour test also pinged the gateway over Wi-Fi once per minute. The stock
firmware of the test router, an ASUS RT-AX53U, disconnected a station after
about 8 minutes without traffic, which happens when Ethernet carries all
traffic. After the router moved to OpenWrt, which instead polls idle stations
about every 5 minutes, the Pi stayed connected without any traffic for over an
hour. The image therefore includes no keepalive.

## Power and PowerTOP

The native setup keeps all four cores available and uses `schedutil` with the
full 600–1800 MHz range on the tested board. It starts with Wi-Fi and Bluetooth
soft-blocked and uses wired Ethernet; see [Wi-Fi](#wi-fi) to enable wireless.

PowerTOP is included for diagnostics:

```sh
sudo powertop
sudo powertop --html=/tmp/powertop.html --time=15
sudo powertop --auto-tune-dump
```

The local launcher gives PowerTOP a read-only view of the CPU sysfs subtree in
a temporary mount namespace. This prevents its measurement routine from
changing shared CPU frequency limits, including if the tool is interrupted.
Other tunables remain writable; this is not a general read-only mode. The tested
2.16.1 report completed with CPU bounds unchanged at 600–1800 MHz.

No automatic tuning service or blanket `--auto-tune` is enabled. Suggested
changes need individual checks because they can affect disk writeback,
networking, USB resume or performance. The SEJIN keyboard `0510:0032` is kept
awake by a device-specific udev rule following unreliable resume on the M1S.
Other USB devices retain driver defaults. Repeated keyboard disconnects with
pauses and moves between USB 2 and USB 3 ports worked; the rule kept this keyboard
awake after reconnection. Unplugging HDMI, waiting and reconnecting restored the
4K console at 120 columns and 33 rows without a reboot. These results apply to
the tested keyboard and display.

Some CPU-idle statistics are unavailable on this Pi. PowerTOP helps identify
activity; the wall wattmeter remains the reference for whole-device consumption.
See the [PowerTOP documentation](https://github.com/fenrus75/powertop#readme).
