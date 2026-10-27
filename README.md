# RAID Forge

Point-and-click software RAID for the Raspberry Pi 5, Pi 500 and Pi 400. Boot Pi OS from the SD card,
put your drives on a HAT, and build a RAID 0, 1, 5, 6 or 10 array without touching the terminal.

## Install
```bash
git clone https://github.com/hardwaremack-prog/raidforge
cd raidforge
bash install.sh
```
Then open **Menu → System Tools → RAID Forge**. Use **RAID Forge (Demo)** first to try it with fake drives.

## What it does
**① Set up RAID**
- Checks the board, confirms you booted from the SD card, and that `mdadm` is installed (one-click install if not)
- Enables the Pi 5 PCIe connector for SATA/NVMe HATs (Gen 2 or Gen 3), backs up `config.txt`, offers a reboot
- Lists every drive with type (SATA/NVMe/USB), size and model; the SD boot card and in-use drives are locked out
- Pick RAID 0/1/5/6/10, tick drives, optionally tick hot spares — shows usable space, how many drives you can lose, and efficiency live
- Name it, choose a mount folder, ext4 or XFS, chunk size, and whether it auto-mounts at boot
- Type `ERASE` to confirm → wipes, creates the array, formats (stripe-aligned), mounts, writes `mdadm.conf` + `fstab` (`nofail`, so the Pi still boots if the HAT is unplugged), updates initramfs

**② My Arrays** — live status every 3 s with sync/rebuild progress, details, scrub, speed test, replace a failed drive, add a hot spare, stop, re-assemble, delete (also cleans up fstab/mdadm.conf)

**③ Drive Health** — SMART pass/fail, temperature, power-on hours, reallocated/pending sectors, NVMe wear

## Hardware notes
| Setup | Notes |
|---|---|
| Pi 5 + Radxa Penta SATA HAT | Up to 5 SATA drives. Needs PCIe enabled and the HAT's own 12 V supply. |
| Pi 5 + dual/quad NVMe HAT (PCIe switch) | Use PCIe Gen 2. 2 drives → RAID 0/1; 4 drives → any level. |
| Pi 500 | No PCIe connector is exposed, so use USB 3 drives, a USB multi-bay dock or USB HAT. Use powered enclosures. |
| Pi 400 | Same as the Pi 500: no PCIe, so use the two blue USB 3 ports with powered drives or a multi-bay dock. Slower (USB 3 is shared), but RAID 1 / 5 work well. |
| Pi 500+ | Has one internal M.2 slot — add USB drives to reach RAID levels that need more. |

Minimum drives: RAID 0 / 1 → 2, RAID 5 → 3, RAID 6 / 10 → 4 (RAID 10 even numbers only).

RAID is not a backup — keep a copy of anything important somewhere else.
