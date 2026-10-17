# Shared build paths. Source after changing to the board project directory.
# This default is only for new builds, not installed-card compatibility.
RPI4_BOOT_MODE=${RPI4_BOOT_MODE:-native}
while (($#)); do
    case $1 in
        --boot-mode)
            [[ $# -ge 2 ]] || { echo '--boot-mode requires native or uefi.' >&2; return 2; }
            RPI4_BOOT_MODE=$2
            shift 2
            ;;
        *) printf 'Unexpected build option: %s\n' "$1" >&2; return 2 ;;
    esac
done
case $RPI4_BOOT_MODE in
    uefi) RPI4_BUILD_DIR=build; RPI4_ARTIFACTS_DIR=artifacts ;;
    native) RPI4_BUILD_DIR=build/native; RPI4_ARTIFACTS_DIR=artifacts/native ;;
    *) printf 'Invalid RPI4_BOOT_MODE: %s (expected uefi or native).\n' "$RPI4_BOOT_MODE" >&2; return 1 ;;
esac
RPI4_ROOTFS_DIR=$RPI4_BUILD_DIR/rootfs
export RPI4_BOOT_MODE RPI4_BUILD_DIR RPI4_ARTIFACTS_DIR RPI4_ROOTFS_DIR
