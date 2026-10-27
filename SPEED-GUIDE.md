# RAID Forge speed, wear & hardware guide

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

## SSD wear, wear levelling and TRIM

**RAID doesn't interfere with wear levelling.** Each SSD spreads writes across its own flash internally. Linux
RAID sits above that and just sends normal reads and writes, so every drive still levels itself.

**RAID 1's quirk: both mirrors wear together.** A mirror writes the same data to both drives, so a matching pair
reaches its write limit at about the same time. Ways around it:
- Use two different models, or drives of different ages.
- Watch **SSD wear** on the Drive Health tab. RAID Forge warns at 60% and 80%, and flags mirror pairs
  that are wearing in step.
- Replace one drive early (My Arrays > Replace a drive) so the pair gets out of step.

**How much wear to expect.** A typical 1 TB NVMe drive is rated for about 600 TB written. A Pi running its system
plus light NAS use writes roughly 5–20 GB a day, which works out to decades. Wear only becomes a real factor with
heavy constant writing, like security cameras or databases.

**The RAID level makes a difference:**
| Level | Writes per drive |
|---|---|
| RAID 1 / 10 | Same as a single drive (plus one full write during the first sync) |
| RAID 5 / 6 | More: small writes also update parity. Fine for big files, less ideal for a system drive |
| Monthly `mdadm` check | Reads only, so no wear |

**TRIM** tells an SSD which blocks are free, so it doesn't keep moving deleted data around. RAID Forge's
Drive Health tab has the controls:
- **Run automatically: Weekly (recommended) / Daily / Off.** Weekly is plenty for most Pis, because TRIM only
  clears blocks freed since the last run. Daily is harmless on NVMe and helps if you delete lots of data often
  (camera footage, big downloads). Daily adds a systemd override at
  `/etc/systemd/system/fstrim.timer.d/raidforge.conf`.
- **TRIM now** runs it straight away on every mounted SSD.
- It checks each drive: NVMe on the Pi 5's PCIe connector supports TRIM, many **USB enclosures don't pass it
  through** (it says so), RAID 0/1/10 pass it to the drives, and **RAID 5/6 blocks it by default** for safety.
- RAID Forge uses scheduled TRIM, not the `discard` mount option, which can slow down every delete.

**Fewer writes:**
- Newly built arrays and moved systems mount with `noatime`, so reading a file doesn't cause a write.
- **Move system to RAID > Low-wear** moves swap from a file on the drive (dphys-swapfile) into compressed RAM
  (zram). Systems that already use zram (as Trixie does) are left as they are.
- After **Move system to RAID**, the SD card is only written when a kernel update lands in the boot partition.
  SD cards have the weakest wear levelling of all, so this is the biggest win for reliability.

## Telling identical drives apart

Most USB enclosures and docks wire their activity light to the enclosure's own chip, so software can't switch it on
or off directly. (`ledctl`-style "locate" lights only exist on server backplanes.) RAID Forge makes the light blink
**by reading from that one drive** in a rhythm: about 1 second of steady reads, then 1 second idle. The reads skip the
cache and only read, so it's safe on a running array.
- On a busy array (resync, big copies) every light flickers, so RAID Forge switches to 2-second bursts. Look for the
  light that goes **fully dark** for 2 seconds at a time.
- A failed drive often doesn't answer at all, so it can't blink. **Find the failed drive** blinks the healthy ones
  instead, and the dead one is the light that stays dark.
- Every drive also shows its **port** (USB 3 blue / USB 2 black, bus and port, hub port, dock slot; SATA port; NVMe
  PCIe address) and **serial number**. Write the serial on a sticker on each enclosure when you set the array up.
- Some enclosures only have a power light. Then the port and serial are how you find it.

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
