#!/usr/bin/env python3
"""Tests for creature.py against a simulated Trill Craft. Run on any computer:

    python3 test_creature.py

This proves the logic (protocol order, thresholds, debounce, MIDI bytes,
control messages). It does NOT prove the real I2C or USB hardware path.
"""
import os
import tempfile
import unittest

import creature as c


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeBus:
    """Behaves like a Trill on the I2C bus, as far as Trill-Linux describes it:
    a command is [0, cmd, args...]; the next 1-byte read is the ack (fw 3+);
    writing [4] points reads at the 30 big-endian channel words."""

    def __init__(self, dev_type=3, fw=3, addr=0x30):
        self.dev_type, self.fw, self.addr = dev_type, fw, addr
        self.present = True
        self.selected = None
        self.offset = 0
        self.last_cmd = 0
        self.commands = []
        self.levels = [0] * 30
        self.baselines = 0

    def set_address(self, addr):
        self.selected = addr

    def _alive(self):
        if not self.present or self.selected != self.addr:
            raise OSError(121, "Remote I/O error")

    def write(self, data):
        self._alive()
        self.offset = data[0]
        if len(data) > 1:
            self.last_cmd = data[1]
            self.commands.append(tuple(data[1:]))
            if data[1] == c.CMD_BASELINE_UPDATE:
                self.baselines += 1

    def read(self, n):
        self._alive()
        if self.offset == c.OFFSET_COMMAND:
            if self.last_cmd == c.CMD_IDENTIFY:
                return bytes((c.CMD_ACK, self.dev_type, self.fw))[:n]
            return bytes((c.CMD_ACK, self.last_cmd, 0))[:n]
        out = bytearray()
        for v in self.levels:
            out += bytes((v >> 8, v & 0xFF))
        return bytes(out[:n])


class FakeMidi:
    def __init__(self):
        self.sent = bytearray()
        self.inbox = b""

    def send(self, data):
        self.sent += data

    def receive(self):
        data, self.inbox = self.inbox, b""
        return data

    def messages(self):
        """Split what was sent into 3-byte messages."""
        return [tuple(self.sent[i:i + 3]) for i in range(0, len(self.sent), 3)]

    def notes(self):
        return [m for m in self.messages() if m[0] in (c.NOTE_ON, c.NOTE_OFF)]

    def side(self, key):
        vals = [m[2] for m in self.messages() if m[0] == c.SIDE_PRESSURE and m[1] == key]
        return vals[-1] if vals else None


def make(bus=None, **kw):
    clock = FakeClock()
    bus = bus or FakeBus()
    midi = FakeMidi()
    cr = c.Creature(c.Trill(bus, sleep=clock.sleep), midi, clock=clock.now, sleep=clock.sleep, **kw)
    return cr, bus, midi, clock


def run(cr, clock, steps):
    for _ in range(steps):
        cr.step()
        clock.sleep(c.POLL_S)


class CreatureTests(unittest.TestCase):
    def test_finds_craft_and_configures_in_bela_order(self):
        cr, bus, midi, clock = make()
        run(cr, clock, 1)
        self.assertEqual(cr.state, c.STATE_PLAYING)
        self.assertEqual(cr.trill.name, "Craft")
        cmds = [x for x in bus.commands if x[0] != c.CMD_IDENTIFY]
        self.assertEqual(cmds, [
            (c.CMD_SCAN_TRIGGER, 0), (c.CMD_MODE, 3), (c.CMD_PRESCALER, 1),
            (c.CMD_SCAN_SETTINGS, 0, 12), (c.CMD_NOISE_THRESHOLD, 0x28),
            (c.CMD_SCAN_TRIGGER, 1), (c.CMD_BASELINE_UPDATE,)])
        self.assertEqual(bus.offset, c.OFFSET_DATA)
        self.assertEqual(midi.side(c.KEY_STATE), c.STATE_PLAYING)

    def test_old_firmware_skips_scan_trigger(self):
        cr, bus, midi, clock = make(FakeBus(fw=2))
        run(cr, clock, 1)
        self.assertEqual(cr.state, c.STATE_PLAYING)
        self.assertNotIn(c.CMD_SCAN_TRIGGER, [x[0] for x in bus.commands])

    def test_touch_plays_note_after_debounce_and_releases(self):
        cr, bus, midi, clock = make()
        run(cr, clock, 1)
        bus.levels[0] = 600
        run(cr, clock, 1)
        self.assertEqual(midi.notes(), [])  # one frame is not enough
        run(cr, clock, 1)
        self.assertEqual(len(midi.notes()), 1)
        status, note, vel = midi.notes()[0]
        self.assertEqual((status, note), (c.NOTE_ON, 60))
        self.assertTrue(50 <= vel <= 127)
        bus.levels[0] = 0
        run(cr, clock, 2)
        self.assertEqual(len(midi.notes()), 1)  # two quiet frames: still held
        run(cr, clock, 1)
        self.assertEqual(midi.notes()[-1], (c.NOTE_OFF, 60, 0))

    def test_below_threshold_is_silent_and_hysteresis_holds(self):
        cr, bus, midi, clock = make()
        run(cr, clock, 1)
        bus.levels[3] = 279  # default threshold is 280
        run(cr, clock, 10)
        self.assertEqual(midi.notes(), [])
        bus.levels[3] = 400
        run(cr, clock, 3)
        bus.levels[3] = 200  # above the release level of 168
        run(cr, clock, 10)
        self.assertEqual([m[0] for m in midi.notes()], [c.NOTE_ON])

    def test_eight_pads_are_pentatonic_from_middle_c(self):
        cr, bus, midi, clock = make()
        self.assertEqual([cr.note_for(i) for i in range(8)], [60, 62, 64, 67, 69, 72, 74, 76])
        self.assertEqual(cr.note_for(15), cr.note_for(0))  # wraps after three octaves
        self.assertTrue(all(0 <= cr.note_for(i) <= 127 for i in range(30)))

    def test_several_pads_sound_together(self):
        cr, bus, midi, clock = make()
        run(cr, clock, 1)
        for pad in (0, 2, 4):
            bus.levels[pad] = 900
        run(cr, clock, 3)
        self.assertEqual(sorted(m[1] for m in midi.notes()), [60, 64, 69])

    def test_firmer_grab_is_louder(self):
        cr, bus, midi, clock = make()
        run(cr, clock, 1)
        bus.levels[0], bus.levels[1] = 300, 1500
        run(cr, clock, 3)
        vel = {m[1]: m[2] for m in midi.notes()}
        self.assertLess(vel[60], vel[62])

    def test_no_sensor_keeps_searching_then_recovers(self):
        bus = FakeBus()
        bus.present = False
        cr, bus, midi, clock = make(bus)
        run(cr, clock, 250)
        self.assertEqual(cr.state, c.STATE_SEARCHING)
        self.assertEqual(midi.side(c.KEY_STATE), c.STATE_SEARCHING)
        bus.present = True
        run(cr, clock, 120)
        self.assertEqual(cr.state, c.STATE_PLAYING)

    def test_unplugging_stops_notes_and_goes_back_to_searching(self):
        cr, bus, midi, clock = make()
        run(cr, clock, 1)
        bus.levels[0] = 900
        run(cr, clock, 3)
        bus.present = False
        run(cr, clock, c.MAX_READ_FAIL + 1)
        self.assertEqual(cr.state, c.STATE_SEARCHING)
        self.assertEqual(midi.notes()[-1], (c.NOTE_OFF, 60, 0))

    def test_noisy_pad_raises_its_own_threshold(self):
        bus = FakeBus()
        bus.levels[5] = 150  # noise present while calibrating
        cr, bus, midi, clock = make(bus)
        run(cr, clock, 1)
        self.assertEqual(cr.on_thr[5], 150 * 3 + 20)
        self.assertEqual(cr.on_thr[6], 280)

    def test_page_controls(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            cr, bus, midi, clock = make(state_file=path)
            run(cr, clock, 1)
            before = bus.baselines
            midi.inbox = bytes((c.SIDE_CC, c.CC_RECALIBRATE, 127))
            run(cr, clock, 1)
            self.assertEqual(bus.baselines, before + 1)
            self.assertEqual(cr.state, c.STATE_PLAYING)
            midi.inbox = bytes((c.SIDE_CC, c.CC_SENS, 6, c.CC_PRESCALER, 4))  # running status
            run(cr, clock, 1)
            self.assertEqual((cr.sens, cr.prescaler), (6, 4))
            self.assertIn((c.CMD_PRESCALER, 4), bus.commands)
            self.assertEqual(cr.on_thr[0], 100)
            self.assertEqual(midi.side(c.KEY_SENS), 6)
            self.assertEqual(midi.side(c.KEY_PRESCALER), 4)
            midi.inbox = bytes((c.SIDE_CC, c.CC_SCALE, 2))
            run(cr, clock, 1)
            self.assertEqual(cr.note_for(3), 65)  # major scale: F
            # a fresh program picks the saved settings up again
            cr2, bus2, midi2, clock2 = make(state_file=path)
            cr2.load_settings()
            self.assertEqual((cr2.sens, cr2.prescaler, cr2.scale), (6, 4, 2))

    def test_ordinary_notes_from_the_mac_are_ignored(self):
        cr, bus, midi, clock = make()
        run(cr, clock, 1)
        midi.inbox = bytes((0x90, 20, 100, 0xB0, c.CC_SENS, 0, 0xF8))
        run(cr, clock, 1)
        self.assertEqual(cr.sens, c.DEFAULT_SENS)

    def test_levels_side_channel(self):
        cr, bus, midi, clock = make()
        run(cr, clock, 1)
        bus.levels[7] = 1000
        run(cr, clock, 10)
        self.assertEqual(midi.side(7), 1000 >> 4)
        self.assertEqual(midi.side(c.KEY_THRESHOLD), 280 >> 4)
        self.assertEqual(len(midi.sent) % 3, 0)

    def test_bar_uses_only_the_strongest_channel(self):
        cr, bus, midi, clock = make(FakeBus(dev_type=1, addr=0x20))
        run(cr, clock, 1)
        self.assertEqual(cr.trill.name, "Bar")
        self.assertEqual(cr.prescaler, 2)
        bus.levels[10], bus.levels[11], bus.levels[12] = 700, 1200, 700
        run(cr, clock, 3)
        self.assertEqual(len(midi.notes()), 1)

    def test_no_levels_flag_sends_notes_only(self):
        cr, bus, midi, clock = make(levels=False)
        run(cr, clock, 1)
        bus.levels[0] = 900
        run(cr, clock, 150)
        self.assertTrue(all(m[0] in (c.NOTE_ON, c.NOTE_OFF, c.CC_CH1) for m in midi.messages()))


class MidiPortTests(unittest.TestCase):
    def test_writes_raw_bytes_and_never_blocks_on_an_empty_pipe(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "midi")
            os.mkfifo(path)
            reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            port = c.MidiPort(path)
            port.send(bytes((0x90, 60, 100)))
            self.assertEqual(os.read(reader, 16), bytes((0x90, 60, 100)))
            self.assertEqual(port.receive(), b"")  # nothing waiting: returns at once
            os.close(reader)

    def test_missing_device_is_not_an_error(self):
        port = c.MidiPort("/nonexistent/midi")
        port.send(b"\x90\x3c\x64")
        self.assertEqual(port.receive(), b"")


if __name__ == "__main__":
    unittest.main(verbosity=1)
