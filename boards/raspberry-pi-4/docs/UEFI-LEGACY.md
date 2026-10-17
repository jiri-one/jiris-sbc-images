# Deprecated Raspberry Pi 4 UEFI variant

Native boot is the [default and maintained configuration](../README.md).
UEFI is retained only as a historical alternative and for comparing existing
cards. There are no planned UEFI builds, updates, tests or further tuning unless
a concrete need arises. The existing release may be the last; this source may
become obsolete or be removed. This personal project does not maintain boot
variants that its owner no longer uses.

UEFI supplies EDK2 and systemd-boot, but our board-specific Arch Linux Ports
image does not need them. Both paths use the mainline Ports kernel. On the
tested setup, native boot reached the console noticeably sooner and drew less
power at settled idle. The main README records the measurements and their limits.

On the manually configured test board, UEFI fills the screen and is readable.
That result also uses per-board UEFI display settings; these firmware variables
are not embedded in newly built images. **Menu navigation still responds slowly**;
this was not resolved. Text in UEFI and systemd-boot could
also be larger for viewing from a distance. The Linux console font is readable,
but the long loader interval and black screen before Linux output remain reasons
to prefer native boot.

## Historical build and verification

These commands explicitly select the legacy path; omitting the flag now builds
or verifies native boot:

```sh
scripts/build-image.sh --boot-mode uefi
PATH="$PWD/build/tools:$PATH" python3 scripts/verify-images.py --boot-mode uefi
python3 scripts/verify-qemu.py --boot-mode uefi --container arch-rpi4-tools
```

Run from `boards/raspberry-pi-4`. Output remains
`artifacts/rpi4-archlinux-ports-sd-usb.img` and `.img.zst`, separate from native's
`artifacts/native/`. Existing UEFI cards retain their boot path during package
updates, including old cards without `RPI4_BOOT_MODE`. This compatibility does
not imply future validation against new kernel or firmware releases.

The firmware combines pinned [pftf UEFI v1.53](https://github.com/pftf/RPi4/releases/tag/v1.53)
with the independently verified, matched Raspberry Pi runtime pair described
in the main README. `RPI_EFI.fd`, DTBs and overlays retain their pftf pins.
EDK2 defaults to ACPI and a **3 GiB RAM limit**. On a 4/8 GB board, disable
`Limit RAM to 3GB` in the Raspberry Pi configuration after an initial successful
boot, then retest. See the [firmware documentation](https://github.com/pftf/RPi4/blob/v1.53/Readme.md).

## Device Tree mode

For the tested Device Tree path, select **ACPI + Devicetree** in UEFI's Raspberry
Pi configuration. Then add this to `/etc/rpi4-image.conf` and run
`sudo rpi4-update-boot` before rebooting:

```sh
RPI4_FIRMWARE_TABLES=devicetree
```

The updater preserves this choice, uses `acpi=off`, and keeps the UEFI
framebuffer through `simpledrm` by blocking `vc4` and `v3d`. It uses EDK2's
fixed-up Device Tree without a loader DTB override and does not change UEFI
settings. Keep the USB-C data-disable overlay described in the main README.

On the Pi 4B 8 GB with Ports 7.2.9, Device Tree enabled `schedutil` at
600–1800 MHz. All four cores, full RAM, SD, Ethernet, USB-A and HDMI worked;
memory checks and SD/network transfers passed with 4.5 GiB allocated. USB
storage with the RAM limit disabled remains untested. The ACPI path does not
expose Linux CPU frequency scaling. To return to it, set
`RPI4_FIRMWARE_TABLES=acpi` and rerun the updater. An absent setting means ACPI;
empty or unknown values are rejected before changing boot files.

Existing release images predate this option and the lean initramfs; rebuild or
update the updater and initramfs configuration before opting in. The boot entry
also skips the kernel's `psci_checker` development self-test, which could offline
cores and fail to bring them back with this ACPI firmware. Ordinary PSCI support
stays enabled. Verify `/sys/devices/system/cpu/online` reports `0-3`.

## Display settings

The UEFI initramfs loads `simpledrm` to retain the firmware framebuffer after
systemd-boot. The image allows native firmware modes up to 4K:

```ini
max_framebuffer_width=4096
max_framebuffer_height=2160
```

Leave `framebuffer_width`, `framebuffer_height` and HDMI timings unset for monitor
detection. A capped framebuffer previously distorted the tested UHD display to
1920 × 1280. Raising these limits restored 3840 × 2160; the adaptive Linux font
then provided 120 columns and 33 rows. These firmware limits do not provide
independent UEFI font scaling or fix its slow response. See the
[Raspberry Pi framebuffer settings](https://www.raspberrypi.com/documentation/computers/legacy_config_txt.html#max_framebuffer_height-max_framebuffer_width).
