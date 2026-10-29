# Jiri's SBC Images

I like Arch Linux for its simplicity and use it almost everywhere
([#arch-everywhere](https://github.com/topics/arch-everywhere)
[#iusearchbtw](https://github.com/topics/iusearchbtw)). Getting it
running on single-board computers has often taken more work, but AI has made
that easier. I started building Arch images with AI assistance for the SBCs
I own, and I'm sharing them here for anyone who might find them useful.
This is a personal side project: maintenance follows the boards and configurations
I use, rather than a commitment to support every alternative.

Headless **Arch Linux** image builders with Ethernet, DHCP and SSH.
Raspberry Pi 4 and ODROID-M1S use community **Arch Linux Ports AArch64**
packages. JaguarBoard uses official **Arch Linux x86_64** packages.
Each board has its own boot setup.

The images and scripts are provided as is, without warranty. Use them at your
own risk.

| Board | Boot | Status |
| --- | --- | --- |
| [Raspberry Pi 4](boards/raspberry-pi-4/README.md) | Native Raspberry Pi boot, default | [48-hour CPU/RAM test passed; Wi-Fi probes had no loss](boards/raspberry-pi-4/README.md#48-hour-stability-test) on the maintained installation with manually configured Wi-Fi. SD boot, CPU scaling, Ethernet, SSH, 4K HDMI, keyboard/display reconnection, a kernel upgrade and the [kernel fallback](boards/raspberry-pi-4/README.md#kernel-fallback) checked. USB-root boot untested. [UEFI is deprecated](boards/raspberry-pi-4/docs/UEFI-LEGACY.md), with no planned maintenance. |
| [ODROID-M1S](boards/odroid-m1s/README.md) | SD/eMMC, U-Boot + extlinux | Four generic QEMU scenarios passed. Kernel, U-Boot and boot updater match the board in production; this image has not been flashed to hardware. |
| [JaguarBoard](boards/jaguarboard/README.md) | USB → eMMC, systemd-boot with 32/64-bit EFI | [72-hour stability test passed](boards/jaguarboard/README.md#72-hour-stability-test) on v1.3 hardware running from eMMC with a Samsung USB charger. New USB image awaits hardware testing. |

## Download

Newest released image for each board:

| Board | Image | Checksums | Release |
| --- | --- | --- | --- |
| Raspberry Pi 4 | [rpi4-archlinux-ports-sd-usb.img.zst](https://github.com/jiri-one/jiris-sbc-images/releases/download/2026.10.14/rpi4-archlinux-ports-sd-usb.img.zst) | [SHA256SUMS](https://github.com/jiri-one/jiris-sbc-images/releases/download/2026.10.14/SHA256SUMS) | [2026.10.14](https://github.com/jiri-one/jiris-sbc-images/releases/tag/2026.10.14), older UEFI build; see [its README](boards/raspberry-pi-4/README.md) |
| ODROID-M1S | [odroid-m1s-archlinux-ports-sd-emmc.img.zst](https://github.com/jiri-one/jiris-sbc-images/releases/download/2026.10.29/odroid-m1s-archlinux-ports-sd-emmc.img.zst) | [SHA256SUMS](https://github.com/jiri-one/jiris-sbc-images/releases/download/2026.10.29/SHA256SUMS) | [2026.10.29](https://github.com/jiri-one/jiris-sbc-images/releases/tag/2026.10.29) |
| JaguarBoard | [arch-jaguar.img.zst](https://github.com/jiri-one/jiris-sbc-images/releases/download/2026.10.15.2/arch-jaguar.img.zst) | [SHA256SUMS](https://github.com/jiri-one/jiris-sbc-images/releases/download/2026.10.15.2/SHA256SUMS) | [2026.10.15.2](https://github.com/jiri-one/jiris-sbc-images/releases/tag/2026.10.15.2) |

License and source notices: [M1S 2026.10.29](notices/2026.10.29-m1s/NOTICE.md),
[RPi4/M1S 2026.10.14](notices/2026.10.14/NOTICE.md), and
[JaguarBoard 2026.10.15–15.2](notices/jaguarboard-2026.10.15/NOTICE.md).

Older builds are under [releases](https://github.com/jiri-one/jiris-sbc-images/releases).

## Build requirements

Use an x86-64 Linux host with Python 3.11+, rootless Podman, Git, curl, GnuPG,
bsdtar, zstd, coreutils, util-linux and e2fsprogs. For ARM builds, register
`qemu-aarch64-static` with binfmt_misc enabled and the `F` flag.
Pi builds also need dosfstools. JaguarBoard builds run natively in a rootless
Arch container; see its README for the smaller host tool list.
Allow about 30 GiB of free space per board.

Run builds as a normal user. Each board's README has the commands. Named
builder containers belong to one checkout; an existing container mounted to
another directory is deliberately rejected.

`scripts/` contains build and image-verification tools, `configs/` holds package
sources and board settings, and `overlay/` contains files installed in the image.
Builds produce `.img`, `.img.zst` and `SHA256SUMS` under the board's `artifacts/`
(`artifacts/native/` for Raspberry Pi 4). Package versions and input hashes are recorded there too; rolling repositories
mean later builds can contain different packages.

## Images and login

Download an image and its `SHA256SUMS` from [Download](#download).
Follow the board's installation instructions. Before flashing, use
[`scripts/configure-image.py`](scripts/configure-image.py) on Linux to add your
SSH public key to a new copy of the image. It needs Python 3.11+, OpenSSH,
util-linux, e2fsprogs and coreutils; no root access is needed.

Log in as `rpi` on Raspberry Pi 4, `odroid` on ODROID-M1S, or `jaguar` on JaguarBoard.
Password login is disabled; the configured key grants access to passwordless
`sudo`. Without configuring a key first, neither SSH nor the local console
accepts a login.

## License and credits

Project scripts and documentation are licensed under [GPL-3.0-or-later](LICENSE).
Releases up to and including 2026.10.29 were published under MIT. Packages and
firmware retain their own licenses. Thanks to [Arch Linux Ports](https://ports.archlinux.page/aarch64/),
the [pftf firmware maintainers](https://github.com/pftf/RPi4),
[EndeavourOS ARM](https://github.com/endeavouros-arm/image-build-script), and
[k1nerda's M1S guide](https://forum.odroid.com/viewtopic.php?f=214&t=50877).
