# Boot script for the small SD carrier. The NVMe image's boot partition is 1.
echo "Arch Linux Ports - ODROID M1S NVMe loader"
if nvme scan; then
    if sysboot nvme 0:1 any ${pxefile_addr_r} /extlinux/extlinux.conf; then
        echo "NVMe kernel returned unexpectedly"
    else
        echo "Cannot boot NVMe partition 1: check the NVMe image and extlinux.conf"
    fi
else
    echo "NVMe controller scan failed"
fi
echo "NVMe boot failed. Use the full SD image or the Hardkernel recovery card."
sleep 10
reset
