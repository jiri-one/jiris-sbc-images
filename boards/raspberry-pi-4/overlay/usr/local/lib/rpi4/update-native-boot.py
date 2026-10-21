#!/usr/bin/env python3
"""Stage a matching native Pi kernel/DTB/initramfs and select it last.

Arch Linux Ports may package an EFI zboot executable. Extract its ARM64 Image
before giving it to the Raspberry Pi firmware; keep the packaged executable
at /boot/vmlinuz-linux for mkinitcpio's normal preset.

The firmware cannot fall back on its own, so the newest boot set known to
start stays selectable through native-fallback.txt, together with its modules.
"""

from pathlib import Path
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib


BOOT = Path('/boot')
CONFIG = Path('/etc/rpi4-image.conf')
MODULES = Path('/usr/lib/modules')
SHARE = Path('/usr/share/rpi4')
LOCK = Path('/run/lock/rpi4-update-boot.lock')
CMDLINE = Path('/proc/cmdline')
OSRELEASE = Path('/proc/sys/kernel/osrelease')
PAYLOAD = ('kernel8.img', 'bcm2711-rpi-4-b.dtb', 'initramfs-linux.img', 'kernel-version')
FILES = PAYLOAD + ('cmdline.txt',)
CURRENT, FALLBACK = 'native-current.txt', 'native-fallback.txt'
BOOT_SET_OPTION = 'rpi4.boot_set='
# Written by keep-running-modules into module trees that no package owns.
KEPT_MODULES = '.rpi4-kept'
MAX_IMAGE_BYTES = 512 * 1024 * 1024


def command(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def output(args):
    return subprocess.check_output(args, text=True).strip()


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def decompress_gzip(data, limit):
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            result = stream.read(limit + 1)
    except (OSError, EOFError, zlib.error) as error:
        raise ValueError('Invalid gzip kernel payload') from error
    if len(result) > limit:
        raise ValueError('Kernel expanded size exceeds the allowed limit')
    return result


def decompress_zstd(data, limit):
    # A temporary input avoids pipe deadlock while output is read with a hard
    # bound. Neither the compressed stream nor decoder diagnostics are exposed
    # through named files, and both temporary files are closed on every exit.
    with tempfile.TemporaryFile() as encoded, tempfile.TemporaryFile() as errors:
        encoded.write(data)
        encoded.seek(0)
        with subprocess.Popen(['zstd', '-d', '-q', '-c'], stdin=encoded,
                              stdout=subprocess.PIPE, stderr=errors) as process:
            result = process.stdout.read(limit + 1)
            if len(result) > limit:
                process.kill()
                process.wait()
                raise ValueError('Kernel expanded size exceeds the allowed limit')
            if process.wait() != 0:
                raise ValueError('Invalid zstd kernel payload')
    return result


def raw_image(data):
    """Decode Image, Image.gz or Linux EFI zboot; pad through image_size.

    The zboot layout is defined by Linux drivers/firmware/efi/libstub/
    zboot-header.S and Makefile.zboot. Zstd's expanded-size trailer follows
    the advertised compressed payload; gzip carries it in its own footer.
    """
    maximum = MAX_IMAGE_BYTES
    if not 1 <= len(data) <= maximum:
        raise ValueError('Kernel input size is outside the supported range')
    wrapped = False
    if data[:2] == b'MZ' and data[4:8] == b'zimg':
        wrapped = True
        if len(data) < 64 or data[:8] != b'MZ\0\0zimg' or data[56:60] != b'\xcd\x23\x82\x81':
            raise ValueError('Invalid Linux EFI zboot header')
        if data[16:24] != b'\0' * 8 or b'\0' not in data[24:56]:
            raise ValueError('Unsupported zboot header fields')
        offset, length = struct.unpack_from('<II', data, 8)
        if offset < 64 or not length or offset + length > len(data):
            raise ValueError('Invalid zboot payload bounds')
        pe = struct.unpack_from('<I', data, 60)[0]
        if pe < 64 or pe + 26 > offset or data[pe:pe + 4] != b'PE\0\0':
            raise ValueError('Invalid zboot PE header location')
        if struct.unpack_from('<H', data, pe + 4)[0] != 0xaa64:
            raise ValueError('zboot PE image is not AArch64')
        compression = data[24:56].split(b'\0', 1)[0]
        payload = data[offset:offset + length]
        if compression in (b'zstd', b'zstd22'):
            if offset + length + 4 > len(data):
                raise ValueError('Missing zboot uncompressed-size trailer')
            expected = struct.unpack_from('<I', data, offset + length)[0]
            if not 64 <= expected <= maximum:
                raise ValueError('Invalid zboot expanded size')
            data = decompress_zstd(payload, expected)
        elif compression == b'gzip':
            if length < 18:
                raise ValueError('Truncated zboot gzip payload')
            expected = struct.unpack_from('<I', payload, length - 4)[0]
            if not 64 <= expected <= maximum:
                raise ValueError('Invalid zboot expanded size')
            data = decompress_gzip(payload, expected)
        else:
            raise ValueError(f'Unsupported zboot compression: {compression!r}')
        if len(data) != expected:
            raise ValueError('zboot expanded size does not match its trailer')
    elif data.startswith(b'\x1f\x8b'):
        data = decompress_gzip(data, maximum)
    if len(data) < 64 or data[56:60] != b'ARM\x64':
        raise ValueError('Kernel is not an uncompressed ARM64 Image')
    offset, size, flags = struct.unpack_from('<QQQ', data, 8)
    if not 64 <= size <= maximum or len(data) > size or flags & 1:
        raise ValueError('Unsupported ARM64 Image size or byte order')
    if wrapped and len(data) != size:
        raise ValueError('Padded zboot payload differs from ARM64 memory size')
    if offset != 0 or (flags >> 1) & 3 != 1:
        raise ValueError('Native boot requires text_offset=0 and 4 KiB pages')
    # Raw Images can omit BSS. Firmware's followkernel initramfs placement must
    # leave that region intact when the kernel clears it during startup.
    return data + b'\0' * (size - len(data))


def validate_dtb(data):
    if len(data) < 40:
        raise ValueError('Truncated DTB')
    magic, size = struct.unpack_from('>II', data)
    if magic != 0xd00dfeed or not 40 <= size <= len(data):
        raise ValueError('Invalid DTB header')
    if b'raspberrypi,4-model-b\0' not in data[:size] or b'spin-table\0' not in data[:size]:
        raise ValueError('Expected a Raspberry Pi 4 spin-table device tree')


def valid_version(version):
    return version not in ('.', '..') and re.fullmatch(r'[0-9A-Za-z._+-]+', version) is not None


def boot_set_id(payload_sums, base_cmdline):
    """Name a boot set without its command line, which carries the name."""
    return hashlib.sha256(f'{payload_sums}cmdline {base_cmdline}\n'.encode()).hexdigest()[:24]


def read_text(path):
    try:
        return path.read_text()
    except OSError:
        return ''


def selected_set(selector):
    match = re.search(r'^kernel=native/([a-f0-9]{24})/kernel8.img$', read_text(selector), re.M)
    return match[1] if match else None


def booted_set():
    names = [word[len(BOOT_SET_OPTION):] for word in read_text(CMDLINE).split()
             if word.startswith(BOOT_SET_OPTION)]
    return names[0] if len(names) == 1 and re.fullmatch(r'[a-f0-9]{24}', names[0]) else None


def set_version(directory):
    try:
        if directory.is_symlink() or not directory.is_dir():
            return None
        sums = {}
        for line in (directory / 'SHA256SUMS').read_text().splitlines():
            match = re.fullmatch(r'([a-f0-9]{64})  ([A-Za-z0-9_.-]+)', line)
            if not match or match[2] in sums:
                return None
            sums[match[2]] = match[1]
        if set(sums) != set(FILES) or any(digest(directory / name) != value
                                          for name, value in sums.items()):
            return None
        version = (directory / 'kernel-version').read_text().strip()
    except OSError:
        return None
    return version if valid_version(version) else None


def modules_present(version):
    # A kept tree is complete only once its restore has finished and left the
    # marker; otherwise the kernel's package must still be installed.
    tree = MODULES / version
    if tree.is_symlink() or not (tree / 'modules.order').is_file():
        return False
    if (tree / KEPT_MODULES).is_file():
        return True
    owner = subprocess.run(['pacman', '-Qqo', '--', str(tree / 'vmlinuz')],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return owner.returncode == 0


def choose_fallback(native, release, version):
    """The running set becomes the fallback whenever the selection moves away
    from it: it is the newest set known to boot. While the running set stays
    selected, the earlier fallback is kept if it is still complete.
    """
    running = read_text(OSRELEASE).strip()
    booted = booted_set()
    if (booted and booted != release and set_version(native / booted) == running
            and modules_present(running)):
        return booted, running
    previous = selected_set(BOOT / FALLBACK)
    if previous and previous != release:
        previous_version = set_version(native / previous)
        if previous_version and modules_present(previous_version):
            return previous, previous_version
    return release, version


def selector_text(heading, release):
    content = (f'# Managed by rpi4-update-boot. {heading}\n'
               f'kernel=native/{release}/kernel8.img\n'
               f'device_tree=native/{release}/bcm2711-rpi-4-b.dtb\n'
               f'initramfs native/{release}/initramfs-linux.img followkernel\n'
               f'cmdline=native/{release}/cmdline.txt\n')
    if any(len(line) > 80 for line in content.splitlines() if not line.startswith('#')):
        raise ValueError('Firmware configuration exceeds 80 characters')
    return content.encode()


def prune_kept_modules(keep):
    # No package owns a kept tree, so pacman never removes it.
    for tree in MODULES.iterdir():
        if (tree.name in keep or tree.is_symlink() or not tree.is_dir() or tree.is_mount()
                or not (tree / KEPT_MODULES).is_file()):
            continue
        owner = subprocess.run(['pacman', '-Qqo', '--', str(tree)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if owner.returncode != 0:
            shutil.rmtree(tree)


def sync(path):
    command(['sync', '-f', str(path)])


def atomic_file(path, data):
    descriptor, name = tempfile.mkstemp(prefix=f'.{path.name}-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        sync(temporary)
        temporary.replace(path)
        sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_config():
    values = {}
    for line in CONFIG.read_text().splitlines():
        fields = shlex.split(line, comments=True)
        if not fields:
            continue
        if len(fields) != 1 or '=' not in fields[0]:
            raise ValueError('Image configuration must contain KEY=value assignments')
        key, value = fields[0].split('=', 1)
        values[key] = value
    if values.get('RPI4_BOOT_MODE', 'uefi') != 'native':
        raise ValueError('Native backend requires RPI4_BOOT_MODE=native')
    if values.get('IMAGE_FLAVOUR') != 'sd-usb':
        raise ValueError('Unsupported image flavour')
    if not re.fullmatch(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}',
                        values.get('ROOT_PARTUUID', '')):
        raise ValueError('Invalid root PARTUUID')
    return values


def validate_boot_inputs():
    manifest = json.loads((SHARE / 'runtime-firmware.json').read_text())
    hashes = manifest.get('sha256', {})
    if set(hashes) != {'start4.elf', 'fixup4.dat'}:
        raise ValueError('Runtime firmware manifest must identify the matched start4/fixup4 pair')
    for name, expected in hashes.items():
        if not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected):
            raise ValueError(f'Invalid runtime firmware digest: {name}')
        if digest(BOOT / name) != expected:
            raise ValueError(f'Pinned runtime firmware mismatch: {name}')
    lines = [line.split('#', 1)[0].strip() for line in (BOOT / 'config.txt').read_text().splitlines()]
    required = ('arm_64bit=1', 'enable_gic=1', 'kernel_address=0x200000',
                'start_file=start4.elf', 'fixup_file=fixup4.dat')
    includes = [line for line in lines if line in (f'include {CURRENT}', f'include {FALLBACK}')]
    if any(lines.count(line) != 1 for line in required) or len(includes) != 1:
        raise ValueError('config.txt does not select the expected native boot configuration')
    if any(re.match(r'(kernel|device_tree|cmdline)\s*=|initramfs\s', line) for line in lines):
        raise ValueError('Native boot paths must come from native-current.txt or native-fallback.txt')
    overlay = BOOT / 'overlays/rpi4-usb-c-data-disable.dtbo'
    if not overlay.is_file() or not overlay.stat().st_size:
        raise ValueError('Missing USB-C data-controller overlay')
    return includes[0].split()[1]


def update():
    command(['/usr/local/sbin/rpi4-check-boot'])
    config = read_config()
    pattern = re.escape(str(MODULES)) + r'/[^/]+/vmlinuz'
    kernels = [Path(line) for line in output(['pacman', '-Qql', 'linux']).splitlines()
               if re.fullmatch(pattern, line)]
    if len(kernels) != 1:
        raise ValueError('Expected exactly one package-owned linux kernel')
    kernel = kernels[0]
    version = kernel.parent.name
    if not valid_version(version):
        raise ValueError('Invalid kernel version')
    dtb = kernel.parent / 'dtb/broadcom/bcm2711-rpi-4-b.dtb'
    if output(['pacman', '-Qqo', '--', str(dtb)]) != 'linux-dtbs':
        raise ValueError('Matching DTB is not owned by linux-dtbs')
    packages = dict(line.split() for line in output(['pacman', '-Q', 'linux', 'linux-dtbs']).splitlines())
    if set(packages) != {'linux', 'linux-dtbs'} or packages['linux'] != packages['linux-dtbs']:
        raise ValueError('linux and linux-dtbs package versions differ')
    packaged_kernel = kernel.read_bytes()
    image = raw_image(packaged_kernel)
    dtb_data = dtb.read_bytes()
    validate_dtb(dtb_data)
    included = validate_boot_inputs()
    build = os.environ.get('RPI4_IMAGE_BUILD', '0') == '1'
    if build and (not (BOOT / 'initramfs-linux.img').is_file()
                  or not (BOOT / 'initramfs-linux.img').stat().st_size
                  or (SHARE / 'initramfs-kernel').read_text().strip() != version):
        raise ValueError('The image-builder initramfs does not match the installed kernel')

    native = BOOT / 'native'
    if native.is_symlink():
        raise ValueError('Refusing symlink for native boot directory')
    native.mkdir(exist_ok=True)
    # An interrupted process cannot run its finally block. Under the shared
    # update lock, reclaim only directories created by this backend.
    for path in native.iterdir():
        if (re.fullmatch(r'\.staging-[a-z0-9_]{8}', path.name)
                and path.is_dir() and not path.is_symlink() and not path.is_mount()):
            shutil.rmtree(path)
    staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=native))
    try:
        (staging / 'kernel8.img').write_bytes(command(
            ['gzip', '-n', '-6', '-c'], input=image, capture_output=True).stdout)
        (staging / 'bcm2711-rpi-4-b.dtb').write_bytes(dtb_data)
        if build:
            shutil.copyfile(BOOT / 'initramfs-linux.img', staging / 'initramfs-linux.img')
        else:
            command(['mkinitcpio', '-k', version, '-g', str(staging / 'initramfs-linux.img')])
        if not (staging / 'initramfs-linux.img').stat().st_size:
            raise ValueError('Empty initramfs')
        (staging / 'kernel-version').write_text(version + '\n')
        # Native VC4 owns the console. Prevent a later simple-framebuffer
        # modalias from creating an additional simpledrm device after switch-root.
        cmdline = (f"root=PARTUUID={config['ROOT_PARTUUID']} rw rootwait rootfstype=ext4 "
                   'console=tty1 systemd.show_status=yes cma=256M@0-1G '
                   'rfkill.default_state=0 module_blacklist=simpledrm')
        payload_sums = ''.join(f'{digest(staging / name)}  {name}\n' for name in PAYLOAD)
        release = boot_set_id(payload_sums, cmdline)
        # The running system finds its own boot set through this option.
        (staging / 'cmdline.txt').write_text(f'{cmdline} {BOOT_SET_OPTION}{release}\n')
        checksums = ''.join(f'{digest(staging / name)}  {name}\n' for name in FILES)
        (staging / 'SHA256SUMS').write_text(checksums)
        destination = native / release
        for path in staging.iterdir():
            path.chmod(0o644)
        sync(staging)
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise ValueError('Invalid existing boot set')
            if ((destination / 'SHA256SUMS').read_text() != checksums
                    or any(digest(destination / name) != digest(staging / name) for name in FILES)):
                raise ValueError('Existing boot set is damaged')
            shutil.rmtree(staging)
        else:
            staging.rename(destination)
        sync(native)
        if included == FALLBACK:
            # The user chose to boot the fallback; it must keep pointing there.
            fallback = selected_set(BOOT / FALLBACK)
            fallback_version = set_version(native / fallback) if fallback else None
            if not fallback_version or not modules_present(fallback_version):
                raise ValueError('The fallback boot set selected in config.txt is missing, damaged '
                                 'or without its kernel modules')
        else:
            fallback, fallback_version = choose_fallback(native, release, version)
        atomic_file(BOOT / 'vmlinuz-linux', packaged_kernel)
        atomic_file(BOOT / FALLBACK, selector_text(f'Fallback kernel {fallback_version}', fallback))
        # Commit last. Every selected payload is complete and synced. Atomic
        # renames reduce partial-write exposure but cannot make FAT power-safe.
        atomic_file(BOOT / CURRENT, selector_text(f'Kernel {version}', release))
        for directory in native.iterdir():
            if (directory.is_dir() and not directory.is_symlink() and not directory.is_mount()
                    and re.fullmatch(r'[a-f0-9]{24}', directory.name)
                    and directory.name not in (release, fallback)):
                shutil.rmtree(directory)
        sync(native)
        prune_kept_modules({version, fallback_version, read_text(OSRELEASE).strip()})
        print(f'Native boot files ready: {version}, boot set {release}')
        if fallback == release:
            print('No older boot set is kept as a fallback yet.')
        else:
            print(f'Fallback: {fallback_version}, boot set {fallback}')
        if included == FALLBACK:
            print(f'config.txt boots the fallback. Restore "include {CURRENT}" to start {version}.')
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run as root.')
    if not CONFIG.exists():
        if os.environ.get('RPI4_IMAGE_BUILD') != '1':
            raise ValueError(f'Image configuration is missing: {CONFIG}; refusing boot update.')
        print('Image configuration is not ready; no boot files changed.')
        return
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        update()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(f'Native boot update failed: {error}')
