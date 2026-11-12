"""Start the HTTP server and one independent calculation worker in production."""
import os
import signal
import subprocess
import sys
import time
from agent.jobs.store import start_queue_server


def main():
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    environment = os.environ.copy()
    environment.update(start_queue_server())
    children = []
    result = 0
    try:
        children.append(subprocess.Popen([sys.executable, '-m', 'agent.jobs.worker'], start_new_session=True, env=environment))
        children.append(subprocess.Popen([
            'gunicorn', '--bind', '0.0.0.0:5000', '--timeout', '600', 'agent.app:app'
        ], start_new_session=True, env=environment))
        while not stopping:
            if any(child.poll() is not None for child in children):
                result = 1  # Let the container orchestrator restart the service.
                break
            time.sleep(0.5)
    finally:
        # Kill process groups so Java gateway children do not survive shutdown.
        for child in children:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 8
        for child in children:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
    return result


if __name__ == '__main__':
    sys.exit(main())
