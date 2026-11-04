#!/usr/bin/env python3
"""Type hello world through CH9329 at the focused input; do not press Enter."""
import argparse
import time

from ch9329 import ProtocolError, choose_port, connect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="default: auto-detect CH340")
    parser.add_argument("--delay", type=float, default=5, help="seconds before typing")
    args = parser.parse_args()
    if args.delay < 0:
        parser.error("delay must be nonnegative")
    port = args.port or choose_port()
    with connect(port, 9600, 0, .5, False) as device:
        info, config = device.info(), device.config()
        if not info["usb_enumerated"] or config["work_mode"] != 0 or config["serial_mode"] != 0:
            raise ProtocolError("requires enumerated composite HID in serial protocol mode")
        print(f"Typing hello world in {args.delay:g}s...", flush=True)
        time.sleep(args.delay)
        # US keyboard usages. Compensate for Caps Lock without toggling it.
        modifiers = 2 if device.info()["caps_lock"] else 0
        usages = (0x0B, 0x08, 0x0F, 0x0F, 0x12, 0x2C, 0x1A, 0x12, 0x15, 0x0F, 0x07)
        try:
            for usage in usages:
                device.send_keyboard([usage], modifiers if usage != 0x2C else 0)
                time.sleep(.04)
                device.send_keyboard()
                time.sleep(.04)
        finally:
            device.release_best_effort()
    print("Sent hello world; all serial ACKs passed. No Enter sent.", flush=True)


if __name__ == "__main__":
    main()
