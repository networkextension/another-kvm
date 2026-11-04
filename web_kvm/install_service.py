#!/usr/bin/env python3
"""Install this checkout as a macOS user LaunchAgent; no sudo required."""
import argparse
from pathlib import Path
import os
import plistlib
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', type=int, default=8080)
parser.add_argument('--serial', help='default: auto-detect one CH340')
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
python = root / '.venv/bin/python'
helper = root / 'web_kvm/Camera.app/Contents/MacOS/Capture'
if not python.exists() or not helper.exists():
    raise SystemExit('First install requirements into .venv and run sh web_kvm/native/build.sh')
label = 'local.web-kvm'
job = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
job.parent.mkdir(parents=True, exist_ok=True)
program = [str(python), '-u', '-m', 'web_kvm.server', '--port', str(args.port)]
if args.serial:
    program += ['--serial', args.serial]
config = {'Label':label, 'ProgramArguments':program, 'WorkingDirectory':str(root),
          'RunAtLoad':True, 'KeepAlive':True, 'ThrottleInterval':10,
          'StandardOutPath':str(root / 'web_kvm/service.log'),
          'StandardErrorPath':str(root / 'web_kvm/service.log'),
          'EnvironmentVariables':{'PYTHONUNBUFFERED':'1'}}
job.write_bytes(plistlib.dumps(config))
domain = f'gui/{os.getuid()}'
subprocess.run(['launchctl', 'bootout', f'{domain}/{label}'], capture_output=True)
subprocess.run(['launchctl', 'bootstrap', domain, str(job)], check=True)
print(f'Installed {job}; KVM listens on port {args.port}')
