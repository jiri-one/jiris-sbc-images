# Jiri's SBC Images

Headless **Arch Linux** image builders with Ethernet, DHCP and SSH.
Raspberry Pi 4 and ODROID-M1S use community **Arch Linux Ports AArch64**
packages. JaguarBoard uses official **Arch Linux x86_64** packages.
Each board has its own boot setup.

The images and scripts are provided as is, without warranty. Use them at your
own risk.

| Board | Boot | Status |
| --- | --- | --- |
| [Raspberry Pi 4](boards/raspberry-pi-4/README.md) | SD/USB, EDK2 UEFI + systemd-boot | Offline and generic QEMU checks passed; physical Pi boot is untested. |
| [ODROID-M1S](boards/odroid-m1s/README.md) | SD/eMMC, U-Boot + extlinux | Offline and generic QEMU checks passed. Earlier SD/eMMC builds booted on hardware; this rebuild still needs hardware testing. |
| [JaguarBoard](boards/jaguarboard/README.md) | USB → eMMC, systemd-boot with 32/64-bit EFI | eMMC upgrade tested on v1.3 hardware; new USB image awaits hardware testing. Intermittent freezes are being investigated. |

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
Builds produce `.img`, `.img.zst` and `SHA256SUMS` under the board's `artifacts/`.
Package versions and input hashes are recorded there too; rolling repositories
mean later builds can contain different packages.

## Images and login

Compressed images and checksums are attached to [releases](https://github.com/jiri-one/jiris-sbc-images/releases).
Follow the board's installation instructions. Before flashing, use
[`scripts/configure-image.py`](scripts/configure-image.py) on Linux to add your
SSH public key to a new copy of the image. It needs Python 3.11+, OpenSSH,
util-linux, e2fsprogs and coreutils; no root access is needed.

Log in as `rpi` on Raspberry Pi 4, `odroid` on ODROID-M1S, or `jaguar` on JaguarBoard.
Password login is disabled; the configured key grants access to passwordless
`sudo`. Without configuring a key first, neither SSH nor the local console
accepts a login.

## License and credits

Project scripts and documentation use [MIT](LICENSE). Packages and firmware
retain their own licenses. Thanks to [Arch Linux Ports](https://ports.archlinux.page/aarch64/),
the [pftf firmware maintainers](https://github.com/pftf/RPi4),
[EndeavourOS ARM](https://github.com/endeavouros-arm/image-build-script), and
[k1nerda's M1S guide](https://forum.odroid.com/viewtopic.php?f=214&t=50877).

Created with AI assistance and human review by Jiří.
