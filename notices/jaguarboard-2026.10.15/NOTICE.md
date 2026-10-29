# NOTICE — JaguarBoard images 2026.10.15, 2026.10.15.1 and 2026.10.15.2

These x86_64 images contain official Arch Linux packages. This is a
retrospective license and source-location notice for the three releases.

## Project files

This project's original scripts and documentation are licensed under MIT, not
GPL. The repository LICENSE is reproduced below. Third-party software keeps
its own licenses; MIT does not relicense the image as a whole.

## Packages and license texts

The image is an aggregation of independently licensed packages. License texts
are installed under `/usr/share/licenses` (including common licenses); consult
those texts and upstream notices for the applicable terms. Firmware also has
its own license terms.

Each release's existing `packages.txt` asset records its package versions.
A complete inventory annotated with licenses has not been reconstructed; most
local build data is no longer available. Consult the image's pacman database
for installed versions and license fields, and `/usr/share/licenses` for terms.
No inventory from a newer image is substituted for an older release.

## Official Arch Linux package sources

- Packaging recipes, history and upstream source/patch URLs:
  https://gitlab.archlinux.org/archlinux/packaging/packages
- Historical Arch Linux packages and repository snapshots:
  https://archive.archlinux.org/ and https://archive.archlinux.org/packages/

Use the version in that release's `packages.txt` to select matching packaging
history. PKGBUILD `source` entries point to upstream sources and patches.
For Linux and BusyBox, source archives are at https://www.kernel.org/ and
https://busybox.net/downloads/ respectively; obtain the exact versions from
the release's package inventory. systemd-boot comes from the systemd package:
https://gitlab.archlinux.org/archlinux/packaging/packages/systemd .
These images do not use Arch Linux Ports or the M1S U-Boot build.

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
