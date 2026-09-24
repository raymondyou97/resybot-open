"""Scan indexed files without printing any matched value. Standard library only."""

import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_NAMES = {
    'accounts.json',
    'tasks.json',
    'info.json',
    'proxies.json',
    'resrevations.json',
    'license_key.json',
    'access_key.json',
    'schedules.json',
    'reservation-settings.json',
    'booking-state.json',
    '.local-server-token',
    '.env',
    'am.txt',
    'pyvenv.cfg',
}
PATTERNS = [
    ('private key', re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
    ('GitHub credential', re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})')),
    ('AWS credential', re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b')),
    (
        'Discord webhook credential',
        re.compile(rb'https://discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{20,}'),
    ),
    ('JWT', re.compile(rb'\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b')),
]
SECRET_KEYS = {
    'auth_token',
    'capsolver_key',
    'capmonster_key',
    'discord_webhook',
    'resy_token',
    'password',
    'card_number',
    'payment_id',
}


def local_secrets():
    values = set()

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in SECRET_KEYS and isinstance(item, (str, int)) and len(str(item)) >= 8:
                    values.add(str(item).encode())
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for name in PRIVATE_NAMES:
        path = ROOT / 'client' / name
        if path.exists() and path.suffix == '.json':
            collect(json.loads(path.read_text()))
    token_file = ROOT / '.state/.local-server-token'
    if token_file.exists():
        values.add(token_file.read_bytes().strip())
    return {value for value in values if value}


def scan(path, content, values=()):
    findings = []
    parts = Path(path).parts
    if Path(path).name in PRIVATE_NAMES or any(
        part.startswith('.venv') or part in {'.state', '.logs'} for part in parts
    ):
        findings.append('private/runtime file')
    if path.startswith('server/bin/') or path.endswith(('.sqlite3', '.log')):
        findings.append('runtime artifact')
    if Path(path).name == 'reservations.txt':
        private_fields = '|'.join(sorted(SECRET_KEYS | {'account_name', 'account_id'})).encode()
        if re.search(rb'(?im)^\s*(?:' + private_fields + rb')\s*[:=]', content):
            findings.append('private setting in public reservation plan')
    if any(value in content for value in values):
        findings.append('matches a local credential')
    findings.extend(label for label, pattern in PATTERNS if pattern.search(content))
    return findings


def main():
    try:
        values = local_secrets()
        paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).split(b'\0')
        failed = False
        count = 0
        for encoded in paths:
            if not encoded:
                continue
            path = encoded.decode()
            content = subprocess.check_output(['git', 'show', f':{path}'], cwd=ROOT)
            count += 1
            for finding in scan(path, content, values):
                print(f'BLOCKED: {path}: {finding}')
                failed = True
        if failed:
            return 1
        print(f'Secret check passed for {count} indexed files; no matched values printed.')
        return 0
    except Exception:
        print('Secret check could not complete; refusing to pass. Inspect local configuration privately.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
