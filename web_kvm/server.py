#!/usr/bin/env python3
"""LAN web KVM: shared latest-frame capture and a single leased HID controller."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from contextlib import suppress
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import time
from urllib.parse import urlsplit

from aiohttp import web, WSMsgType
import serial
from ch9329 import Device, absolute, keyboard, relative, choose_port

LOG = logging.getLogger('kvm')
ROOT = Path(__file__).resolve().parent


def jpeg_dimensions(data):
    """Read JPEG SOF dimensions without an image-library dependency."""
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xff:
            return None
        marker = data[offset + 1]
        if marker in (0xda, 0xd9):
            return None
        size = int.from_bytes(data[offset + 2:offset + 4], 'big')
        if size < 2 or offset + 2 + size > len(data):
            return None
        if marker in (0xc0, 0xc1, 0xc2) and size >= 7:
            height = int.from_bytes(data[offset + 5:offset + 7], 'big')
            width = int.from_bytes(data[offset + 7:offset + 9], 'big')
            return width, height
        offset += 2 + size
    return None


def integer(data, name, minimum, maximum, default=None):
    value = data.get(name, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name}: expected integer {minimum}..{maximum}')
    return value


def validate_input(data):
    kind = data.get('type')
    if kind == 'keyboard':
        keys = data.get('keys')
        if not isinstance(keys, list) or len(keys) > 6 or any(type(k) is not int for k in keys):
            raise ValueError('keys: expected up to six HID usages')
        return 2, keyboard(keys, integer(data, 'modifiers', 0, 255, 0))
    if kind == 'mouse':
        buttons = integer(data, 'buttons', 0, 7, 0)
        wheel = integer(data, 'wheel', -127, 127, 0)
        if data.get('mode', 'absolute') == 'relative':
            return 5, relative(integer(data, 'dx', -127, 127, 0),
                               integer(data, 'dy', -127, 127, 0), wheel, buttons)
        if data.get('mode', 'absolute') != 'absolute':
            raise ValueError('unknown mouse mode')
        return 4, absolute(integer(data, 'x', 0, 4095), integer(data, 'y', 0, 4095), wheel, buttons)
    raise ValueError('unknown input type')


class HID:
    def __init__(self, port=None, baud=9600):
        self.name, self.baud = port, baud
        self.device = None
        self.lock = asyncio.Lock()
        self.error = 'Not connected'
        self.info = self.config = None
        self.last_buttons = 0
        self.last_position = (2048, 2048)
        self.last_mode = 'relative'
        self.acks = 0

    def _open(self):
        if self.device:
            self.device.port.close()
            self.device = None
        name = self.name or choose_port()
        port = serial.Serial(name, self.baud, timeout=.02, write_timeout=.5,
                             exclusive=True, xonxoff=False, rtscts=False, dsrdtr=False)
        try:
            port.reset_input_buffer()
            device = Device(port)
            info, config = device.info(), device.config()
            if not info['usb_enumerated'] or config['work_mode'] != 0 or config['serial_mode'] != 0:
                raise RuntimeError('CH9329 must enumerate as composite HID in protocol mode')
            # Recover any stale key/button state before accepting a controller.
            device.release_best_effort()
            self.device, self.info, self.config = device, info, config
            self.error = None
            self.last_buttons = 0
        except BaseException:
            port.close()
            raise

    async def connect(self):
        async with self.lock:
            try:
                await asyncio.to_thread(self._open)
            except Exception as exc:
                self.error = str(exc)
                LOG.warning('Serial connection: %s', exc)

    async def send(self, data):
        command, payload = validate_input(data)
        async with self.lock:
            if not self.device or self.error:
                raise RuntimeError(self.error or 'serial offline')
            try:
                await asyncio.to_thread(self.device.request, command, payload, True)
                self.acks += 1
                if command in (4, 5):
                    self.last_mode = 'absolute' if command == 4 else 'relative'
                    if command == 4:
                        self.last_position = (data['x'], data['y'])
                    self.last_buttons = data.get('buttons', 0)
            except Exception as exc:
                self.error = str(exc)
                raise

    async def release(self):
        async with self.lock:
            if not self.device:
                return
            try:
                # Best-effort release remains possible after a lost serial ACK.
                await asyncio.to_thread(self.device.release_best_effort)
                if self.last_mode == 'absolute':
                    x, y = self.last_position
                    if not self.device.failed:
                        await asyncio.to_thread(self.device.request, 4, absolute(x, y), True)
                    else:
                        from ch9329 import frame
                        await asyncio.to_thread(self.device.port.write, frame(4, absolute(x, y)))
                        await asyncio.to_thread(self.device.port.flush)
                self.last_buttons = 0
            except Exception as exc:
                self.error = str(exc)
                LOG.warning('Release: %s', exc)

    async def close(self):
        await self.release()
        if self.device:
            await asyncio.to_thread(self.device.port.close)

    def status(self):
        return {'online': bool(self.device and not self.error), 'error': self.error,
                'port': self.device.port.port if self.device else self.name,
                'baud': self.baud, 'info': self.info, 'config': self.config, 'acks': self.acks}


class Capture:
    def __init__(self, args):
        self.args = args
        self.process = None
        self.frame = None
        self.sequence = 0
        self.changed = asyncio.Condition()
        self.last_frame = 0
        self.error = 'Starting capture'
        self.logs = deque(maxlen=12)
        self.fps = 0
        self.frame_times = deque(maxlen=60)
        self.stopping = False
        self.dimensions = None

    async def stderr(self, stream):
        while line := await stream.readline():
            text = line.decode(errors='replace').strip()
            if text:
                self.logs.append(text)
                LOG.info('capture: %s', text)

    async def run(self):
        import imageio_ffmpeg
        exe = self.args.ffmpeg or imageio_ffmpeg.get_ffmpeg_exe()
        while not self.stopping:
            stderr_task = None
            try:
                cmd = [exe, '-hide_banner', '-loglevel', 'warning', '-nostdin',
                       '-f', 'avfoundation', '-framerate', str(self.args.capture_fps),
                       '-video_size', self.args.resolution, '-i', self.args.camera + ':none',
                       '-an', '-vf', f'fps={self.args.fps}', '-c:v', 'mjpeg',
                       '-q:v', str(self.args.quality), '-threads', '2', '-flush_packets', '1',
                       '-f', 'image2pipe', 'pipe:1']
                if self.args.capture_backend == 'native':
                    helper = ROOT / 'Camera.app' / 'Contents' / 'MacOS' / 'Capture'
                    cmd = [str(helper), self.args.camera, self.args.resolution, str(self.args.fps)]
                self.process = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stderr_task = asyncio.create_task(self.stderr(self.process.stderr))
                buf = bytearray()
                while True:
                    chunk = await asyncio.wait_for(self.process.stdout.read(65536), timeout=120 if self.sequence == 0 else 15)
                    if not chunk:
                        raise RuntimeError('Capture exited: ' + ' | '.join(self.logs)[-1500:])
                    buf.extend(chunk)
                    if len(buf) > 12 * 1024 * 1024:
                        raise RuntimeError('Oversized JPEG stream')
                    while True:
                        start = buf.find(b'\xff\xd8')
                        if start < 0:
                            if len(buf) > 1:
                                del buf[:-1]
                            break
                        end = buf.find(b'\xff\xd9', start + 2)
                        if end < 0:
                            if start:
                                del buf[:start]
                            break
                        frame = bytes(buf[start:end + 2])
                        del buf[:end + 2]
                        async with self.changed:
                            self.frame = frame
                            if not self.dimensions:
                                self.dimensions = jpeg_dimensions(frame)
                            self.sequence += 1
                            self.last_frame = time.monotonic()
                            self.frame_times.append(self.last_frame)
                            if len(self.frame_times) > 1:
                                self.fps = (len(self.frame_times) - 1) / (self.frame_times[-1] - self.frame_times[0])
                            self.error = None
                            self.changed.notify_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = str(exc) or 'No video frames for 15 seconds'
                LOG.warning('Video: %s', self.error)
            finally:
                if self.process and self.process.returncode is None:
                    self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), 3)
                    except asyncio.TimeoutError:
                        self.process.kill()
                        await self.process.wait()
                if stderr_task:
                    stderr_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await stderr_task
            await asyncio.sleep(3)

    def status(self):
        age = time.monotonic() - self.last_frame if self.last_frame else None
        return {'online': age is not None and age < 3, 'camera': self.args.camera,
                'resolution': 'x'.join(map(str, self.dimensions)) if self.dimensions else self.args.resolution,
                'requested_resolution': self.args.resolution, 'fps': round(self.fps, 1),
                'frames': self.sequence, 'frame_age_ms': round(age * 1000) if age is not None else None,
                'error': self.error, 'logs': list(self.logs)}


class KVM:
    def __init__(self, args):
        self.args = args
        self.hid = HID(args.serial, args.baud)
        self.capture = Capture(args)
        self.owner = None
        self.lease_lock = asyncio.Lock()
        self.last_heartbeat = 0
        self.password = args.password
        self.failed_logins = {}
        self.started = time.monotonic()

    def session(self, timestamp=None):
        timestamp = str(timestamp or int(time.time()))
        signature = hmac.new(self.password.encode(), timestamp.encode(), hashlib.sha256).hexdigest()
        return timestamp + '.' + signature

    def authorized(self, request):
        value = request.cookies.get('kvm_session', '')
        try:
            timestamp, _ = value.split('.', 1)
            age = time.time() - int(timestamp)
            return 0 <= age < 43200 and hmac.compare_digest(value, self.session(timestamp))
        except (ValueError, TypeError):
            return False

    @staticmethod
    def check_origin(request):
        origin = request.headers.get('Origin')
        if origin is None or urlsplit(origin).netloc != request.host or urlsplit(origin).scheme != request.scheme:
            raise web.HTTPForbidden(text='Same-origin browser connection required')

    async def login(self, request):
        self.check_origin(request)
        now = time.monotonic()
        recent = [t for t in self.failed_logins.get(request.remote, []) if now - t < 60]
        if len(recent) >= 8:
            raise web.HTTPTooManyRequests(text='Try again in one minute')
        data = await request.json()
        if not isinstance(data, dict) or not isinstance(data.get('password'), str):
            raise web.HTTPBadRequest()
        if not hmac.compare_digest(data['password'].encode(), self.password.encode()):
            self.failed_logins[request.remote] = recent + [now]
            raise web.HTTPUnauthorized(text='Incorrect access code')
        self.failed_logins.pop(request.remote, None)
        response = web.json_response({'ok': True})
        response.set_cookie('kvm_session', self.session(), httponly=True, samesite='Strict',
                            secure=request.secure, max_age=43200)
        return response

    async def status(self, request):
        return web.json_response({'video': self.capture.status(), 'hid': self.hid.status(),
                                  'controller': self.owner is not None,
                                  'uptime_seconds': int(time.monotonic() - self.started)})

    async def snapshot(self, request):
        if not self.capture.frame or not self.capture.status()['online']:
            raise web.HTTPServiceUnavailable(text=self.capture.error or 'No fresh video frame')
        return web.Response(body=self.capture.frame, content_type='image/jpeg')

    async def video(self, request):
        self.check_origin(request)
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=1024, compress=False)
        await ws.prepare(request)

        async def send_frames():
            sequence = -1
            while not ws.closed:
                async with self.capture.changed:
                    await self.capture.changed.wait_for(lambda: self.capture.sequence != sequence and self.capture.frame is not None)
                    sequence, frame = self.capture.sequence, self.capture.frame
                # Slow readers see the latest frame, never an unbounded frame queue.
                await asyncio.wait_for(ws.send_bytes(frame), timeout=3)

        task = asyncio.create_task(send_frames())
        task.add_done_callback(lambda _: asyncio.create_task(ws.close()) if not ws.closed else None)
        try:
            async for _ in ws:
                pass
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError, ConnectionError, asyncio.TimeoutError):
                await task
        return ws

    async def release_owner(self, ws):
        async with self.lease_lock:
            if self.owner is ws:
                await self.hid.release()
                self.owner = None

    async def control(self, request):
        self.check_origin(request)
        ws = web.WebSocketResponse(heartbeat=5, max_msg_size=4096, compress=False)
        await ws.prepare(request)
        await ws.send_json({'type': 'ready'})
        count, window = 0, time.monotonic()
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(message.data)
                    if not isinstance(data, dict):
                        raise ValueError('expected object')
                    kind = data.get('type')
                    if time.monotonic() - window >= 1:
                        count, window = 0, time.monotonic()
                    count += 1
                    if count > 100:
                        raise ValueError('too many input messages')
                    if kind == 'claim':
                        async with self.lease_lock:
                            if self.owner is not None and self.owner is not ws:
                                await ws.send_json({'type': 'busy', 'error': 'Another browser has control'})
                            elif self.hid.error:
                                await ws.send_json({'type': 'error', 'error': self.hid.error})
                            else:
                                self.owner = ws
                                self.last_heartbeat = time.monotonic()
                                await ws.send_json({'type': 'claimed'})
                    elif kind == 'ping':
                        if self.owner is ws:
                            self.last_heartbeat = time.monotonic()
                        await ws.send_json({'type': 'pong', 't': data.get('t')})
                    elif kind == 'release':
                        await self.release_owner(ws)
                        await ws.send_json({'type': 'released'})
                    elif kind in ('keyboard', 'mouse'):
                        # Hold lease through serial delivery so watchdog cannot
                        # release a key and then race with a stale queued press.
                        async with self.lease_lock:
                            if self.owner is not ws:
                                raise ValueError('claim control first')
                            await self.hid.send(data)
                        await ws.send_json({'type': 'ack', 'id': data.get('id')})
                    else:
                        raise ValueError('unknown message type')
                except Exception as exc:
                    await self.release_owner(ws)
                    await ws.send_json({'type': 'error', 'error': str(exc)})
        finally:
            await self.release_owner(ws)
        return ws

    async def watchdog(self):
        while True:
            await asyncio.sleep(1)
            if self.owner and time.monotonic() - self.last_heartbeat > 5:
                ws = self.owner
                await self.release_owner(ws)
                await ws.close(message=b'Control heartbeat expired')
            if not self.owner and self.hid.error:
                await self.hid.connect()

    async def lifecycle(self, app):
        await self.hid.connect()
        tasks = [asyncio.create_task(self.capture.run()), asyncio.create_task(self.watchdog())]
        yield
        if self.owner:
            await self.owner.close()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await self.hid.close()


def make_app(args):
    kvm = KVM(args)

    @web.middleware
    async def access(request, handler):
        if request.path.startswith('/api/') or request.path in ('/video', '/control'):
            if not kvm.authorized(request):
                raise web.HTTPUnauthorized(text='Sign in to the KVM')
        response = await handler(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    async def index(request):
        return web.FileResponse(ROOT / 'static' / 'index.html')

    app = web.Application(middlewares=[access], client_max_size=8192)
    app[KVM_KEY] = kvm
    app.router.add_get('/', index)
    app.router.add_post('/login', kvm.login)
    app.router.add_get('/api/status', kvm.status)
    app.router.add_get('/api/snapshot', kvm.snapshot)
    app.router.add_get('/video', kvm.video)
    app.router.add_get('/control', kvm.control)
    app.router.add_static('/static/', ROOT / 'static', show_index=False)
    app.cleanup_ctx.append(kvm.lifecycle)
    return app


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--serial')
    parser.add_argument('--baud', type=int, default=9600)
    parser.add_argument('--camera', default='HD33 video')
    parser.add_argument('--resolution', default='1280x720')
    parser.add_argument('--fps', type=int, default=30)
    parser.add_argument('--capture-fps', type=float, default=60,
                        help='AVFoundation native rate; HD33 720p supports 60, output defaults to 30')
    parser.add_argument('--quality', type=int, default=5)
    parser.add_argument('--ffmpeg')
    parser.add_argument('--capture-backend', choices=('native', 'ffmpeg'), default='native')
    args = parser.parse_args(argv)
    args.password = os.environ.get('KVM_PASSWORD')
    if not args.password:
        path = ROOT / '.access-code'
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(secrets.token_urlsafe(12) + '\n')
        args.password = path.read_text().strip()
    if len(args.password) < 8:
        parser.error('KVM_PASSWORD must contain at least 8 characters')
    return args


KVM_KEY = web.AppKey('kvm', KVM)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    args = arguments()
    web.run_app(make_app(args), host=args.host, port=args.port,
                access_log=None, shutdown_timeout=3)
