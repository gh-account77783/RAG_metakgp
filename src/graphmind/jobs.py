"""Single-writer durable job execution."""

from __future__ import annotations

import logging
import math
import socket
import threading
import uuid
from collections.abc import Callable

from .rag.components.contracts import Job, JobStatus
from .errors import (
    GraphMindError, JobExecutionError, JobLeaseError, JobNotFoundError, JobNotReadyError,
)
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

    def run_forever(self, stop: threading.Event, *, poll_seconds: float = 5.0) -> None:
        """Drain the existing queue; finish the current fenced job on shutdown."""
        if not math.isfinite(poll_seconds) or not 1 <= poll_seconds <= 60:
            raise ValueError("Worker polling must be between 1 and 60 seconds")
        while not stop.is_set():
            try:
                # A crashed predecessor's lease may expire after startup.
                # Reuse normal reconciliation instead of stranding its job.
                self.reconcile()
                if stop.is_set():
                    break
                completed = self.run_once()
            except Exception as exc:
                # Keep document bodies, paths and external error details out of
                # service logs. run_once records the durable job failure.
                logger.warning("Worker attempt failed (%s)",
                               exc.code if isinstance(exc, GraphMindError) else type(exc).__name__)
                completed = None
            if completed is None:
                stop.wait(poll_seconds)

    def run_job(self, job_id: str) -> Job:
        """Complete one explicit submission; never drain another job's work.

        Normal workers retain FIFO. Host commands may select their own job,
        using the very same lock, leases, handler and completion fencing.
        """
        current = self.metadata.job(job_id)
        if current is None:
            raise JobNotFoundError("The submitted job does not exist")
        if current.status is JobStatus.SUCCEEDED:
            return current
        with exclusive_file_lock(self.metadata.path.with_suffix(".writer.lock")) as acquired:
            if not acquired:
                raise JobLeaseError("Another ingestion process is still running")
            # Reconcile only while holding the kernel lock, as for normal
            # recovery. An unexpired lease must still be respected.
            self.metadata.reconcile()
            current = self.metadata.job(job_id)
            if current is None:
                raise JobNotFoundError("The submitted job does not exist")
            if current.status is JobStatus.SUCCEEDED:
                return current
            if current.status is JobStatus.RUNNING:
                raise JobLeaseError("The submitted job still has an active lease; retry later")
            if current.status is JobStatus.FAILED:
                self.metadata.retry_job(job_id)
            try:
                completed = self._run_once_locked(job_id=job_id)
            except GraphMindError:
                raise
            except Exception as exc:
                # The common worker records internal_error. Keep raw handler
                # details out of host command output, just as for typed errors.
                raise JobExecutionError("The submitted job failed; inspect its recorded status") from exc
            if completed is None:
                raise JobNotReadyError(
                    "The submitted job is delayed or has earlier work for its document; run the worker or retry later"
                )
            if completed.job_id != job_id or completed.status is not JobStatus.SUCCEEDED:
                raise JobExecutionError("The submitted job did not complete successfully")
            return completed

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

    def _run_once_locked(self, *, job_id: str | None = None) -> Job | None:
        if not self.metadata.acquire_writer(self.owner, self.lease_seconds):
            raise JobLeaseError("Another ingestion writer holds the active lease")
        try:
            job = self.metadata.claim_next_job(self.owner, self.lease_seconds, job_id=job_id)
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
            except Exception as exc:
                self._finish(job, succeeded=False, error_code="internal_error")
                logger.error("Unhandled ingestion job failure (%s)", type(exc).__name__,
                             extra={"job_id": job.job_id})
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
