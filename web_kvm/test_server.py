import asyncio
from types import SimpleNamespace
import unittest
from aiohttp import CookieJar, WSMsgType
from aiohttp.test_utils import TestClient, TestServer
from web_kvm.server import KVM_KEY, jpeg_dimensions, make_app, validate_input


class FakeHID:
    error = None
    def __init__(self):
        self.sent = []
        self.released = asyncio.Event()

    async def send(self, data):
        validate_input(data)
        self.sent.append(data)

    async def release(self):
        self.released.set()

    def status(self):
        return {'online': True}


class InputTests(unittest.TestCase):
    def test_actual_jpeg_dimensions(self):
        raw = bytes.fromhex('ffd8 ffe0 0004 0000 ffc0 000b 08 0438 0780 01 01 11 00 ffd9')
        self.assertEqual(jpeg_dimensions(raw), (1920, 1080))
        self.assertIsNone(jpeg_dimensions(b'\xff\xd8\xff\xda\x00\x02'))

    def test_absolute_and_relative_frames(self):
        command, data = validate_input({'type':'mouse','x':320,'y':533,'buttons':1})
        self.assertEqual((command, data), (4, bytes.fromhex('02 01 40 01 15 02 00')))
        self.assertEqual(validate_input({'type':'mouse','mode':'relative','dx':-8})[1],
                         bytes.fromhex('01 00 f8 00 00'))

    def test_reject_invalid_input(self):
        for data in ({'type':'keyboard','keys':[4]*7},
                     {'type':'keyboard','keys':[True]},
                     {'type':'mouse','x':4096,'y':0},
                     {'type':'mouse','x':0,'y':0,'buttons':8},
                     {'type':'mouse','mode':'invalid'},
                     {'type':'reset'}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                validate_input(data)


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        args = SimpleNamespace(serial=None, baud=9600, camera='HD33 video', resolution='1280x720',
                               fps=30, quality=5, ffmpeg=None, password='unit-test-code')
        self.app = make_app(args)
        self.app.cleanup_ctx.clear()  # Hardware is replaced for transport/lease tests.
        self.kvm = self.app[KVM_KEY]
        self.hid = self.kvm.hid = FakeHID()
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.origin = str(self.client.make_url('/').origin())

    async def asyncTearDown(self):
        await self.client.close()

    async def login(self):
        response = await self.client.post('/login', json={'password':'unit-test-code'},
                                          headers={'Origin':self.origin})
        self.assertEqual(response.status, 200)

    async def ws(self):
        ws = await self.client.ws_connect('/control', headers={'Origin':self.origin})
        self.assertEqual((await ws.receive_json())['type'], 'ready')
        return ws

    async def test_authentication_and_origin(self):
        self.assertEqual((await self.client.get('/api/status')).status, 401)
        response = await self.client.post('/login', json={'password':'unit-test-code'},
                                          headers={'Origin':'http://malicious.invalid'})
        self.assertEqual(response.status, 403)
        await self.login()
        self.assertEqual((await self.client.get('/api/status')).status, 200)
        self.assertEqual((await self.client.get('/api/snapshot')).status, 503)

    async def test_controller_exclusion_and_disconnect_release(self):
        await self.login()
        first, second = await self.ws(), await self.ws()
        await first.send_json({'type':'claim'})
        self.assertEqual((await first.receive_json())['type'], 'claimed')
        await second.send_json({'type':'claim'})
        self.assertEqual((await second.receive_json())['type'], 'busy')
        await first.send_json({'type':'keyboard','keys':[],'modifiers':2,'id':42})
        self.assertEqual(await first.receive_json(), {'type':'ack','id':42})
        await first.close()
        await asyncio.wait_for(self.hid.released.wait(), 1)
        self.assertIsNone(self.kvm.owner)
        await second.send_json({'type':'claim'})
        self.assertEqual((await second.receive_json())['type'], 'claimed')
        await second.close()

    async def test_unclaimed_and_invalid_events_do_not_reach_serial(self):
        await self.login()
        ws = await self.ws()
        await ws.send_json({'type':'keyboard','keys':[4]})
        self.assertEqual((await ws.receive_json())['type'], 'error')
        self.assertEqual(self.hid.sent, [])
        await ws.send_json({'type':'claim'})
        await ws.receive_json()
        await ws.send_json({'type':'mouse','x':99999,'y':0})
        self.assertEqual((await ws.receive_json())['type'], 'error')
        self.assertIsNone(self.kvm.owner)
        self.assertTrue(self.hid.released.is_set())
        self.assertEqual(self.hid.sent, [])
        await ws.close()

    async def test_tampered_session_rejected(self):
        self.client.session.cookie_jar.update_cookies({'kvm_session': self.kvm.session()+'x'})
        self.assertEqual((await self.client.get('/api/status')).status, 401)


if __name__ == '__main__':
    unittest.main()
