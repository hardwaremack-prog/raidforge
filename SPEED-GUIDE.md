# RAID Forge speed & hardware guide

What speeds to expect from NVMe, SATA and USB drives on a Raspberry Pi 5, and which RAID level makes sense.

## The Pi 5's PCIe lane is the limit

The Pi 5 has **one PCIe lane**. Every NVMe or SATA HAT shares it.

| PCIe setting | How to set it (`/boot/firmware/config.txt`) | Top speed |
|---|---|---|
| Gen 2 (official default) | `dtparam=pciex1` | ~450 MB/s |
| Gen 3 (works on most hardware, not certified) | `dtparam=pciex1` + `dtparam=pciex1_gen=3` | ~800–900 MB/s |

RAID Forge's **Enable PCIe for HAT…** button sets either one and keeps a backup of `config.txt`.

## Where the "800+ MB/s" numbers come from

People who get 800–900 MB/s on a Pi 5 are running **one NVMe drive wired straight to the Pi** (no switch chip) **at Gen 3**.

**Dual and quad NVMe HATs** put a **PCIe switch chip** between the Pi and the drives. Most of those chips are Gen 2 only. Forcing Gen 3 on the Pi doesn't help, because the switch is still the bottleneck.

| Setup | Realistic speed |
|---|---|
| One NVMe on a single-slot HAT (Raspberry Pi M.2 HAT+, Pimoroni NVMe Base, regular Pironman 5) at Gen 3 | ~800–900 MB/s |
| Same, at Gen 2 (default) | ~450 MB/s |
| Dual NVMe behind a Gen 2 switch (e.g. Pironman 5-MAX), any setting, single drive or RAID | ~410–450 MB/s total |
| USB 3 drives (Pi 5 / 500 / 400) | ~300–400 MB/s per port; the Pi 400's two USB 3 ports share one controller |
| SD card (fast A2/V30) | ~85 MB/s read, ~30 MB/s write |

**The trade-off:** for a mirror (RAID 1), the Pi 5 needs two drives, which means a switch chip and ~450 MB/s. For top speed, use one drive at Gen 3 (~850 MB/s), with no mirror, plus a regular backup.

Even at 450 MB/s, NVMe is about 5× faster than an SD card. Its fast response to small reads and writes (roughly 100,000 vs ~3,500 random-read IOPS) is what makes the system *feel* snappy.

## Does RAID 1 double read speed?

**Only partly.** Linux RAID 1 (`mdadm`) keeps a full copy on each drive and can send *different* read requests to *different* drives:

- **One big file** (copying a movie): about the speed of **one** drive. A single stream mostly stays on one mirror.
- **Several things reading at once** (a few programs, or a few people using a NAS): reads spread across both drives, so the total can approach **2×**.
- **Writes:** a little slower than one drive, because every write goes to both.

If the PCIe lane is already full (dual NVMe behind a Gen 2 switch), none of this shows up. RAID 1 runs at about one drive's speed. Choose RAID 1 because **a dead drive doesn't take your system or files with it**, not for speed.

RAID 0 on a shared lane barely helps either. One review measured ~425 MB/s for RAID 0 vs ~410 MB/s for a single drive, while RAID 0 doubles the risk.

## SunFounder Pironman 5-MAX (dual NVMe case)

- 2 × M.2 M-key slots (2230–2280) behind an **ASM1182e Gen 2 PCIe switch**, plus a micro SD slot
- Sold as supporting "RAID 0/1 NAS". That's Linux software RAID, which RAID Forge sets up for you
- **In RAID Forge:** choose **Pironman 5-MAX** (or *Dual NVMe HAT*) in the HAT list, **Enable PCIe → Gen 2**, tick both NVMe drives, pick **RAID 1**
- RAID 5/6/10 need 3–4 drives, so with two NVMe drives it's RAID 0 or 1. Don't mix in USB drives to get there
- **Move system to RAID** works well here: the SD card starts the Pi, and the system runs from the NVMe mirror
- Using one slot for a Hailo AI card leaves one slot for storage, so no RAID
- Use the official **27 W (5 V / 5 A)** power supply, or the Pi can reboot under load
- If no drives show up, re-seat the PCIe ribbon cable straight, with both latches open

## Recommended setups

| Goal | Setup |
|---|---|
| Safe NAS / system on RAID (Pi 5) | 2 × NVMe in a dual-NVMe case/HAT, **RAID 1**, PCIe Gen 2 |
| Most speed (Pi 5) | 1 × NVMe on a single-slot HAT at **Gen 3**, plus backups |
| Lots of space with protection (Pi 5) | Radxa Penta SATA HAT, 3–5 drives, **RAID 5** (or **RAID 6** with 4+) |
| Pi 500 / Pi 400 | USB 3 drives in powered enclosures or a multi-bay dock, **RAID 1** |

> RAID protects against a dead drive. It is **not a backup**. Deleting a file, ransomware or a bad update hits both mirrors at once. Keep a copy of anything important on another device.

## Sources
- [SunFounder Pironman 5-MAX specifications](https://docs.sunfounder.com/projects/pironman5/en/latest/pironman5_max/intro_pironman5_max.html)
- [Raspberry.tips: Pironman 5 MAX review (RAID 1 setup and speed tests)](https://raspberry.tips/en/raspberrypi-tutorials/pironman-5-max-review)
- [CNX Software: Pironman 5-Max announcement](https://www.cnx-software.com/2025/05/07/pironman-5-max-tower-pc-case-for-the-raspberry-pi-5-gets-dual-m-2-pcie-sockets-programmable-rgb-leds-new-black-design/)
