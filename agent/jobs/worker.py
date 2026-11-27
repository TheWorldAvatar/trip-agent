"""Run separately from Gunicorn: python -m agent.jobs.worker."""
from twa import agentlogging
import threading
import time
from uuid import uuid4

from agent.jobs.store import jobs

logger = agentlogging.get_logger('dev')


def execute_job(job, store=jobs, worker_id=None):
    from agent.trip.kg_client import KgClient
    from agent.trip.process_trajectory import process_trajectories

    payload = job['payload']
    def progress(stage):
        store.progress(job['id'], worker_id, stage)
        logger.info('Trip job %s stage: %s', job['id'], stage)
    kg_client = KgClient()
    if job['owner_id'] is not None:
        progress('resolving_owned_trajectories')
        point_iris = kg_client.get_point_iris(job['owner_id'])
        if not point_iris:
            raise ValueError('No trajectory points found for this user')
        participant_id = job['owner_id']
    else:
        point_iris = payload['point_iris']
        participant_id = point_iris[0]
    return process_trajectories(
        point_iris, payload.get('lowerbound'), payload.get('upperbound'), participant_id,
        kg_client=kg_client, update_timeline_table=job['owner_id'] is not None,
        progress=progress)


def run_job(job, worker_id, store=jobs, execute=execute_job):
    started = time.perf_counter()
    stopped = threading.Event()

    def keep_alive():
        while not stopped.wait(30):
            try:
                store.heartbeat(job['id'], worker_id)
            except Exception:
                logger.exception('Heartbeat failed for job %s', job['id'])

    heartbeat = threading.Thread(target=keep_alive, daemon=True)
    heartbeat.start()
    try:
        logger.info('Starting trip job %s', job['id'])
        result = execute(job, store, worker_id)
        store.finish(job['id'], worker_id, result=result)
        logger.info('Trip job %s completed successfully in %.2f seconds: %s',
                    job['id'], time.perf_counter() - started, result)
    except Exception:
        logger.exception('Trip job %s for user %s failed after %.2f seconds',
                         job['id'], job.get('owner_id'), time.perf_counter() - started)
        # Detailed database/endpoint errors stay in logs, not API responses.
        store.finish(job['id'], worker_id, error='Trip processing failed; see worker logs for job ' + job['id'])
    finally:
        stopped.set()
        heartbeat.join(timeout=2)


def main():
    worker_id = str(uuid4())
    jobs.initialize()
    logger.info('Background trip worker %s started', worker_id)
    while True:
        try:
            job = jobs.claim(worker_id)
            if job is None:
                time.sleep(2)
            else:
                run_job(job, worker_id)
        except Exception:
            logger.exception('Job queue operation failed')
            time.sleep(5)


if __name__ == '__main__':
    main()
