# RAID Forge

Point-and-click software RAID for the Raspberry Pi 5, Pi 500 and Pi 400. Boot Pi OS from the SD card,
put your drives on a HAT, and build a RAID 0, 1, 5, 6 or 10 array without touching the terminal.

![RAID Forge: RAID 1 on a Pironman 5-MAX](docs/setup.png)

| | |
|:---:|:---:|
| ![My Arrays](docs/my-arrays.png) | ![Move system to RAID](docs/move-system-to-raid.png) |
| **My Arrays**: live rebuild progress, drive swaps, scrubs | **Move system to RAID**: the SD card only starts the Pi |

## Install
```bash
git clone https://github.com/hardwaremack-prog/raidforge
cd raidforge
bash install.sh
```
Or download the zip, unzip it and run `bash install.sh` inside the `raidforge` folder.

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

**Move system to RAID** (on My Arrays) — runs the whole system from the array; the SD card only starts the Pi
- Checks first: system is on the SD card, array is healthy and big enough, startup files (initramfs) can find RAID
- Copies everything from the SD card onto the array with live progress (rsync), erasing the array first if needed
- Points the new copy's `fstab` at the array, adds the RAID/USB/NVMe/SATA drivers to the initramfs and rebuilds it
- Backs up `cmdline.txt` as `cmdline.txt.raidforge-sd`, then sets `root=` to the array and asks to reboot
- The SD card keeps its boot partition and its old copy of the system as a fallback
- **Boot from SD again…** puts the original `cmdline.txt` back. If the Pi ever won't start, put the SD card in another
  computer and copy `cmdline.txt.raidforge-sd` over `cmdline.txt` (see `RAID-FORGE-README.txt` on the card)
- Use RAID 1 (or 5/6/10) for this: on RAID 0 one dead drive takes the whole system with it
- **Low-wear** option: moves swap from a file on the drives into compressed RAM (zram)

**③ Drive Health**: SMART pass/fail, temperature, power-on hours, bad sectors, and **SSD wear** (NVMe and SATA SSDs),
with warnings at 60% / 80% and a flag for mirror pairs wearing in step. Its **TRIM** panel runs TRIM weekly
(recommended), daily or not at all, has a **TRIM now** button, and shows which drives, USB enclosures and arrays support it.

![Drive Health: SSD wear and TRIM](docs/drive-health.png)

## Hardware notes
| Setup | Notes |
|---|---|
| Pi 5 + Radxa Penta SATA HAT | Up to 5 SATA drives. Needs PCIe enabled and the HAT's own 12 V supply. |
| Pi 5 + dual/quad NVMe HAT (PCIe switch) | Use PCIe Gen 2. 2 drives → RAID 0/1; 4 drives → any level. |
| Pi 500 | No PCIe connector is exposed, so use USB 3 drives, a USB multi-bay dock or USB HAT. Use powered enclosures. |
| Pi 400 | Same as the Pi 500: no PCIe, so use the two blue USB 3 ports with powered drives or a multi-bay dock. Slower (USB 3 is shared), but RAID 1 / 5 work well. |
| Pi 500+ | Has one internal M.2 slot — add USB drives to reach RAID levels that need more. |
| Pi 5 + SunFounder Pironman 5-MAX | 2 NVMe slots behind a Gen 2 switch: RAID 1, PCIe Gen 2, ~410–450 MB/s. |

Minimum drives: RAID 0 / 1 → 2, RAID 5 → 3, RAID 6 / 10 → 4 (RAID 10 even numbers only).

**📈 Speeds, Gen 2 vs Gen 3, "does RAID 1 double reads?", SSD wear and TRIM, and recommended setups: see [SPEED-GUIDE.md](SPEED-GUIDE.md).**

RAID is not a backup — keep a copy of anything important somewhere else.
