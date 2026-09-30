# Packing list: Raspberry Pi route

Written 30 Sep 2026. "Owned" means a note or photo says so; the source is given on each line. **FLAG** marks anything that could not be confirmed.

## Must buy

- [ ] microSD card, 16 GB or larger, "A1" class (for example "microSDHC 32 GB A1"). For the second Pi. The only good card is in the mesh Pi (log.org, 25 Sep).
- [ ] Female pin header (also sold as "header socket strip"), single row, 2.54 mm pitch, straight, 5 positions. A 6, 8 or 10 position one works the same way. This is the socket that gets soldered onto the Pi.
- [ ] Dupont jumper wires, male-to-female, 20 cm, a ribbon of 40. Four are used. The same four serve the Flipper. KB said on 30 Sep that he has no wires.

## Buy only if needed

- [ ] USB-C to Micro-USB (micro-B) data cable, USB 2.0, 0.5 to 1 m. Needed only if the ISY cable below is busy powering the mesh Pi. It must be a data cable, not a charge-only one.

## Already owned

- [ ] Second Raspberry Pi Zero W, the unused one. Source: S22 photos, 18 Sep; it booted on 25 Sep (log.org). **FLAG:** its pads were bare on 18 Sep; not re-checked since.
- [ ] Trill Craft and its loose 6-pin right-angle header. Source: KB's photos, 30 Sep.
- [ ] Flipper Zero. It is the quick tester for the Craft's solder joints. Source: project memory, 30 Sep.
- [ ] MacBook Pro with Google Chrome, charged, with `~/repos/trill/pi/` on it. The guide and the player open from disk, so no Wi-Fi is needed. Source: checked on the Mac, 30 Sep.
- [ ] microSD to SD adapter, for flashing the new card at home. Source: project memory, 24 Sep. **FLAG:** not seen since.
- [ ] ISY USB 2.0 data cable to micro-USB, plus the USB-C hub it plugs into. Source: memory note of 24 Sep (KB photographed the box). **FLAG:** it may be the cable that powers the mesh Pi, which must stay on. If so, buy the cable above.
- [ ] Trill Bar with its cable. Source: project memory, 24 Sep. Its four pins push straight into the new Pi socket, so it can test the Pi's joints without the Craft.

## Not in hand

- Kiwi Electronics order (hammer header, 40 female-to-female wires, USB-C to micro-B adapter, soldering station, solder, flux). The mail shows "Payment received" on 28 Sep and no shipping notice. Do not count on it. If it arrives, the adapter solves the cable question.

## The makerspace should have these

**FLAG:** none of these were confirmed with the library. Ask before going.

- [ ] Soldering iron with a fine tip, and a stand.
- [ ] Thin solder, 0.5 to 0.8 mm, with a flux core.
- [ ] A board holder or "helping hands".
- [ ] Masking tape or Blu-Tack, to hold the socket while the board is upside down.
- [ ] A magnifier lamp.
- [ ] A multimeter with a beep test, to check that no two joints touch.
- [ ] Safety glasses.

## Nice to have

- [ ] Headphones for the Mac. It is a library, and the test makes sound. **FLAG:** not confirmed that KB owns wired ones.
- [ ] Male pin header strip, single row, 1x40, 2.54 mm, breakable, plus female-to-female jumper wires. The fallback if no female strip can be found: same five holes, other kind of wire. Kiwi listed "40-pin Header Strip - 2.54mm pitch" at EUR 0.96, in stock, when read on 30 Sep.
- [ ] A jumper wire kit with all three kinds (male-male, male-female, female-female). It costs little more than one ribbon and covers every case.
- [ ] A small box or bag for the Pi, so the new socket does not get bent on the way home.
- [ ] A second five-hole socket strip, in case the first goes on crooked.

## Do at home first

- [ ] Flash the new card and install the program (README.md, "Steps").
- [ ] Run the home test (README.md, "Test at home, before the library"). The player page should say "Pi is here. Searching for the Craft."
- [ ] If the home test has not been done by library day, still take the Pi: the five joints can be soldered anyway, and the Flipper can test the Craft.
