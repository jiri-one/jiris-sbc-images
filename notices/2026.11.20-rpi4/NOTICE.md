# NOTICE — Raspberry Pi 4 image 2026.11.20

Image: `rpi4-archlinux-ports-native-sd-usb.img.zst`  
SHA256: `6030dd76df3c08197af3250624e36082ef37e99829fc58be92038f42e8a1627f`  
Decompressed image SHA256: `f61d426d2cf3e65e1c1ad3033f146f75ce5b6e0bfc6a6caafc12acbf30247751`  
Image build source: `0bba62ee53a6c1cd7bd8c4603b300fe2f9bff91e` in
https://github.com/jiri-one/jiris-sbc-images

## Project files

This project's original scripts and documentation are licensed under
GPL-3.0-or-later. The license text is the repository
[LICENSE](https://github.com/jiri-one/jiris-sbc-images/blob/2026.11.20/LICENSE)
and is also installed in the image as
`/usr/share/licenses/spdx/GPL-3.0-or-later.txt`. The project scripts installed
in the image, such as `/usr/local/sbin/rpi4-update-boot`, are shell and Python
source. The boot partition's `overlays/rpi4-usb-c-data-disable.dtbo` is
compiled from `boards/raspberry-pi-4/configs/overlays/rpi4-usb-c-data-disable-overlay.dts`.
The build scripts are in the repository at the commit above. Third-party
software keeps its own licenses.

## Packages and license texts

The image is an aggregation of independently licensed packages. License texts
are installed under `/usr/share/licenses` (including common licenses); consult
those texts and upstream notices for the applicable terms. Firmware also has
its own license terms.

`packages.tsv`, distributed beside this NOTICE and stored in this directory,
lists all 161 packages with exact versions and license fields extracted from
the released image's `/var/lib/pacman/local/*/desc`. License labels are copied
verbatim; they are metadata, not a replacement for the license texts. The
Raspberry Pi boot and Wi-Fi firmware are installed outside pacman and are
described below. `/usr/share/licenses/rpi4-uefi-firmware/` belongs to the
deprecated UEFI build; this native image contains none of that firmware.

## Arch Linux Ports package sources

- Port and package-source guidance: https://ports.archlinux.page/aarch64/
- Binary package repository used by this image: https://arch.bens.haus/repos/
- Official packaging recipes and their upstream source URLs:
  https://gitlab.archlinux.org/archlinux/packaging/packages
- AArch64 recipe forks: https://gitlab.archlinux.org/bschnei

PKGBUILD `source` entries identify upstream sources and patches. Select the
recipe matching the installed package version; a rolling branch may have moved.

## Exact GPLv2 component versions

### Linux 7.3.4 (packages linux and linux-dtbs 7.3.4.arch1-3)

- Upstream: https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.3.4.tar.xz
- Arch patch: https://github.com/archlinux/linux/releases/download/v7.3.4-arch1/linux-v7.3.4-arch1.patch.zst
- Arch Linux Ports recipe, including `config.aarch64`:
  https://gitlab.archlinux.org/bschnei/linux/-/tree/5d00060357aadef0a2bb21d27ec6a7053315bfbf

That recipe sets `pkgrel=2`; the package was built with `pkgrel=3`. With only
that line changed, the PKGBUILD SHA256 matches the package's .BUILDINFO:
`4e75c767357db604779ea70be31f8123fb63e984e02b637ed006363ba1d15d62`.
`config.aarch64` SHA256:
`e2182ed73234ccf4393a1f677b59772c7ae99ba9ffb64a64c35036c997c4a46f`.
Upstream archive SHA256:
`b11b6df8295e8e991ff040b71aaa4994a8b55caa23b6b092907f195b5840fd1f`.
Arch patch SHA256:
`0bd7c69abfa671c377989c4795bcb3662a25904d4ec8dbda73204b5689405c2f`.

The boot partition's `kernel8.img` is this kernel, extracted from the
package's EFI zboot image by the project's boot updater. Its Device Tree comes
from `linux-dtbs`.

### BusyBox 1.36.1 (mkinitcpio-busybox 1.36.1-1)

- Upstream: https://busybox.net/downloads/busybox-1.36.1.tar.bz2
- Arch Linux Ports recipe, including the BusyBox `config`:
  https://gitlab.archlinux.org/bschnei/mkinitcpio-busybox/-/tree/f09f1d52bfed732e0cd52cd84a7fa8c8670e0e6a

Its PKGBUILD SHA256 matches the package's .BUILDINFO:
`1035d9aabe68fa993c9b290590465cc2748b29a4228b4a0a78a7a7c9fd4055dc`.
`config` SHA256:
`f1a67ecda5b73a50e49953efaaa0be582e4b707cf5a6270519e28ee022098518`.

BusyBox is also part of the initramfs on the boot partition.

### Recipe copies

Neither recipe commit is on a branch of its repository, so copies of the files
are stored beside this NOTICE in the project repository,
https://github.com/jiri-one/jiris-sbc-images/tree/2026.11.20/notices/2026.11.20-rpi4/recipes :
`recipes/linux/PKGBUILD` (as built, with `pkgrel=3`),
`recipes/linux/config.aarch64`, `recipes/mkinitcpio-busybox/PKGBUILD` and
`recipes/mkinitcpio-busybox/config`. Their SHA256 values are the ones above.

## Raspberry Pi firmware

These are binary firmware files without published source code.

- VideoCore runtime firmware `start4.elf` and `fixup4.dat` from Raspberry Pi
  firmware release 1.20260915, commit `6f0881cba8bea8ec24956a5718714730e9936ec5`:
  https://github.com/raspberrypi/firmware/tree/6f0881cba8bea8ec24956a5718714730e9936ec5/boot
  Their license is `LICENCE.broadcom` on the boot partition. SHA256:
  `560ead37044d849325755d3684ac1f74654b3ce6450604a42df6dd832cda9ede` (`start4.elf`),
  `d181d5055daebac22513d6904fa9ce21d1e99b852cc391af095ade6f0b45c400` (`fixup4.dat`).
- Wi-Fi firmware 7.45.265 for the CYW43455, with its CLM and board NVRAM
  files, from RPi-Distro/firmware-nonfree commit
  `3bab0f823f5b53150b76aab77093adef6655b920`:
  https://github.com/RPi-Distro/firmware-nonfree/tree/3bab0f823f5b53150b76aab77093adef6655b920/debian/added-firmware
  It is installed in `/usr/lib/firmware/updates/brcm/`. Its license is
  `/usr/share/licenses/rpi4-wifi-firmware/LICENCE.cypress`; file hashes and
  origin are in `/usr/share/rpi4/wifi-firmware.json`.
