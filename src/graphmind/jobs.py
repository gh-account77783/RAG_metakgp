"""Single-writer durable job execution."""

from __future__ import annotations

import logging
import socket
import threading
import uuid
from collections.abc import Callable

from .domain import Job
from .errors import GraphMindError, JobLeaseError
from .locking import exclusive_file_lock
from .metadata import MetadataStore


logger = logging.getLogger(__name__)


class DurableJobExecutor:
    def __init__(
        self,
        metadata: MetadataStore,
        handler: Callable[[Job], None],
        *,
        lease_seconds: int,
        owner: str | None = None,
    ) -> None:
        self.metadata = metadata
        self.handler = handler
        self.lease_seconds = lease_seconds
        self.owner = owner or f"{socket.gethostname()}:{uuid.uuid4()}"

    def reconcile(self) -> int:
        with exclusive_file_lock(self.metadata.path.with_suffix(".writer.lock")) as acquired:
            if not acquired:
                raise JobLeaseError("Another ingestion process is still running")
            return self.metadata.reconcile()

    def run_once(self) -> Job | None:
        # A lease expiring cannot safely interrupt an in-flight Chroma/Neo4j
        # write. Keep a kernel lock until it finishes, even after lease loss.
        with exclusive_file_lock(self.metadata.path.with_suffix(".writer.lock")) as acquired:
            if not acquired:
                raise JobLeaseError("Another ingestion process is still running")
            return self._run_once_locked()

    def _finish(self, job: Job, *, succeeded: bool, error_code: str | None = None) -> None:
        try:
            self.metadata.finish_job(
                job.job_id, self.owner, succeeded=succeeded, error_code=error_code, job=job
            )
        except JobLeaseError:
            if succeeded:
                raise
            # Leave the expired attempt for reconciliation; never overwrite a
            # successor's state, even if it reused this owner identifier.

    def _run_once_locked(self) -> Job | None:
        if not self.metadata.acquire_writer(self.owner, self.lease_seconds):
            raise JobLeaseError("Another ingestion writer holds the active lease")
        try:
            job = self.metadata.claim_next_job(self.owner, self.lease_seconds)
            if job is None:
                return None
            stop_heartbeat = threading.Event()
            lease_lost = threading.Event()

            def maintain_lease() -> None:
                interval = max(1.0, self.lease_seconds / 3)
                while not stop_heartbeat.wait(interval):
                    try:
                        if not self.metadata.heartbeat(job.job_id, self.owner, self.lease_seconds, job=job):
                            lease_lost.set()
                            return
                    except Exception:
                        logger.exception("Ingestion lease heartbeat failed", extra={"job_id": job.job_id})
                        lease_lost.set()
                        return

            heartbeat = threading.Thread(
                target=maintain_lease,
                name=f"graphmind-job-heartbeat-{job.job_id}",
                daemon=True,
            )
            heartbeat.start()
            try:
                self.handler(job)
            except GraphMindError as exc:
                self._finish(job, succeeded=False, error_code=exc.code)
                raise
            except Exception:
                self._finish(job, succeeded=False, error_code="internal_error")
                logger.exception("Unhandled ingestion job failure", extra={"job_id": job.job_id})
                raise
            finally:
                stop_heartbeat.set()
                heartbeat.join(timeout=max(1.0, self.lease_seconds / 3 + 1))
            if lease_lost.is_set():
                self._finish(job, succeeded=False, error_code=JobLeaseError.code)
                raise JobLeaseError("The ingestion writer lease was lost during processing")
            self._finish(job, succeeded=True)
            return self.metadata.job(job.job_id)
        finally:
            self.metadata.release_writer(self.owner)
