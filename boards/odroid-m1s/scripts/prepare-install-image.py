#!/usr/bin/env python3
"""Create an independently identified SD/eMMC image without writing block devices.

The source image and its adjacent JSON manifest must be a verified project
build. Output contains new GPT identifiers and ext4 UUIDs, with matching fstab,
image configuration and extlinux entries. Configure the SSH key on the output
with the repository's configure-image.py before deployment.
"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import stat
import subprocess
import uuid


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def digest_region(path, offset, size):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        stream.seek(offset)
        remaining = size
        while remaining:
            chunk = stream.read(min(4 * 1024 * 1024, remaining))
            require(bool(chunk), f'Truncated image region: {path}')
            checksum.update(chunk)
            remaining -= len(chunk)
    return checksum.hexdigest()


def check_geometry(metadata, builder):
    # make_disk_table() uses fixed starts and default filesystem sizes. Accept
    # only that supported layout, even if another source has a valid GPT.
    expected = {
        'image_bytes': builder.IMAGE_BYTES,
        'boot_start_bytes': builder.BOOT_START,
        'boot_size_bytes': builder.BOOT_BYTES,
        'root_start_bytes': builder.ROOT_START,
        'root_size_bytes': builder.ROOT_BYTES,
        'uboot_start_bytes': builder.UBOOT_OFFSET,
    }
    for key, value in expected.items():
        require(metadata[key] == value, f'Unsupported source geometry: {key}')


def check_gpt(stream, metadata, verify):
    stream.seek(0)
    mbr = stream.read(512)
    require(mbr[510:512] == b'\x55\xaa' and mbr[450:451] == b'\xee',
            'Missing protective MBR')
    entries, parts, backup_lba = verify.gpt_header(stream, 1, metadata['image_bytes'])
    backup, backup_parts, primary_lba = verify.gpt_header(
        stream, backup_lba, metadata['image_bytes'])
    require(entries == backup and parts == backup_parts and primary_lba == 1
            and len(parts) == 2, 'Invalid or inconsistent GPT')
    require(backup_lba == metadata['image_bytes'] // 512 - 1,
            'Backup GPT is not at the end of the image')
    for lba in (1, backup_lba):
        stream.seek(lba * 512 + 56)
        require(str(uuid.UUID(bytes_le=stream.read(16))) == metadata['disk_uuid'],
                'GPT disk UUID mismatch')
    for part, prefix in zip(parts, ('boot', 'root')):
        require(part['uuid'] == metadata[f'{prefix}_partuuid']
                and part['start'] * 512 == metadata[f'{prefix}_start_bytes']
                and part['sectors'] * 512 == metadata[f'{prefix}_size_bytes']
                and part['flags'] == (4 if prefix == 'boot' else 0),
                f'GPT {prefix} partition geometry/identity/attributes mismatch')
    return parts


def check_filesystem_uuid(filesystem, expected):
    with filesystem.open('rb') as stream:
        stream.seek(1128)
        require(str(uuid.UUID(bytes=stream.read(16))) == expected,
                f'Filesystem UUID mismatch: {filesystem}')


def check_configuration(fstab, configuration, extlinux, metadata):
    entries = [line.split('#', 1)[0].split() for line in fstab.splitlines()]
    for mountpoint, source in (('/', f"UUID={metadata['root_fs_uuid']}"),
                               ('/boot', f"UUID={metadata['boot_fs_uuid']}"),
                               ('/mnt/nvme', metadata['data_mount']['source'])):
        matches = [entry for entry in entries if len(entry) > 1 and entry[1] == mountpoint]
        require(len(matches) == 1 and len(matches[0]) == 6
                and matches[0][0] == source and matches[0][2] == 'ext4',
                f'Unexpected fstab entry for {mountpoint}')
        if mountpoint == '/mnt/nvme':
            options = set(matches[0][3].split(','))
            require(options == set(metadata['data_mount']['options'].split(','))
                    and {'nofail', 'x-systemd.automount', 'x-systemd.device-timeout=5s'} <= options
                    and not {'x-systemd.makefs', 'x-systemd.growfs'} & options,
                    'Unsafe or inconsistent optional data mount options')
    assignments = [line.strip().split('=', 1) for line in configuration.splitlines()
                   if line.strip() and not line.lstrip().startswith('#')]
    for key, value in (('ROOT_PARTUUID', metadata['root_partuuid']),
                       ('BOOT_FS_UUID', metadata['boot_fs_uuid']),
                       ('IMAGE_FLAVOUR', 'sd-emmc')):
        matches = [entry for entry in assignments if entry[0] == key]
        require(matches == [[key, value]], f'Unexpected image configuration: {key}')
    append_lines = [line.split()[1:] for line in extlinux.splitlines()
                    if line.split() and line.split()[0].upper() == 'APPEND']
    require(bool(append_lines), 'No extlinux APPEND entries')
    for arguments in append_lines:
        roots = [argument for argument in arguments if argument.startswith('root=')]
        require(roots == [f"root=PARTUUID={metadata['root_partuuid']}"],
                'Unexpected extlinux root device')


def replace_file(filesystem, destination, contents, temporary):
    temporary.write_text(contents)
    temporary.chmod(0o600)
    # debugfs has its own command parser. Fixed destination names and a
    # whitespace-free working directory keep paths unambiguous.
    run('debugfs', '-w', '-R', f'rm {destination}', filesystem)
    run('debugfs', '-w', '-R', f'write {temporary} {destination}', filesystem)
    for field, value in (('mode', '0100644'), ('uid', '0'), ('gid', '0')):
        run('debugfs', '-w', '-R', f'set_inode_field {destination} {field} {value}', filesystem)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--data-uuid', type=uuid.UUID)
    args = parser.parse_args()
    source = args.source.resolve()
    output_dir = args.output_dir.resolve()
    if not stat.S_ISREG(source.stat().st_mode):
        raise SystemExit('Source must be a regular image file, never a block device')
    if output_dir.exists():
        raise SystemExit('Output directory must not already exist')
    if any(c.isspace() or c in '\"\\' for c in str(output_dir)):
        raise SystemExit('Use an output path without whitespace, quotes or backslashes')
    for command in ('sfdisk', 'debugfs', 'tune2fs', 'e2fsck'):
        if not shutil.which(command):
            raise SystemExit(f'Missing command: {command}')

    verify = load_module('m1s_verify', 'verify-images.py')
    builder = load_module('m1s_builder', 'make-images.py')
    old = json.loads(source.with_suffix('.json').read_text())
    if old['flavour'] != 'sd-emmc' or source.stat().st_size != old['image_bytes']:
        raise SystemExit('Source must match an SD/eMMC image manifest')
    check_geometry(old, builder)
    source_sha = digest(source)
    if source_sha != old['image_sha256']:
        raise SystemExit('Source image checksum mismatch')
    output_dir.mkdir(mode=0o700, parents=True)
    boot = output_dir / 'boot.ext4'
    root = output_dir / 'root.ext4'
    with source.open('rb') as stream:
        parts = check_gpt(stream, old, verify)
        verify.extract_partition(stream, parts[0], boot)
        verify.extract_partition(stream, parts[1], root)

    fstab = verify.read_file(root, '/etc/fstab').decode()
    configuration = verify.read_file(root, '/etc/odroid-m1s-image.conf').decode()
    extlinux = verify.read_file(boot, '/extlinux/extlinux.conf').decode()
    check_configuration(fstab, configuration, extlinux, old)
    require(extlinux == old['extlinux'], 'Source extlinux differs from its manifest')
    new = dict(old)
    identity_keys = ('disk_uuid', 'boot_partuuid', 'root_partuuid', 'boot_fs_uuid', 'root_fs_uuid')
    for key in identity_keys:
        new[key] = str(uuid.uuid4())
    require(len({new[key] for key in identity_keys}) == len(identity_keys)
            and not {new[key] for key in identity_keys} & {old[key] for key in identity_keys},
            'New identifiers must be distinct from each other and the source')
    for filesystem, key in ((boot, 'boot_fs_uuid'), (root, 'root_fs_uuid')):
        check_filesystem_uuid(filesystem, old[key])
        run('tune2fs', '-U', new[key], filesystem)

    for key in ('root_fs_uuid', 'boot_fs_uuid'):
        fstab = fstab.replace(old[key], new[key])
    for key in ('root_partuuid', 'boot_fs_uuid'):
        configuration = configuration.replace(old[key], new[key])
    extlinux = extlinux.replace(old['root_partuuid'], new['root_partuuid'])
    if args.data_uuid:
        if 'LABEL=M1S_DATA /mnt/nvme ext4 ' not in fstab:
            raise SystemExit('Expected optional data mount is absent')
        fstab = fstab.replace('LABEL=M1S_DATA /mnt/nvme ext4 ', f'UUID={args.data_uuid} /mnt/nvme ext4 ')
        new['data_mount'] = dict(new['data_mount'], source=f'UUID={args.data_uuid}')
    check_configuration(fstab, configuration, extlinux, new)
    require(not any(old[key] in text for key in identity_keys
                    for text in (fstab, configuration, extlinux)),
            'Old system identifiers remain in the new boot configuration')
    patch = output_dir / 'file-content'
    replace_file(root, '/etc/fstab', fstab, patch)
    replace_file(root, '/etc/odroid-m1s-image.conf', configuration, patch)
    replace_file(boot, '/extlinux/extlinux.conf', extlinux, patch)
    patch.unlink()
    for filesystem in (boot, root):
        run('e2fsck', '-fn', filesystem)
    for filesystem, key in ((boot, 'boot_fs_uuid'), (root, 'root_fs_uuid')):
        check_filesystem_uuid(filesystem, new[key])
    for filesystem, path, expected in ((root, '/etc/fstab', fstab),
                                       (root, '/etc/odroid-m1s-image.conf', configuration),
                                       (boot, '/extlinux/extlinux.conf', extlinux)):
        require(verify.read_file(filesystem, path).decode() == expected,
                f'Edited file readback mismatch: {path}')

    image = output_dir / 'odroid-m1s-emmc-install.img'
    builder.make_disk_table(image, old['image_bytes'], new)
    # Preserve the verified loader and padding through the first partition.
    loader_size = old['boot_start_bytes'] - old['uboot_start_bytes']
    with source.open('rb') as incoming, image.open('r+b') as outgoing:
        incoming.seek(old['uboot_start_bytes'])
        outgoing.seek(old['uboot_start_bytes'])
        loader = incoming.read(loader_size)
        require(len(loader) == loader_size, 'Truncated source loader region')
        outgoing.write(loader)
    builder.sparse_copy(boot, image, old['boot_start_bytes'])
    builder.sparse_copy(root, image, old['root_start_bytes'])
    require(image.stat().st_size == new['image_bytes'], 'Assembled image size mismatch')
    with image.open('rb') as stream:
        check_gpt(stream, new, verify)
    require(digest_region(image, new['uboot_start_bytes'], loader_size)
            == digest_region(source, old['uboot_start_bytes'], loader_size),
            'Assembled loader region differs from the source')
    for filesystem, prefix in ((boot, 'boot'), (root, 'root')):
        require(filesystem.stat().st_size == new[f'{prefix}_size_bytes'],
                f'Unexpected {prefix} filesystem size')
        require(digest_region(image, new[f'{prefix}_start_bytes'], new[f'{prefix}_size_bytes'])
                == digest(filesystem), f'Assembled {prefix} filesystem differs from checked file')
    new.update(source_image_sha256=source_sha, image_sha256=digest(image), extlinux=extlinux)
    image.with_suffix('.json').write_text(json.dumps(new, indent=2) + '\n')
    print(f'Prepared {image}; original source image unchanged.')


if __name__ == '__main__':
    main()
