"""Start the local bridge and interactive client without changing working directory."""

from pathlib import Path
import subprocess
import signal
import sys
import time
import urllib.request

from client.local_auth import local_token

ROOT = Path(__file__).resolve().parent


def terminate(process):
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        print('Process required forced shutdown; inspect persistent submission holds before retrying.')


def main():
    local_token()
    server = client = None
    try:
        server = subprocess.Popen([sys.executable, str(ROOT / 'server/server.py')], cwd=ROOT)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError('Local server failed to start; check whether port 8000 is already in use.')
            try:
                with urllib.request.urlopen('http://127.0.0.1:8000/', timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError('Local server did not become ready.')
        client = subprocess.Popen([sys.executable, str(ROOT / 'client/entry.py'), *sys.argv[1:]], cwd=ROOT)
        client.wait()
    except KeyboardInterrupt:
        print('Stopping local processes; unresolved submissions remain held.')
    finally:
        terminate(client)
        terminate(server)


if __name__ == '__main__':
    main()
