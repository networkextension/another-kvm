"""macOS IOHID input value observer, matched to one USB device location.

No exclusive seize, global keyboard hook or injected software events.
IOHID access may require Input Monitoring permission for the launching terminal.
"""
import ctypes as C
from collections import deque
import re
import subprocess
import sys
import time


def inventory():
    if sys.platform != "darwin":
        raise RuntimeError("USB inventory requires macOS")
    # Text output also works when macOS cannot open the plist archive DTD.
    raw = subprocess.check_output(["/usr/sbin/ioreg", "-p", "IOUSB", "-l", "-w", "0"], text=True)
    return parse_inventory(raw)


def parse_inventory(raw):
    devices = []
    for node in re.split(r"^.*\+-o ", raw, flags=re.MULTILINE):
        properties = {}
        for key in ("idVendor", "idProduct", "locationID"):
            match = re.search(r'"' + key + r'" = (\d+)', node)
            if match:
                properties[key] = int(match.group(1))
        if "idVendor" in properties and "idProduct" in properties:
            product = re.search(r'"USB Product Name" = "([^"\n]*)"', node)
            devices.append({"vid": properties["idVendor"], "pid": properties["idProduct"],
                            "product": product.group(1) if product else "",
                            "location": properties.get("locationID")})
    return devices


class Monitor:
    def __init__(self, vid=0x1A86, pid=0xE129, location=None):
        if sys.platform != "darwin":
            raise RuntimeError("loopback currently supports macOS only")
        candidates = [d for d in inventory() if (d["vid"], d["pid"]) == (vid, pid)
                      and (location is None or d["location"] == location)]
        if len(candidates) != 1:
            raise RuntimeError(f"expected one matching HID USB device, found {candidates}; "
                               "select --hid-vid/--hid-pid/--hid-location")
        self.location = candidates[0]["location"]
        if self.location is None:
            raise RuntimeError("matching USB device has no locationID")
        self.vid, self.pid = vid, pid
        self.events = deque(maxlen=512)
        self.manager = None
        self.scheduled = False
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.io = C.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        self._declare()

    def _declare(self):
        p, u, l = C.c_void_p, C.c_uint32, C.c_long
        declarations = [
            (self.cf, "CFStringCreateWithCString", p, [p, C.c_char_p, u]),
            (self.cf, "CFNumberCreate", p, [p, l, p]),
            (self.cf, "CFDictionaryCreateMutable", p, [p, l, p, p]),
            (self.cf, "CFDictionarySetValue", None, [p, p, p]),
            (self.cf, "CFRelease", None, [p]),
            (self.cf, "CFRunLoopGetCurrent", p, []),
            (self.cf, "CFRunLoopRunInMode", C.c_int32, [p, C.c_double, C.c_bool]),
            (self.cf, "CFSetGetCount", l, [p]),
            (self.io, "IOHIDManagerCreate", p, [p, u]),
            (self.io, "IOHIDManagerSetDeviceMatching", None, [p, p]),
            (self.io, "IOHIDManagerOpen", C.c_int32, [p, u]),
            (self.io, "IOHIDManagerClose", C.c_int32, [p, u]),
            (self.io, "IOHIDManagerCopyDevices", p, [p]),
            (self.io, "IOHIDManagerScheduleWithRunLoop", None, [p, p, p]),
            (self.io, "IOHIDManagerUnscheduleFromRunLoop", None, [p, p, p]),
            (self.io, "IOHIDManagerRegisterInputValueCallback", None, [p, p, p]),
            (self.io, "IOHIDValueGetElement", p, [p]),
            (self.io, "IOHIDValueGetIntegerValue", l, [p]),
            (self.io, "IOHIDElementGetUsagePage", u, [p]),
            (self.io, "IOHIDElementGetUsage", u, [p]),
        ]
        for lib, name, result, args in declarations:
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = result, args
        self.mode = p.in_dll(self.cf, "kCFRunLoopDefaultMode").value
        self.callback_type = C.CFUNCTYPE(None, p, C.c_int32, p, p)

    def __enter__(self):
        # CFType callbacks retain matching keys/numbers for the dictionary lifetime.
        keys = (C.c_byte * 1).in_dll(self.cf, "kCFTypeDictionaryKeyCallBacks")
        values = (C.c_byte * 1).in_dll(self.cf, "kCFTypeDictionaryValueCallBacks")
        dictionary = self.cf.CFDictionaryCreateMutable(
            None, 3, C.addressof(keys), C.addressof(values))
        if not dictionary:
            raise MemoryError("CFDictionaryCreateMutable failed")
        objects = []
        try:
            self.manager = self.io.IOHIDManagerCreate(None, 0)
            if not self.manager:
                raise RuntimeError("IOHIDManagerCreate failed")
            for key, value in (("VendorID", self.vid), ("ProductID", self.pid),
                               ("LocationID", self.location)):
                key_ref = self.cf.CFStringCreateWithCString(None, key.encode(), 0x08000100)
                number = C.c_int64(value)
                value_ref = self.cf.CFNumberCreate(None, 4, C.byref(number))  # SInt64
                objects.extend((key_ref, value_ref))
                self.cf.CFDictionarySetValue(dictionary, key_ref, value_ref)
            self.io.IOHIDManagerSetDeviceMatching(self.manager, dictionary)
            self.callback = self.callback_type(self._value)
            self.io.IOHIDManagerRegisterInputValueCallback(self.manager, self.callback, None)
            self.runloop = self.cf.CFRunLoopGetCurrent()
            self.io.IOHIDManagerScheduleWithRunLoop(self.manager, self.runloop, self.mode)
            self.scheduled = True
            result = self.io.IOHIDManagerOpen(self.manager, 0)
            if result:
                raise PermissionError(f"IOHIDManagerOpen failed: 0x{result & 0xffffffff:08x}; "
                                      "check Terminal's Input Monitoring permission")
            devices = self.io.IOHIDManagerCopyDevices(self.manager)
            try:
                if not devices or not self.cf.CFSetGetCount(devices):
                    raise RuntimeError("no HID interfaces at selected USB location")
            finally:
                if devices:
                    self.cf.CFRelease(devices)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise
        finally:
            for ref in objects:
                self.cf.CFRelease(ref)
            self.cf.CFRelease(dictionary)

    def _value(self, context, result, sender, value):
        if result or not value:
            return
        element = self.io.IOHIDValueGetElement(value)
        self.events.append({"page": self.io.IOHIDElementGetUsagePage(element),
                            "usage": self.io.IOHIDElementGetUsage(element),
                            "value": self.io.IOHIDValueGetIntegerValue(value)})

    def pump(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.cf.CFRunLoopRunInMode(self.mode, min(.02, deadline - time.monotonic()), False)

    def __exit__(self, *args):
        if self.manager:
            if self.scheduled:
                self.io.IOHIDManagerUnscheduleFromRunLoop(self.manager, self.runloop, self.mode)
            self.io.IOHIDManagerClose(self.manager, 0)
            self.cf.CFRelease(self.manager)
            self.manager = None
