#!/usr/bin/env python3
"""Brass Creature, Raspberry Pi side.

Reads a Bela Trill Craft over I2C and sends one MIDI note per touched pad out
of the Pi's USB port (the g_midi "gadget"), so a Mac hears the Pi as a MIDI
keyboard. Standard library only: nothing to apt-install, nothing to compile.

Wiring (Pi 40-pin header, physical pin numbers):
    pin 1  3V3   -> Craft +V
    pin 3  GPIO2 -> Craft SDA
    pin 5  GPIO3 -> Craft SCL
    pin 9  GND   -> Craft GND
The Pi has 1.8 k pull-ups on pins 3 and 5, so the Craft needs none.

The Trill protocol here follows BelaPlatform/Trill-Linux (lib/Trill.cpp) and
mirrors the Flipper app next door (../flipper/creature.c), so both brains
behave the same: same thresholds, same debounce, same scales.

MIDI it sends
    channel 1   note on / note off, one per pad
    channel 16  polyphonic key pressure, used as a side channel that ordinary
                synths ignore. key 0-29 = live level of that pad,
                key 127 = state (0 searching, 1 playing, 2 calibrating),
                key 126 = electrode size (prescaler), key 125 = sensitivity,
                key 124 = scale, key 123 = touch threshold.
MIDI it accepts (channel 16 control change, sent by play.html)
    CC 20 recalibrate, CC 21 sensitivity 0-7, CC 22 electrode size 1-8,
    CC 23 scale 0-5, CC 24 "say your state again".

NOT YET RUN ON HARDWARE. The logic is covered by test_creature.py against a
simulated Trill; the real I2C and USB paths are untested.
"""
from __future__ import annotations

import argparse
import fcntl
import glob
import json
import os
import signal
import struct
import sys
import time

# ---------------------------------------------------------------- constants

I2C_SLAVE = 0x0703  # ioctl: choose which address this file handle talks to

NUM_CH = 30
OFFSET_COMMAND = 0
OFFSET_DATA = 4
CMD_MODE = 1
CMD_SCAN_SETTINGS = 2
CMD_PRESCALER = 3
CMD_NOISE_THRESHOLD = 4
CMD_BASELINE_UPDATE = 6
CMD_SCAN_TRIGGER = 15  # firmware 3+
CMD_ACK = 254
CMD_IDENTIFY = 255
MODE_DIFF = 3
SCAN_BITS = 12
SCAN_TRIGGER_DISABLED = 0
SCAN_TRIGGER_I2C = 1
TYPE_CRAFT = 3

# (type, address, channels, default prescaler, name). The Craft is the
# sculpture. The others are accepted so the Pi wiring can be tested with a Bar
# or Square, which need no soldering.
TRILL_KINDS = (
    (3, 0x30, 30, 1, "Craft"),
    (1, 0x20, 26, 2, "Bar"),
    (2, 0x28, 30, 1, "Square"),
    (4, 0x38, 30, 2, "Ring"),
    (5, 0x40, 30, 1, "Hex"),
    (6, 0x48, 30, 4, "Flex"),
)

SCALES = (
    ("Pentatonic", (0, 2, 4, 7, 9)),
    ("Minor penta", (0, 3, 5, 7, 10)),
    ("Major", (0, 2, 4, 5, 7, 9, 11)),
    ("Harmonic minor", (0, 2, 3, 5, 7, 8, 11)),
    ("Whole tone", (0, 2, 4, 6, 8, 10)),
    ("Chromatic", tuple(range(12))),
)
OCTAVE_SPAN = 3  # pads beyond three octaves wrap around

# Touch threshold per sensitivity step, in 12-bit sensor units.
# A higher step means a lighter touch is enough.
SENS_THRESHOLD = (800, 560, 400, 280, 200, 140, 100, 70)
DEFAULT_SENS = 3

ON_FRAMES = 2  # debounce
OFF_FRAMES = 3
MAX_READ_FAIL = 8
POLL_S = 0.010
SEARCH_S = 1.0
HEARTBEAT_S = 1.0
LEVELS_S = 0.066

STATE_SEARCHING = 0
STATE_PLAYING = 1
STATE_CALIBRATING = 2

NOTE_ON = 0x90  # channel 1
NOTE_OFF = 0x80
CC_CH1 = 0xB0
SIDE_PRESSURE = 0xAF  # polyphonic key pressure, channel 16
SIDE_CC = 0xBF  # control change, channel 16
KEY_STATE = 127
KEY_PRESCALER = 126
KEY_SENS = 125
KEY_SCALE = 124
KEY_THRESHOLD = 123
CC_RECALIBRATE = 20
CC_SENS = 21
CC_PRESCALER = 22
CC_SCALE = 23
CC_HELLO = 24


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- I2C


class I2CBus:
    """The Pi's I2C bus as a plain file: write bytes, read bytes."""

    def __init__(self, bus: int) -> None:
        self.path = f"/dev/i2c-{bus}"
        self.fd = -1

    def open(self) -> None:
        if self.fd < 0:
            self.fd = os.open(self.path, os.O_RDWR)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def set_address(self, addr: int) -> None:
        self.open()
        fcntl.ioctl(self.fd, I2C_SLAVE, addr)

    def write(self, data: bytes) -> None:
        if os.write(self.fd, data) != len(data):
            raise OSError("short I2C write")

    def read(self, n: int) -> bytes:
        data = os.read(self.fd, n)
        if len(data) != n:
            raise OSError("short I2C read")
        return data


class Trill:
    """One Trill sensor in DIFF mode: 30 numbers, each 0 when untouched."""

    def __init__(self, bus, sleep=time.sleep) -> None:
        self.bus = bus
        self.sleep = sleep
        self.kind = None  # entry of TRILL_KINDS once found
        self.fw = 0
        self.dev_type = TYPE_CRAFT

    @property
    def name(self) -> str:
        return self.kind[4] if self.kind else "none"

    @property
    def channels(self) -> int:
        return self.kind[2] if self.kind else NUM_CH

    def command(self, *payload: int) -> None:
        """Send a command and give the sensor time to take it. Firmware 3+
        acknowledges commands; older firmware just needs a pause. A missing
        ack is not fatal."""
        self.bus.write(bytes((OFFSET_COMMAND,) + payload))
        if self.fw < 3:
            self.sleep(0.015)
            return
        wait, total = 0.001, 0.0
        while total < 0.2:
            self.sleep(wait)
            if self.bus.read(1)[0] == CMD_ACK:
                return
            total += wait
            wait *= 2
        log(f"warning: no ack for Trill command {payload[0]}")

    def identify(self, kind) -> bool:
        """Ask one address who is there."""
        try:
            self.bus.set_address(kind[1])
            self.bus.write(bytes((OFFSET_COMMAND, CMD_IDENTIFY)))
            self.sleep(0.025)
            ident = self.bus.read(3)
        except OSError:
            return False
        if ident[1] == 0:
            return False
        self.kind = kind
        self.fw = ident[2]
        self.dev_type = ident[1]
        return True

    def find(self) -> bool:
        kinds = ([self.kind] if self.kind else []) + list(TRILL_KINDS)
        return any(self.identify(k) for k in kinds)

    def configure(self, prescaler: int) -> None:
        if self.fw >= 3:
            self.command(CMD_SCAN_TRIGGER, SCAN_TRIGGER_DISABLED)
        self.command(CMD_MODE, MODE_DIFF)
        self.command(CMD_PRESCALER, prescaler)
        self.command(CMD_SCAN_SETTINGS, 0, SCAN_BITS)
        self.command(CMD_NOISE_THRESHOLD, 0x28)
        if self.fw >= 3:
            self.command(CMD_SCAN_TRIGGER, SCAN_TRIGGER_I2C)

    def update_baseline(self) -> None:
        """Tell the sensor "nobody is touching right now"."""
        self.command(CMD_BASELINE_UPDATE)
        self.point_at_data()

    def point_at_data(self) -> None:
        self.bus.write(bytes((OFFSET_DATA,)))
        self.sleep(0.002)

    def read_frame(self) -> tuple:
        return struct.unpack(">30H", self.bus.read(NUM_CH * 2))


# ---------------------------------------------------------------- MIDI


class MidiPort:
    """The USB MIDI gadget as a raw MIDI device file. Opens it when it
    appears, never blocks, and drops bytes rather than stall the touch loop."""

    def __init__(self, path: str | None = None) -> None:
        self.wanted = path
        self.path = None
        self.fd = -1
        self.next_try = 0.0

    @staticmethod
    def find() -> str | None:
        paths = sorted(glob.glob("/dev/snd/midiC*D*"))
        for p in paths:
            card = p.split("midiC")[1].split("D")[0]
            try:
                with open(f"/proc/asound/card{card}/id") as f:
                    ident = f.read().lower()
            except OSError:
                ident = ""
            if "midi" in ident or "gadget" in ident:
                return p
        return paths[0] if paths else None

    def ensure_open(self) -> bool:
        if self.fd >= 0:
            return True
        now = time.monotonic()
        if now < self.next_try:
            return False
        self.next_try = now + 2.0
        path = self.wanted or self.find()
        if not path:
            return False
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            log(f"MIDI: cannot open {path}: {e}")
            return False
        self.path = path
        log(f"MIDI: using {path}")
        return True

    def _drop(self, e: OSError) -> None:
        log(f"MIDI: lost {self.path}: {e}")
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = -1

    def send(self, data: bytes) -> None:
        if not self.ensure_open():
            return
        try:
            os.write(self.fd, data)
        except BlockingIOError:
            pass  # nobody is listening and the buffer is full: drop it
        except OSError as e:
            self._drop(e)

    def receive(self) -> bytes:
        if not self.ensure_open():
            return b""
        try:
            return os.read(self.fd, 256)
        except BlockingIOError:
            return b""
        except OSError as e:
            self._drop(e)
            return b""


class ControlParser:
    """Picks channel-16 control changes out of the incoming MIDI bytes."""

    def __init__(self) -> None:
        self.status = 0
        self.data: list[int] = []

    def feed(self, chunk: bytes) -> list[tuple[int, int]]:
        out = []
        for b in chunk:
            if b >= 0xF8:
                continue  # real-time bytes may appear anywhere
            if b & 0x80:
                self.status = b
                self.data = []
                continue
            if self.status != SIDE_CC:
                continue
            self.data.append(b)
            if len(self.data) == 2:
                out.append((self.data[0], self.data[1]))
                self.data = []
        return out


# ---------------------------------------------------------------- the creature


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


class Creature:
    def __init__(self, trill, midi, *, root=60, scale=0, sens=DEFAULT_SENS,
                 prescaler=None, levels=True, state_file=None,
                 clock=time.monotonic, sleep=time.sleep) -> None:
        self.trill = trill
        self.midi = midi
        self.root = root
        self.scale = clamp(scale, 0, len(SCALES) - 1)
        self.sens = clamp(sens, 0, len(SENS_THRESHOLD) - 1)
        self.prescaler = prescaler  # None = the sensor's own default
        self.levels = levels
        self.state_file = state_file
        self.clock = clock
        self.sleep = sleep

        self.state = STATE_SEARCHING
        self.noise = [0] * NUM_CH
        self.on_thr = [SENS_THRESHOLD[self.sens]] * NUM_CH
        self.debounce = [0] * NUM_CH
        self.on = [False] * NUM_CH
        self.sounding = [0] * NUM_CH  # MIDI note each held pad started
        self.sent_level = [-1] * NUM_CH
        self.frame = (0,) * NUM_CH
        self.read_fail = 0
        self.parser = ControlParser()
        self.next_heartbeat = 0.0
        self.next_levels = 0.0
        self.next_search = 0.0
        self.running = True

    # ----- settings that survive a restart

    def load_settings(self) -> None:
        if not self.state_file:
            return
        try:
            with open(self.state_file) as f:
                s = json.load(f)
        except (OSError, ValueError):
            return
        self.sens = clamp(int(s.get("sens", self.sens)), 0, len(SENS_THRESHOLD) - 1)
        self.scale = clamp(int(s.get("scale", self.scale)), 0, len(SCALES) - 1)
        if s.get("prescaler"):
            self.prescaler = clamp(int(s["prescaler"]), 1, 8)

    def save_settings(self) -> None:
        if not self.state_file:
            return
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"sens": self.sens, "scale": self.scale,
                           "prescaler": self.prescaler}, f)
            os.replace(tmp, self.state_file)
        except OSError as e:
            log(f"settings not saved: {e}")

    # ----- notes

    def note_for(self, pad: int) -> int:
        steps = SCALES[self.scale][1]
        d = pad % (len(steps) * OCTAVE_SPAN)
        return clamp(self.root + 12 * (d // len(steps)) + steps[d % len(steps)], 0, 127)

    def all_notes_off(self) -> None:
        for i in range(NUM_CH):
            if self.on[i]:
                self.midi.send(bytes((NOTE_OFF, self.sounding[i], 0)))
            self.on[i] = False
            self.debounce[i] = 0
        self.midi.send(bytes((CC_CH1, 123, 0)))  # "all notes off", for safety

    # ----- side channel

    def send_status(self) -> None:
        if not self.levels:
            return
        thr = SENS_THRESHOLD[self.sens] // 16
        self.midi.send(bytes((
            SIDE_PRESSURE, KEY_STATE, self.state,
            SIDE_PRESSURE, KEY_PRESCALER, self.prescaler or 0,
            SIDE_PRESSURE, KEY_SENS, self.sens,
            SIDE_PRESSURE, KEY_SCALE, self.scale,
            SIDE_PRESSURE, KEY_THRESHOLD, clamp(thr, 0, 127),
        )))

    def send_levels(self, everything: bool = False) -> None:
        if not self.levels:
            return
        out = bytearray()
        for i in range(NUM_CH):
            q = min(127, self.frame[i] >> 4)
            if everything or q != self.sent_level[i]:
                self.sent_level[i] = q
                out += bytes((SIDE_PRESSURE, i, q))
        if out:
            self.midi.send(bytes(out))

    def set_state(self, state: int) -> None:
        self.state = state
        self.send_status()

    # ----- sensor

    def update_thresholds(self) -> None:
        base = SENS_THRESHOLD[self.sens]
        self.on_thr = [max(base, n * 3 + 20) for n in self.noise]

    def calibrate(self) -> None:
        """Take a fresh "nobody is touching" baseline, then measure the resting
        noise on each pad so a noisy wire cannot trigger itself. Hands off
        while this runs (about a third of a second)."""
        self.all_notes_off()
        self.set_state(STATE_CALIBRATING)
        self.trill.update_baseline()
        self.sleep(0.06)
        noise = [0] * NUM_CH
        for _ in range(25):
            frame = self.trill.read_frame()
            noise = [max(a, b) for a, b in zip(noise, frame)]
            self.sleep(POLL_S)
        self.noise = noise
        self.frame = (0,) * NUM_CH
        self.update_thresholds()

    def connect(self) -> bool:
        """Look for a sensor and set it up. True when it is ready to play."""
        try:
            if not self.trill.find():
                return False
            if self.prescaler is None:
                self.prescaler = self.trill.kind[3]  # the sensor's own default
            self.trill.configure(self.prescaler)
            self.calibrate()
        except OSError as e:
            log(f"sensor setup failed: {e}")
            return False
        self.read_fail = 0
        self.set_state(STATE_PLAYING)
        self.send_levels(everything=True)
        log(f"found Trill {self.trill.name}, firmware {self.trill.fw}, "
            f"electrode size {self.prescaler}")
        return True

    def lose(self) -> None:
        log("sensor stopped answering; searching again")
        self.all_notes_off()
        self.frame = (0,) * NUM_CH
        self.set_state(STATE_SEARCHING)
        self.send_levels()

    # ----- one pass of the touch loop

    def play_step(self) -> None:
        try:
            frame = self.trill.read_frame()
        except OSError:
            self.read_fail += 1
            if self.read_fail >= MAX_READ_FAIL:
                self.lose()
            return
        self.read_fail = 0

        channels = self.trill.channels
        if channels < NUM_CH:
            frame = frame[:channels] + (0,) * (NUM_CH - channels)
        self.frame = frame

        # On a slider sensor one finger covers several neighbouring channels,
        # so only the strongest one counts. On the Craft every pad is its own.
        strongest = -1
        if self.trill.dev_type != TYPE_CRAFT:
            strongest = max(range(channels), key=frame.__getitem__)

        for i in range(NUM_CH):
            v = frame[i] if strongest < 0 or i == strongest else 0
            thr = self.on_thr[i]
            if not self.on[i]:
                if v > thr:
                    self.debounce[i] += 1
                    if self.debounce[i] >= ON_FRAMES:
                        self.on[i] = True
                        self.debounce[i] = 0
                        # a firmer grab is louder
                        x = min(1.0, max(0.0, (v - thr) / (3.0 * thr)))
                        self.sounding[i] = self.note_for(i)
                        self.midi.send(bytes((NOTE_ON, self.sounding[i], int(50 + 77 * x))))
                else:
                    self.debounce[i] = 0
            else:
                if v < thr * 3 // 5:
                    self.debounce[i] += 1
                    if self.debounce[i] >= OFF_FRAMES:
                        self.on[i] = False
                        self.debounce[i] = 0
                        self.midi.send(bytes((NOTE_OFF, self.sounding[i], 0)))
                else:
                    self.debounce[i] = 0

    # ----- requests from the player page

    def handle_control(self, cc: int, value: int) -> None:
        if cc == CC_HELLO:
            self.send_status()
            self.send_levels(everything=True)
            return
        if cc == CC_SENS:
            self.sens = clamp(value, 0, len(SENS_THRESHOLD) - 1)
            self.update_thresholds()
        elif cc == CC_SCALE:
            self.all_notes_off()
            self.scale = clamp(value, 0, len(SCALES) - 1)
        elif cc == CC_PRESCALER:
            self.prescaler = clamp(value, 1, 8)
            if self.state == STATE_PLAYING:
                try:
                    self.trill.configure(self.prescaler)
                    self.calibrate()
                    self.state = STATE_PLAYING
                except OSError:
                    self.lose()
        elif cc == CC_RECALIBRATE:
            if self.state == STATE_PLAYING:
                try:
                    self.calibrate()
                    self.state = STATE_PLAYING
                except OSError:
                    self.lose()
        else:
            return
        self.save_settings()
        self.send_status()

    def service_midi(self) -> None:
        for cc, value in self.parser.feed(self.midi.receive()):
            self.handle_control(cc, value)

    # ----- main loop

    def step(self) -> None:
        now = self.clock()
        self.service_midi()
        if self.state == STATE_SEARCHING:
            if now >= self.next_search:
                self.next_search = now + SEARCH_S
                self.connect()
        else:
            self.play_step()
            if now >= self.next_levels:
                self.next_levels = now + LEVELS_S
                self.send_levels()
        if now >= self.next_heartbeat:
            self.next_heartbeat = now + HEARTBEAT_S
            self.send_status()

    def run(self) -> None:
        self.load_settings()
        log("Brass Creature is running; looking for the Trill sensor")
        while self.running:
            started = self.clock()
            self.step()
            spare = POLL_S - (self.clock() - started)
            self.sleep(spare if spare > 0.001 else 0.001)
        self.all_notes_off()
        self.set_state(STATE_SEARCHING)


# ---------------------------------------------------------------- command line


def usb_host_state() -> str:
    for p in glob.glob("/sys/class/udc/*/state"):
        try:
            with open(p) as f:
                return f.read().strip()
        except OSError:
            pass
    return "no USB device controller (is dtoverlay=dwc2 set?)"


def check(bus_number: int, midi_path: str | None) -> int:
    """Say in plain words what works and what does not. Exit 0 if all good."""
    ok = True
    bus = I2CBus(bus_number)
    if not os.path.exists(bus.path):
        print(f"NO   {bus.path} is missing. I2C is switched off. Run the install script, then reboot.")
        ok = False
    else:
        print(f"OK   {bus.path} exists (I2C is switched on).")
        trill = Trill(bus)
        try:
            found = trill.find()
        except OSError as e:
            found = False
            print(f"NO   cannot use {bus.path}: {e}")
        if found:
            print(f"OK   Trill {trill.name} answered at address {trill.kind[1]:#x}, firmware {trill.fw}.")
        else:
            print("NO   no Trill sensor answered. Check the four wires: "
                  "red hole 1, white hole 2, yellow hole 3, black hole 5.")
            ok = False
    path = midi_path or MidiPort.find()
    if path:
        print(f"OK   MIDI gadget device {path} exists.")
    else:
        print("NO   no MIDI gadget device. The g_midi module is not loaded.")
        ok = False
    state = usb_host_state()
    if state == "configured":
        print("OK   a computer is connected on the USB port and sees the Pi.")
    else:
        print(f"--   USB state: {state}. 'configured' means the Mac sees the Pi. "
              "Use the middle socket (USB), not the end one (PWR).")
    return 0 if ok else 1


def monitor(creature: Creature) -> None:
    """Print the 30 pad levels five times a second. Ctrl-C stops it."""
    creature.load_settings()
    last = 0.0
    while True:
        creature.step()
        if creature.clock() - last > 0.2:
            last = creature.clock()
            if creature.state == STATE_SEARCHING:
                print("searching for the Trill sensor...")
            else:
                print(" ".join(f"{v:4d}" for v in creature.frame))
        time.sleep(POLL_S)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Trill Craft to USB MIDI, for a Raspberry Pi Zero W")
    ap.add_argument("--bus", type=int, default=1, help="I2C bus number (default 1)")
    ap.add_argument("--midi", help="raw MIDI device, e.g. /dev/snd/midiC1D0 (default: find it)")
    ap.add_argument("--root", type=int, default=60, help="MIDI note of pad 0 (default 60 = middle C)")
    ap.add_argument("--scale", type=int, default=0,
                    help="0 pentatonic, 1 minor pentatonic, 2 major, 3 harmonic minor, 4 whole tone, 5 chromatic")
    ap.add_argument("--sens", type=int, default=DEFAULT_SENS, help="touch sensitivity 0-7 (default 3)")
    ap.add_argument("--prescaler", type=int, help="electrode size 1-8; raise it for big pieces of metal")
    ap.add_argument("--state-file", default=os.environ.get("STATE_DIRECTORY", "") and
                    os.path.join(os.environ["STATE_DIRECTORY"], "settings.json"),
                    help="where settings changed from the player page are kept")
    ap.add_argument("--no-levels", action="store_true", help="send notes only, no live-level side channel")
    ap.add_argument("--check", action="store_true", help="test the wiring once, in plain words, and exit")
    ap.add_argument("--print", dest="monitor", action="store_true", help="print the 30 pad levels")
    args = ap.parse_args(argv)

    if args.check:
        return check(args.bus, args.midi)

    creature = Creature(Trill(I2CBus(args.bus)), MidiPort(args.midi), root=args.root,
                        scale=args.scale, sens=args.sens, prescaler=args.prescaler,
                        levels=not args.no_levels, state_file=args.state_file or None)

    def stop(_sig, _frm):
        creature.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if args.monitor:
        signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            monitor(creature)
        except KeyboardInterrupt:
            creature.all_notes_off()
        return 0
    creature.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
