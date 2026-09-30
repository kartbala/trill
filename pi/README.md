# Brass Creature, Raspberry Pi route

Touch a pad on the Trill Craft, the Pi reads it, and the Mac plays a note.

**Status, 30 Sep 2026: staged, not deployed. Nothing here has run on a real Pi or a real Craft.** The program logic and the installer's file edits are tested on the Mac (see "Tests"). The I2C and USB paths are untested.

## Files

- `guide.html` -- the step-by-step wiring guide, 16 screens, same format as `../flipper/pinmap.html`. Open it from disk; it needs no internet.
- `play.html` -- the player page for the Mac. Open it in Google Chrome. It makes the sound, shows 30 live bars, and has the buttons the Flipper has (reset, sensitivity, metal size, scale).
- `creature.py` -- the program for the Pi. Python standard library only.
- `install.sh` -- puts `creature.py` on a Pi and sets the Pi up. Has `--dry-run` and `--uninstall`.
- `packing.md` -- what to carry to the library.
- `test_creature.py`, `test_install.sh` -- tests that run on any computer.

## The wiring

The Pi gets a five-hole socket strip soldered into holes 1, 3, 5, 7 and 9: the first five holes of the inner row, counted from the memory-card end.

| Socket hole (from the card end) | Pi pin | Pi signal | Wire | Craft pin (from the left, chip up, pins pointing away) | Craft label |
|---|---|---|---|---|---|
| 1 | 1 | 3.3 V | red | 3 | +V |
| 2 | 3 | GPIO 2, SDA | white | 2 | SDA |
| 3 | 5 | GPIO 3, SCL | yellow | 1 | SCL |
| 4 | 7 | GPIO 4, unused | -- | -- | -- |
| 5 | 9 | ground | black | 4 | GND |

Craft pins 5 (EVT) and 6 (RST) stay empty.

No resistors are needed. The Pi has 1.8 kilohm pull-up resistors on pins 3 and 5 already.

Sources:

- Pin functions: Raspberry Pi's own pinout diagram, https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#gpio (pin 1 "3V3 power", pin 3 "GPIO 2 (SDA)", pin 5 "GPIO 3 (SCL)", pin 9 "Ground", pins 2 and 4 "5V power").
- Where pin 1 is on a Pi Zero W: the `pinout` command run on KB's own Pi Zero W rev 1.1 on 30 Sep 2026. It draws pin 1 at the `sd` end, in the row nearer the chips, and lists `3V3 (1)`, `GPIO2 (3)`, `GPIO3 (5)`, `GND (9)`.
- Pull-ups: the same Raspberry Pi page says "Pins GPIO2 and GPIO3 have fixed pull-up resistors". The Pi Zero W reduced schematic shows them as R23 and R24, 1.8K 1%, to 3V3 (https://datasheets.raspberrypi.com/rpizero/raspberry-pi-zero-w-reduced-schematics.pdf). pinout.xyz/pinout/i2c says the same value.
- Craft pin order: read off KB's photo of his board (SCL, SDA, +V, GND, EVT, RST, starting at the pad-0 edge).
- Trill protocol, address 0x30, DIFF mode: BelaPlatform/Trill-Linux `lib/Trill.cpp`.

## Why a socket strip, and why only five holes

Three ways to connect to a Pi with bare pads were weighed.

- **A full 2x20 header.** 40 joints at 2.54 mm spacing is a lot for a first soldering job with low vision. It also puts live 5 V pins right beside the ones this build uses.
- **Four wires soldered straight to the pads.** Fewest parts, but stranded wire has to be threaded into small holes, a stray strand can short two pads, the joints break when the wire is tugged, and the Craft could never be unplugged from the Pi.
- **A short single-row strip in the inner row (chosen).** Five joints, all in one line. Pins 1, 3, 5, 7 and 9 happen to hold everything needed: power, data, clock, a spare, ground. The 5 V pins are in the other row and get no connector at all, so nothing can be plugged into 5 V by mistake.

The strip is a **female** one (holes, like the Flipper's), not a male one (pins). Then the same four male-to-female wires serve both brains: they stay on the Craft, and the pin ends go into the Flipper or into the Pi. A socket also has no bare live pins to short. If only a male pin strip can be found, solder it in the same five holes and use female-to-female wires; nothing else changes.

## Why USB MIDI and not Wi-Fi

The Pi shows up on the Mac as a USB MIDI keyboard (the kernel's `g_midi` gadget), and `play.html` plays what it sends.

- One cable gives the Pi power and carries the notes. No network is involved, so it works at the library, where the Pi does not know the Wi-Fi.
- Over Wi-Fi the Pi would have to join a network the Mac is also on, and public Wi-Fi usually stops devices from talking to each other. Notes would also arrive with uneven delay.
- MIDI works with any synth later (GarageBand, Logic), not only this page.

The cost: Chrome is required (Safari has no Web MIDI), and the Pi's USB port can be one thing at a time, so USB networking to that Pi is switched off.

## Which Pi

**Put it on the second, unused Pi (pizero2), not on the mesh base station.** `install.sh` refuses to run on a Pi that has `mesh-listen.service` unless forced.

The second Pi needs its own microSD card. As of 25 Sep (log.org) the only good card is in the mesh Pi and the grey/red card will not boot. An image for it is already made at `~/pi-setup/pios2.img` (hostname `pizero2`).

## What installing changes on a Pi

`sudo ./install.sh --dry-run` prints this list for the Pi it is run on and changes nothing.

1. `/boot/firmware/config.txt`: adds a marked block at the end with `dtparam=i2c_arm=on` (switches I2C on) and, only if missing, `dtoverlay=dwc2,dr_mode=peripheral` (lets the USB port act as a device).
2. `/etc/modules-load.d/trill-creature.conf`: loads `i2c-dev` and `g_midi` at boot.
3. `/etc/modprobe.d/trill-creature.conf`: names the USB device "Creature".
4. **Switches USB networking off** on that Pi: renames `/etc/modules-load.d/usb-gadget.conf` to `usb-gadget.conf.trill-disabled`, and swaps `g_ether` for `g_midi` in `cmdline.txt` if it is there. `ssh pizero2-usb` stops working. SSH over Wi-Fi is not touched.
5. `/opt/trill-creature/creature.py`: the program.
6. `/etc/systemd/system/trill-creature.service`: enabled, starts at every boot, runs as root, keeps its settings in `/var/lib/trill-creature/settings.json`.
7. Copies of every edited file go to `/var/backups/trill-creature/<time>/`.

It installs no packages and downloads nothing. It needs one reboot. `sudo ./install.sh --uninstall` reverses all of it except that I2C stays as it was found.

On the mesh base station the same install would also mean a reboot (the mesh radio link drops until it comes back), the loss of `ssh pizero-usb`, and a second always-running program on a single-core board.

## Steps (none of these have been done)

1. Write `~/pi-setup/pios2.img` to the new microSD (Raspberry Pi Imager, "Use custom", or the way the earlier card was written on 25 Sep). Put it in the second Pi and power it. Wait two minutes.
2. `scp -r ~/repos/trill/pi pizero2:trill-pi`
3. `ssh pizero2`
4. `cd trill-pi && sudo ./install.sh --dry-run` and read the list.
5. `sudo ./install.sh` then `sudo reboot`

## Test at home, before the library

This needs no soldering and no Craft.

1. Plug the Mac into the Pi's **middle** socket (marked USB). Wait one minute.
2. Open `play.html` in Google Chrome. Press Start sound. Allow MIDI if asked.
3. Expected: the page says "Pi is here. Searching for the Craft." and blips every two seconds.

If that works, the whole software and USB chain works, and the only thing left for the library is the five solder joints and four wires. If the page says "No Pi yet", the cable or the install is the problem, and it is better to find that at home.

A Trill Bar or Square can stand in for the Craft with no soldering on the sensor side: the program accepts any Trill sensor.

## Looking inside

- What the program is doing: `ssh pizero2 journalctl -u trill-creature -n 20`
- A plain-words wiring check: `sudo systemctl stop trill-creature; sudo python3 /opt/trill-creature/creature.py --check; sudo systemctl start trill-creature` (stop the service first, or the two will talk over each other on the wires).
- Live numbers for all 30 pads: `sudo python3 /opt/trill-creature/creature.py --print` (also with the service stopped).

## Tests

- `python3 test_creature.py` -- 17 tests against a simulated Trill: command order, thresholds, debounce, MIDI bytes, page controls, unplugging.
- `bash test_install.sh` -- runs the installer against pretend Pi file systems: fresh Pi, a Pi with USB networking on, the mesh Pi (refused), twice in a row, and uninstall (config.txt comes back byte for byte).

## Not verified

- Anything on real hardware: the I2C conversation with the Craft, `g_midi` loading, the Mac seeing the Pi as a MIDI device, Chrome receiving the notes, the systemd service.
- The Craft's firmware version. The program handles both the old and the new command style, as Bela's library does.
- Touch thresholds. They are copied from the Flipper app, which is also untested on the Craft.
- How much of the Pi Zero's single core the program uses. One pass of the loop takes about 5 microseconds on the Mac; the Pi is perhaps a hundred times slower, which would still be well under one millisecond per pass.
