# NOTICE — JaguarBoard image 2026.11.27

- Image `arch-jaguar.img.zst`
- SHA256 `fab02b3131d4b994c07141b150747382634eaba5253a1d079cd5d5699b90593e`
- Decompressed image SHA256 `68b7bbf45f1e925c619691b027e6d7608e4a8025fea2bde1e565023ec87c50c0`
- Image build source [commit `bad3c7180b018c4b243be45934da25cfd779ddcc`](https://github.com/jiri-one/jiris-sbc-images/tree/bad3c7180b018c4b243be45934da25cfd779ddcc/boards/jaguarboard)

## Project files

This project's original scripts and documentation are licensed under
GPL-3.0-or-later. See the repository [LICENSE](https://github.com/jiri-one/jiris-sbc-images/blob/2026.11.27/LICENSE).
The license text is also in the image at
`/usr/share/licenses/spdx/GPL-3.0-or-later.txt`. The installed project scripts
in `/usr/local/sbin` are shell source. The build scripts, configuration and
overlay are available at the build commit linked above. Third-party software
keeps its own licenses.

## Arch Linux packages

The image contains 163 official Arch Linux x86_64 packages. `packages.tsv`
lists their exact versions and the license fields from the image's
`/var/lib/pacman/local` database. The image includes common and package-specific
license texts under `/usr/share/licenses`.

- [Arch Linux packaging recipes](https://gitlab.archlinux.org/archlinux/packaging/packages)
  include the source locations, patches and build configuration. Select
  the packaging tag matching the installed version.
- The [Arch Linux Archive](https://archive.archlinux.org/) keeps historical
  packages and repository snapshots.

## Boot components

### Linux 7.3.6 (`linux` 7.3.6.arch1-1)

- [Linux 7.3.6 source](https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.3.6.tar.xz)
- [Arch Linux arch1 patch](https://github.com/archlinux/linux/releases/download/v7.3.6-arch1/linux-v7.3.6-arch1.patch.zst)
- [Arch recipe and `config.x86_64`](https://gitlab.archlinux.org/archlinux/packaging/packages/linux/-/tree/7.3.6.arch1-1)

The image uses this package's kernel on the EFI partition and modules in the
root filesystem and initramfs.

### BusyBox 1.36.1 (`mkinitcpio-busybox` 1.36.1-1)

- [BusyBox 1.36.1 source](https://busybox.net/downloads/busybox-1.36.1.tar.bz2)
- [Arch recipe and BusyBox `config`](https://gitlab.archlinux.org/archlinux/packaging/packages/mkinitcpio-busybox/-/tree/1.36.1-1)

The initramfs contains the BusyBox binary from this package. It is generated
by [mkinitcpio 42.2-1](https://gitlab.archlinux.org/archlinux/packaging/packages/mkinitcpio/-/tree/42.2-1)
using the JaguarBoard configuration and hooks at the build commit.

### systemd-boot (`systemd` 262.2-1)

- [systemd v262.2 source](https://github.com/systemd/systemd/tree/v262.2)
- [Arch recipe and patch](https://gitlab.archlinux.org/archlinux/packaging/packages/systemd/-/tree/262.2-1)

The EFI partition contains the package's IA32 and x64 loaders.

## Firmware and CPU microcode

- `linux-firmware-intel`, `linux-firmware-realtek` and `linux-firmware-whence`
  are version 20261124-1. [Upstream linux-firmware 20261124](https://gitlab.com/kernel-firmware/linux-firmware/-/tree/20261124)
  and the [Arch recipe](https://gitlab.archlinux.org/archlinux/packaging/packages/linux-firmware/-/tree/20261124-1)
  identify the distributed firmware. See `/usr/share/licenses/linux-firmware-whence/WHENCE`
  and the `linux-firmware-intel` and `linux-firmware-realtek` license directories
  in the image for the terms of each file.
- `intel-ucode` 20261110-1 supplies the Intel CPU microcode in the root filesystem
  and the initramfs. See the [Intel microcode release](https://github.com/intel/Intel-Linux-Processor-Microcode-Data-Files/tree/microcode-20261110),
  the [Arch recipe](https://gitlab.archlinux.org/archlinux/packaging/packages/intel-ucode/-/tree/20261110-1)
  and `/usr/share/licenses/intel-ucode/LICENSE` in the image.

The Wi-Fi regulatory database comes from
[wireless-regdb 2026.09.03-1](https://gitlab.archlinux.org/archlinux/packaging/packages/wireless-regdb/-/tree/2026.09.03-1).
The image ships only in-tree kernel modules. It does not contain the
out-of-tree `bcm4323x` Wi-Fi driver or its companion firmware.
