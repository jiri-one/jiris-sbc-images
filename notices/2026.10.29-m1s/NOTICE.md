# NOTICE — ODROID-M1S image 2026.10.29

Image: `odroid-m1s-archlinux-ports-sd-emmc.img.zst`  
SHA256: `067ddfb833c01d92dd878cb57bd2d5b47175bacc6904a9fd11499a54ed7a5008`  
Image build source: `4efac2a37e4e4b885695edc9928a6fb26945d275` in
https://github.com/jiri-one/jiris-sbc-images

## Project files

This project's original scripts and documentation are licensed under MIT, not
GPL. The repository LICENSE is reproduced below. Third-party software keeps
its own licenses; MIT does not relicense the image as a whole.

## Packages and license texts

The image is an aggregation of independently licensed packages. License texts
are installed under `/usr/share/licenses` (including common licenses); consult
those texts and upstream notices for the applicable terms. Firmware also has
its own license terms.

`packages.tsv`, distributed beside this NOTICE and stored in this directory,
lists all 139 packages with exact versions and license fields extracted from
the released image's `/var/lib/pacman/local/*/desc`. License labels are copied
verbatim, including older generic labels such as `GPL`; they are metadata,
not a replacement for the license texts. U-Boot and its bundled firmware are
built separately and described below.

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

## Exact GPLv2 component versions

### Linux 7.2.11 (packages linux and linux-dtbs 7.2.11.arch1-1)

- Upstream: https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.2.11.tar.xz
- Arch patch: https://github.com/archlinux/linux/releases/download/v7.2.11-arch1/linux-v7.2.11-arch1.patch.zst
- Exact Arch Linux Ports recipe, including `config.aarch64`:
  https://gitlab.archlinux.org/bschnei/linux/-/tree/df552a59d8498405ec283f2b18c6d3ef360372c5

The recipe PKGBUILD SHA256 matches the package's .BUILDINFO:
`201508be956ada3c8dc501139d651f327c456536cf7fbb41c8724b44c327e45a`.
Upstream archive SHA256:
`fd0fc479f983a8c05313f13e9c36330fe6651cf725396748c40d5cd93fa1e6a4`.
Arch patch SHA256:
`741d473c4db335d97d9e1d9b8be9337090522550804fe6454ba336d21e580fd6`.

### U-Boot v2026.01

- Tag: https://source.denx.de/u-boot/u-boot/-/tree/v2026.01
- Exact upstream commit: `127a42c7257a6ffbbd1575ed1cbaa8f5408a44b3`.
- Project build recipe and modification:
  https://github.com/jiri-one/jiris-sbc-images/blob/4efac2a37e4e4b885695edc9928a6fb26945d275/boards/odroid-m1s/scripts/build-uboot.sh

The build starts with `odroid-m1s-rk3566_defconfig`, enables
`CONFIG_CMD_SYSBOOT=y`, then runs `olddefconfig`. There are no project U-Boot
source-code patches. U-Boot license notices are installed under
`/usr/share/licenses/uboot-m1s/u-boot`.

Rockchip DDR v1.23 and BL31 v1.45 binaries are separate firmware, from rkbin
commit `ef49d0c2852b16ce0d72d7ee4849638cb5de27b7`:
https://github.com/rockchip-linux/rkbin/tree/ef49d0c2852b16ce0d72d7ee4849638cb5de27b7
Its license is installed under `/usr/share/licenses/uboot-m1s/rkbin`.

### BusyBox 1.36.1 (mkinitcpio-busybox 1.36.1-1)

- Upstream: https://busybox.net/downloads/busybox-1.36.1.tar.bz2
- Package recipe history (configuration and packaging changes):
  https://gitlab.archlinux.org/archlinux/packaging/packages/mkinitcpio-busybox
- AArch64 forks are linked above. The image package's PKGBUILD SHA256 is
  `75d898d09ca75c5cbd2b55da4e1e10b18475ef0aae790be55e8eeb9b5c3dbcb3`.

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
