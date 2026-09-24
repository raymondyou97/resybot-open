"""Private, atomic JSON storage rooted independently of the current directory."""

import json
import os
from pathlib import Path
import tempfile


DATA_DIR = Path(os.environ.get('RESY_DATA_DIR', Path(__file__).resolve().parent))


def data_path(name):
    path = Path(name)
    return path if path.is_absolute() else DATA_DIR / path


def load_data(name, default):
    path = data_path(name)
    if not path.exists():
        return default
    if path.is_symlink():
        raise ValueError('Refusing to read symlinked configuration.')
    path.chmod(0o600)
    try:
        with path.open() as stream:
            return json.load(stream)
    except (ValueError, OSError):
        raise ValueError('Configuration could not be read. Check its JSON format locally.') from None


def save_data(name, value):
    path = data_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('Refusing to overwrite symlinked configuration.')
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.config-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
