# NOTICE — Arch Linux Ports images 2026.10.14

Applies to the Raspberry Pi 4 and ODROID-M1S images released as `2026.10.14`.
This is a retrospective license and source-location notice.

## Project files

This project's original scripts and documentation are licensed under MIT, not
GPL. The repository LICENSE is reproduced below. Third-party software keeps
its own licenses; MIT does not relicense the image as a whole.

## Packages and license texts

The image is an aggregation of independently licensed packages. License texts
are installed under `/usr/share/licenses` (including common licenses); consult
those texts and upstream notices for the applicable terms. Firmware also has
its own license terms.

A complete package inventory with exact versions and license fields has not
been reconstructed: most local build data is no longer available. The images
record package versions under `/usr/share/rpi4/` or `/usr/share/odroid-m1s/`;
their pacman databases retain installed versions and license metadata. The
2026.10.29 M1S inventory does not describe these older images.

## Arch Linux Ports package sources

- Port and package-source guidance: https://ports.archlinux.page/aarch64/
- Build infrastructure used by the M1S package repository:
  https://github.com/solskogen/archlinux-aarch64-builder
- Official packaging recipes and their upstream source URLs:
  https://gitlab.archlinux.org/archlinux/packaging/packages
- AArch64 recipe forks: https://gitlab.archlinux.org/bschnei and
  https://gitlab.archlinux.org/solskogen
- M1S binary package repository: https://arch-linux-repo.drzee.net/arch/

PKGBUILD `source` entries identify upstream sources and patches. Select the
recipe matching the installed package version; a rolling branch may have moved.

## Boot firmware and GPL components

For the exact Linux and mkinitcpio-busybox versions, consult the respective
image's pacman database, then the matching recipe's upstream source entries:
https://www.kernel.org/ and https://busybox.net/downloads/ .

M1S uses U-Boot v2026.01 (commit
`127a42c7257a6ffbbd1575ed1cbaa8f5408a44b3`):
https://source.denx.de/u-boot/u-boot/-/tree/v2026.01
The project configuration and firmware pins are public:
https://github.com/jiri-one/jiris-sbc-images/blob/2026.10.14/boards/odroid-m1s/scripts/build-uboot.sh

The Raspberry Pi 4 image uses the older UEFI firmware build. Its third-party
firmware license notices are included in the image; the project firmware
configuration is recorded at:
https://github.com/jiri-one/jiris-sbc-images/tree/2026.10.14/boards/raspberry-pi-4

## MIT license for original project files

MIT License

Copyright (c) 2026 Contributors to this image-building project

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

This license covers this project's original scripts and documentation only.
EDK2, Raspberry Pi firmware, U-Boot, Rockchip firmware and Linux distribution
packages keep their own licenses.
