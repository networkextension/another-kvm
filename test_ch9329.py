import unittest
from unittest.mock import patch

from ch9329 import (Device, Parser, ProtocolError, absolute, decode_config,
                    decode_info, frame, keyboard, relative)


class SerialStub:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.writes = []

    @property
    def in_waiting(self):
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, count):
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, raw):
        self.writes.append(raw)
        return len(raw)

    def flush(self):
        pass


class ProtocolTests(unittest.TestCase):
    def test_macos_usb_inventory_parser(self):
        from mac_hid import parse_inventory
        raw = '''+-o Root <class IORegistryEntry>
  +-o WCH UART TO KB-MS_V1.8@01130000 <class IOUSBHostDevice>
    "idProduct" = 57641
    "USB Product Name" = "WCH UART TO KB-MS_V1.8"
    "idVendor" = 6790
    "locationID" = 18022400
  +-o USB Serial@01120000 <class IOUSBHostDevice>
    "idVendor" = 6790
    "idProduct" = 29987
    "locationID" = 17956864
'''
        result = parse_inventory(raw)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], {"vid": 0x1a86, "pid": 0xe129,
                                    "product": "WCH UART TO KB-MS_V1.8", "location": 18022400})

    def test_manual_vectors(self):
        self.assertEqual(frame(1).hex(), "57ab00010003")
        self.assertEqual(frame(2, keyboard([4])).hex(), "57ab000208000004000000000010")
        self.assertEqual(frame(2, keyboard()).hex(), "57ab00020800000000000000000c")
        self.assertEqual(frame(5, relative(-3)).hex(), "57ab0005050100fd00000a")
        self.assertEqual(frame(4, absolute(320, 533)).hex(), "57ab0004070200400115020067")

    def test_fragmentation_noise_and_multiple_frames(self):
        parser = Parser()
        a, b = frame(0x81, bytes(8)), frame(0x82, b"\0")
        self.assertEqual(parser.feed(b"noise\x57"), [])
        self.assertEqual(parser.feed(a[1:4]), [])
        packets = parser.feed(a[4:] + b)
        self.assertEqual([p.command for p in packets], [0x81, 0x82])

    def test_corrupt_checksum_and_invalid_length_resync(self):
        parser = Parser()
        corrupt = bytearray(frame(0x81, bytes(8)))
        corrupt[-1] ^= 1
        packets = parser.feed(bytes(corrupt) + bytes.fromhex("57ab0081ff") + frame(0x82, b"\0"))
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0].command, 0x82)
        self.assertEqual(parser.bad_checksums, 1)

    def test_ignore_other_address_command_and_unsolicited(self):
        port = SerialStub([frame(0x81, bytes(8), 2) + frame(0x88, bytes(50)) +
                           frame(0x87, b"hi") + frame(0x81, b"\x30\x01" + bytes(6))])
        device = Device(port)
        self.assertTrue(device.info()["usb_enumerated"])
        self.assertEqual(device.unsolicited[0].data, b"hi")

    def test_error_ack_and_bad_success_ack(self):
        for response in (frame(0xC2, b"\xe6"), frame(0x82, b"\xe6"), frame(0x82)):
            with self.subTest(response=response):
                device = Device(SerialStub([response]))
                with self.assertRaises(ProtocolError):
                    device.send_keyboard()
                self.assertTrue(device.failed)

    def test_timeout_has_no_automatic_action_retry(self):
        port = SerialStub([])
        device = Device(port, timeout=.001)
        with self.assertRaises(TimeoutError):
            device.move(dx=8)
        self.assertEqual(len(port.writes), 1)
        with self.assertRaises(ProtocolError):
            device.move(dx=8)

    def test_release_after_lost_ack(self):
        port = SerialStub([])
        device = Device(port)
        device.failed = True
        with patch("ch9329.time.sleep"):
            device.release_best_effort()
        self.assertEqual(port.writes, [frame(2, keyboard()), frame(5, relative())])

    def test_config_offsets_and_info(self):
        raw = bytearray(50)
        raw[:3] = bytes((0x80, 0x80, 0))
        raw[3:7] = (9600).to_bytes(4, "big")
        raw[9:11] = b"\0\3"
        raw[36] = 0x87
        config = decode_config(raw)
        self.assertEqual((config["baudrate"], config["packet_interval_ms"], config["string_flags"]),
                         (9600, 3, 0x87))
        self.assertEqual(config["work_mode"], 0)
        self.assertTrue(decode_info(b"\x30\x01\x02" + bytes(5))["caps_lock"])
        for fn in (decode_info, decode_config):
            with self.assertRaises(ProtocolError):
                fn(b"")

    def test_input_bounds(self):
        for call in (lambda: frame(1, bytes(65)), lambda: frame(1, address=255),
                     lambda: keyboard([4] * 7), lambda: keyboard([0xe1]),
                     lambda: relative(128), lambda: absolute(4096, 0)):
            with self.assertRaises(ValueError):
                call()


if __name__ == "__main__":
    unittest.main()
