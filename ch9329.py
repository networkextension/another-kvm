#!/usr/bin/env python3
"""CH340 -> UART -> CH9329 test tool. No persistent configuration writes."""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import time

HEADER = b"\x57\xab"
STATUS = {0xE1: "byte timeout", 0xE2: "bad header", 0xE3: "bad command",
          0xE4: "bad checksum", 0xE5: "bad parameter", 0xE6: "execution failed"}


class ProtocolError(RuntimeError):
    pass


def frame(command: int, data: bytes = b"", address: int = 0) -> bytes:
    if not 0 <= address <= 254:
        raise ValueError("address must be 0..254; broadcast has no reply")
    if not 0 <= command <= 255 or len(data) > 64:
        raise ValueError("invalid command or payload length")
    packet = HEADER + bytes((address, command, len(data))) + data
    return packet + bytes((sum(packet) & 255,))


@dataclass(frozen=True)
class Packet:
    address: int
    command: int
    data: bytes


class Parser:
    """Streaming decoder: handles fragmented/concatenated frames and noise."""
    def __init__(self):
        self.buffer = bytearray()
        self.bad_checksums = 0

    def feed(self, data: bytes) -> list[Packet]:
        self.buffer.extend(data)
        packets = []
        while True:
            start = self.buffer.find(HEADER)
            if start < 0:
                self.buffer[:] = b"\x57" if self.buffer.endswith(b"\x57") else b""
                break
            del self.buffer[:start]
            if len(self.buffer) < 5:
                break
            size = self.buffer[4]
            if size > 64:
                del self.buffer[0]
                continue
            if len(self.buffer) < size + 6:
                break
            raw = bytes(self.buffer[:size + 6])
            if sum(raw[:-1]) & 255 != raw[-1]:
                self.bad_checksums += 1
                del self.buffer[0]
                continue
            del self.buffer[:size + 6]
            packets.append(Packet(raw[2], raw[3], raw[5:-1]))
        return packets


def keyboard(keys=(), modifiers=0) -> bytes:
    keys = tuple(keys)
    if len(keys) > 6 or any(not 4 <= k <= 0xA4 for k in keys):
        raise ValueError("use up to six ordinary HID keycodes (modifiers separately)")
    return bytes((modifiers, 0, *keys)) + bytes(6 - len(keys))


def relative(dx=0, dy=0, wheel=0, buttons=0) -> bytes:
    if not 0 <= buttons <= 7 or any(not -127 <= v <= 127 for v in (dx, dy, wheel)):
        raise ValueError("relative axes/wheel must be -127..127; buttons 0..7")
    return bytes((1, buttons, dx & 255, dy & 255, wheel & 255))


def absolute(x, y, wheel=0, buttons=0) -> bytes:
    if not 0 <= x <= 4095 or not 0 <= y <= 4095:
        raise ValueError("absolute coordinates must be 0..4095")
    if not -127 <= wheel <= 127 or not 0 <= buttons <= 7:
        raise ValueError("invalid wheel/buttons")
    return bytes((2, buttons)) + x.to_bytes(2, "little") + y.to_bytes(2, "little") + bytes((wheel & 255,))


def decode_info(data):
    if len(data) != 8:
        raise ProtocolError(f"GET_INFO expected 8 bytes, got {len(data)}")
    return {"version_raw": f"0x{data[0]:02x}", "usb_enumerated": data[1] == 1,
            "usb_state_raw": data[1], "num_lock": bool(data[2] & 1),
            "caps_lock": bool(data[2] & 2), "scroll_lock": bool(data[2] & 4)}


def decode_config(data):
    if len(data) != 50:
        raise ProtocolError(f"GET_CONFIG expected 50 bytes, got {len(data)}")
    return {"work_mode": data[0] & 0x7f, "work_mode_raw": data[0],
            "serial_mode": data[1] & 0x7f, "serial_mode_raw": data[1],
            "address": data[2], "baudrate": int.from_bytes(data[3:7], "big"),
            "packet_interval_ms": int.from_bytes(data[9:11], "big"),
            "vid_pid_raw": data[11:15].hex(" "),
            "string_flags": data[36], "raw": data.hex(" ")}


class Device:
    def __init__(self, port, address=0, timeout=.5, verbose=False):
        self.port, self.address, self.timeout, self.verbose = port, address, timeout, verbose
        self.parser = Parser()
        self.unsolicited = deque(maxlen=64)
        self.failed = False
        self.last_tx = 0.0

    def request(self, command, data=b"", ack=False):
        if self.failed:
            raise ProtocolError("previous request failed; reopen port before continuing")
        # Never retry actions automatically: an ACK can be lost after HID delivery.
        raw = frame(command, data, self.address)
        time.sleep(max(0, .01 - (time.monotonic() - self.last_tx)))
        if self.verbose:
            print("TX", raw.hex(" "), file=sys.stderr)
        try:
            if self.port.write(raw) != len(raw):
                raise ProtocolError("short serial write")
            self.port.flush()
            self.last_tx = time.monotonic()
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                chunk = self.port.read(max(1, min(self.port.in_waiting, 256)))
                if self.verbose and chunk:
                    print("RX", chunk.hex(" "), file=sys.stderr)
                for packet in self.parser.feed(chunk):
                    if packet.address != self.address:
                        continue
                    if packet.command == 0x87:
                        self.unsolicited.append(packet)
                        continue
                    if packet.command not in (command | 0x80, command | 0xC0):
                        continue
                    if packet.command == command | 0xC0:
                        code = packet.data[0] if packet.data else None
                        raise ProtocolError(f"device error {code!r}: {STATUS.get(code, 'unknown')}")
                    if ack and packet.data != b"\x00":
                        raise ProtocolError(f"bad ACK: {packet.data.hex(' ')}")
                    return packet.data
            raise TimeoutError(f"command 0x{command:02x} timed out; baud/address/mode/cabling? "
                               f"bad checksums={self.parser.bad_checksums}")
        except BaseException:
            self.failed = True
            raise

    def info(self):
        return decode_info(self.request(1))

    def config(self):
        return decode_config(self.request(8))

    def send_keyboard(self, keys=(), modifiers=0):
        self.request(2, keyboard(keys, modifiers), ack=True)

    def move(self, dx=0, dy=0, wheel=0, buttons=0):
        self.request(5, relative(dx, dy, wheel, buttons), ack=True)

    def release_best_effort(self):
        # A timeout makes command/reply correlation unsafe. Release without claiming
        # ACK verification; caller still exits with failure for the original error.
        errors = []
        for command, data in ((2, keyboard()), (5, relative())):
            try:
                if self.failed:
                    raw = frame(command, data, self.address)
                    if self.port.write(raw) != len(raw):
                        raise ProtocolError("short release write")
                    self.port.flush()
                    time.sleep(.03)
                else:
                    self.request(command, data, ack=True)
            except BaseException as exc:
                errors.append(str(exc))
        if errors:
            raise ProtocolError("release failed: " + "; ".join(errors))


def ports():
    from serial.tools import list_ports
    return [{"port": p.device, "description": p.description, "vid": p.vid,
             "pid": p.pid, "location": p.location} for p in list_ports.comports()]


def choose_port():
    found = [p["port"] for p in ports() if (p["vid"], p["pid"]) == (0x1A86, 0x7523)]
    if len(found) != 1:
        raise RuntimeError(f"expected one CH340, found {found}; specify --port")
    return found[0]


@contextmanager
def connect(name, baud, address, timeout, verbose):
    import serial
    with serial.Serial(name, baud, timeout=min(timeout, .02), write_timeout=timeout,
                       xonxoff=False, rtscts=False, dsrdtr=False, exclusive=True) as port:
        port.reset_input_buffer()
        yield Device(port, address, timeout, verbose)


def smoke(device):
    info, config = device.info(), device.config()
    if not info["usb_enumerated"]:
        raise ProtocolError("HID end not enumerated")
    if config["work_mode"] != 0 or config["serial_mode"] != 0:
        raise ProtocolError("smoke requires composite keyboard+mouse, protocol serial mode")
    # No text, clicks, wheel, multimedia keys or persistent writes.
    try:
        device.send_keyboard()
        device.move()
        device.send_keyboard(modifiers=2)  # Left Shift alone
        time.sleep(.08)
        device.send_keyboard()
        device.move(dx=8)
        time.sleep(.08)
        device.move(dx=-8)
    finally:
        device.release_best_effort()
    return {"info": info, "config": config, "serial_ack": "PASS",
            "hid_delivery": "NOT_VERIFIED (use loopback on macOS)"}


def loopback(device, vid, pid, location=None):
    from mac_hid import Monitor
    info, config = device.info(), device.config()
    if not info["usb_enumerated"] or config["work_mode"] != 0 or config["serial_mode"] != 0:
        raise ProtocolError("loopback requires enumerated composite HID + protocol mode")
    with Monitor(vid, pid, location) as monitor:
        monitor.pump(.1)
        try:
            device.send_keyboard()
            device.move()
            monitor.pump(.1)
            stages = []
            for name, action, expected in (
                ("left_shift_down", lambda: device.send_keyboard(modifiers=2), (7, 0xE1, 1)),
                ("left_shift_up", device.send_keyboard, (7, 0xE1, 0)),
                ("mouse_right", lambda: device.move(dx=8), (1, 0x30, 8)),
                ("mouse_left", lambda: device.move(dx=-8), (1, 0x30, -8)),
            ):
                monitor.events.clear()
                action()
                monitor.pump(.25)
                seen = list(monitor.events)
                stages.append({"stage": name, "pass": any(
                    (e["page"], e["usage"], e["value"]) == expected for e in seen),
                    "events": seen})
            return {"info": info, "config": config, "stages": stages,
                    "hid_delivery": "PASS" if all(s["pass"] for s in stages) else "FAIL"}
        finally:
            device.release_best_effort()
            monitor.pump(.05)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list", "info", "probe", "smoke", "loopback"))
    parser.add_argument("--port", help="default: auto-detect exactly one CH340 1a86:7523")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--address", type=lambda x: int(x, 0), default=0)
    parser.add_argument("--timeout", type=float, default=.5)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output", type=Path, help="save JSON result")
    parser.add_argument("--hid-vid", type=lambda x: int(x, 0), default=0x1A86)
    parser.add_argument("--hid-pid", type=lambda x: int(x, 0), default=0xE129)
    parser.add_argument("--hid-location", type=lambda x: int(x, 0))
    args = parser.parse_args(argv)
    if not 0 <= args.address <= 254 or args.timeout <= 0 or args.baud <= 0:
        parser.error("invalid address, timeout or baud")
    result = {"command": args.command, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    exit_code = 0
    try:
        if args.command == "list":
            result["serial_ports"] = ports()
            if sys.platform == "darwin":
                from mac_hid import inventory
                result["usb_devices"] = inventory()
        else:
            name = args.port or choose_port()
            result.update(port=name, baud=args.baud, address=args.address)
            if args.command == "probe":
                attempts = []
                for baud in dict.fromkeys((args.baud, 9600, 19200, 38400, 57600, 115200)):
                    try:
                        with connect(name, baud, args.address, args.timeout, args.verbose) as device:
                            info = device.info()
                        result.update(baud=baud, info=info)
                        break
                    except (TimeoutError, ProtocolError) as exc:
                        attempts.append({"baud": baud, "error": str(exc)})
                else:
                    raise ProtocolError(f"no valid GET_INFO response: {attempts}")
                result["failed_attempts"] = attempts
            else:
                with connect(name, args.baud, args.address, args.timeout, args.verbose) as device:
                    if args.command == "info":
                        result.update(info=device.info(), config=device.config())
                    elif args.command == "smoke":
                        result.update(smoke(device))
                    else:
                        result.update(loopback(device, args.hid_vid, args.hid_pid, args.hid_location))
                        if result["hid_delivery"] != "PASS":
                            exit_code = 1
        result["status"] = "PASS" if exit_code == 0 else "FAIL"
    except Exception as exc:
        result.update(status="ERROR", error=f"{type(exc).__name__}: {exc}")
        exit_code = 1
    except KeyboardInterrupt:
        result.update(status="INTERRUPTED")
        exit_code = 130
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
