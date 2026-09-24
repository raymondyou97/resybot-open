"""A private shared bearer token for the loopback client/server connection."""

import os
import secrets
from pathlib import Path


TOKEN_PATH = Path(__file__).resolve().parents[1] / '.state' / '.local-server-token'


def local_token(path=None):
    path = Path(path) if path else TOKEN_PATH
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.is_symlink():
        raise ValueError('Refusing a symlinked local server token.')
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        path.chmod(0o600)
    else:
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(48))
    token = path.read_text().strip()
    if len(token) < 32:
        raise ValueError('Local server token is invalid; restart setup after resolving it locally.')
    return token


def local_headers():
    return {'Authorization': f'Bearer {local_token()}'}
