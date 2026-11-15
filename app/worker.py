"""Worker process: claim a job, run it, release it, repeat.

Run several in parallel: `docker compose up --scale worker=3`.
On SIGTERM (`docker compose stop`) it finishes the step it is on and exits. On SIGKILL it just dies;
its lock is released with its connection and another worker continues the job (see app/queue.py).

Usage: python -m app.worker
"""

import os
import signal
import socket
import time
import uuid

from app import pipeline, queue
from app.db import connect

POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "1"))


class Worker:
    def __init__(self, steps: dict[str, pipeline.StepFn], worker_id: str | None = None):
        self.steps = steps
        # In a container the pid is always 1 and a restarted container keeps its hostname: add a random part
        # so two worker lives never share an id.
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.stopping = False

    def run_once(self, conn) -> str | None:
        """Process one job if one is due. Returns its final status, or None if there was nothing to do."""
        job = queue.claim(conn, self.worker_id)
        if job is None:
            return None
        try:
            status = pipeline.run(conn, job, self.steps)
            print(f"{self.worker_id} {job.tenant_id}/{job.recording_id}: {status}", flush=True)
            return status
        finally:
            queue.release(conn, job.tenant_id, job.recording_id)

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))
        print(f"{self.worker_id} started", flush=True)
        with connect("worker") as conn:
            while not self.stopping:
                if self.run_once(conn) is None:
                    time.sleep(POLL_SECONDS)
        print(f"{self.worker_id} stopped", flush=True)


def main() -> None:
    from app.steps import build_steps

    Worker(build_steps()).run_forever()


if __name__ == "__main__":
    main()
