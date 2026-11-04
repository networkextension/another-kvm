"""Authenticated LAN smoke check; optional non-text Shift/mouse exercise."""
import argparse
import asyncio
import json
from pathlib import Path
import time
from aiohttp import ClientSession, CookieJar, WSMsgType


async def check(args):
    origin = args.url.rstrip('/')
    code = args.code_file.read_text().strip()
    result = {'url': origin, 'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
        async with client.get(origin + '/api/status') as response:
            assert response.status == 401, 'unauthenticated status must be denied'
        async with client.post(origin + '/login', json={'password':code}, headers={'Origin':origin}) as response:
            assert response.status == 200, 'login failed'
        for path in ('/', '/static/app.js', '/static/style.css'):
            async with client.get(origin + path) as response:
                assert response.status == 200, path
        async with client.get(origin + '/api/status') as response:
            result['status'] = await response.json()
            assert result['status']['hid']['online'], 'serial offline'
        async with client.ws_connect(origin + '/video', headers={'Origin':origin}) as ws:
            sizes = []
            start = time.monotonic()
            for _ in range(15):
                message = await asyncio.wait_for(ws.receive(), 10)
                assert message.type == WSMsgType.BINARY, 'expected JPEG binary'
                assert message.data[:2] == b'\xff\xd8' and message.data[-2:] == b'\xff\xd9', 'invalid JPEG'
                sizes.append(len(message.data))
            result['video_frames_received'] = len(sizes)
            result['video_seconds'] = round(time.monotonic() - start, 3)
            result['jpeg_bytes_average'] = sum(sizes) // len(sizes)
        async with client.get(origin + '/api/snapshot') as response:
            assert response.status == 200, 'snapshot unavailable'
            frame = await response.read()
            if args.snapshot:
                args.snapshot.write_bytes(frame)
        async with client.ws_connect(origin + '/control', headers={'Origin':origin}) as ws:
            assert (await ws.receive_json())['type'] == 'ready'
            await ws.send_json({'type':'claim'})
            assert (await ws.receive_json())['type'] == 'claimed', 'control unavailable'
            try:
                if args.exercise_input:
                    commands = [{'type':'keyboard','keys':[],'modifiers':2},
                                {'type':'keyboard','keys':[],'modifiers':0},
                                {'type':'mouse','mode':'relative','dx':2},
                                {'type':'mouse','mode':'relative','dx':-2}]
                    for i, command in enumerate(commands):
                        await ws.send_json({**command,'id':i})
                        reply = await asyncio.wait_for(ws.receive_json(), 3)
                        assert reply == {'type':'ack','id':i}, reply
                    result['input_ack_checks'] = 4
            finally:
                await ws.send_json({'type':'release'})
                assert (await ws.receive_json())['type'] == 'released'
        async with client.get(origin + '/api/status') as response:
            status = await response.json()
            assert not status['controller'], 'control lease not released'
    result['result'] = 'PASS'
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8080')
    parser.add_argument('--code-file', type=Path, default=Path('web_kvm/.access-code'))
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--exercise-input', action='store_true')
    asyncio.run(check(parser.parse_args()))
