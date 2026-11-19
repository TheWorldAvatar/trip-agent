"""Small in-memory job store, shared with child processes via a local manager."""
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from multiprocessing.managers import BaseManager
import os
import threading
from uuid import uuid4


def now():
    return datetime.now(timezone.utc)


class JobStore:
    def __init__(self):
        self._jobs = {}
        self._pending = deque()
        self._lock = threading.RLock()

    def initialize(self):
        pass

    def enqueue(self, payload, owner_id=None):
        with self._lock:
            job_id = str(uuid4())
            self._jobs[job_id] = {
                'id': job_id, 'owner_id': owner_id, 'payload': deepcopy(payload),
                'status': 'queued', 'stage': 'queued', 'result': None, 'error': None,
                'worker_id': None, 'created_at': now(), 'started_at': None,
                'finished_at': None, 'heartbeat_at': None,
            }
            self._pending.append(job_id)
            return job_id

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None:
                return None
            return deepcopy({k: v for k, v in job.items() if k not in ('payload', 'worker_id')})

    def active_for_owner(self, owner_id):
        with self._lock:
            return deepcopy([
                {k: v for k, v in job.items() if k not in ('payload', 'worker_id')}
                for job in self._jobs.values()
                if owner_id is not None and job['owner_id'] == owner_id
                and job['status'] in ('queued', 'running')
            ])

    def claim(self, worker_id):
        with self._lock:
            if not self._pending:
                return None
            job = self._jobs[self._pending.popleft()]
            job.update(status='running', stage='starting', worker_id=str(worker_id),
                       started_at=now(), heartbeat_at=now())
            return deepcopy({k: job[k] for k in ('id', 'owner_id', 'payload')})

    def heartbeat(self, job_id, worker_id):
        self._change(job_id, worker_id, heartbeat_at=now())

    def progress(self, job_id, worker_id, stage):
        self._change(job_id, worker_id, stage=stage, heartbeat_at=now())

    def finish(self, job_id, worker_id, result=None, error=None):
        changes = dict(status='failed' if error else 'succeeded', result=result,
                       error=error, finished_at=now())
        if not error:
            changes['stage'] = 'completed'
        self._change(job_id, worker_id, **changes)

    def _change(self, job_id, worker_id, **changes):
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job and job['status'] == 'running' and job['worker_id'] == str(worker_id):
                job.update(changes)


_store = JobStore()


def shared_store():
    return _store


class TripQueueManager(BaseManager):
    pass


TripQueueManager.register('get_store', callable=shared_store)


def start_queue_server():
    """Owned by the launcher, so jobs survive an API worker restart only."""
    secret = os.urandom(32)
    manager = TripQueueManager(address=('127.0.0.1', 0), authkey=secret)
    server = manager.get_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return {'TRIP_QUEUE_PORT': str(server.address[1]), 'TRIP_QUEUE_AUTHKEY': secret.hex()}


class QueueClient:
    def __init__(self):
        self._proxy = None
        self._manager = None
        self._lock = threading.Lock()

    def _store(self):
        with self._lock:
            if self._proxy is None:
                if 'TRIP_QUEUE_PORT' not in os.environ or 'TRIP_QUEUE_AUTHKEY' not in os.environ:
                    raise RuntimeError('Start the API and calculation worker with python -m agent.run')
                manager = TripQueueManager(
                    address=('127.0.0.1', int(os.environ['TRIP_QUEUE_PORT'])),
                    authkey=bytes.fromhex(os.environ['TRIP_QUEUE_AUTHKEY']))
                manager.connect()
                self._proxy = manager.get_store()
                self._manager = manager
            return self._proxy

    def __getattr__(self, name):
        return getattr(self._store(), name)


jobs = QueueClient()
