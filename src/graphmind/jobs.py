"""Single-writer durable job execution."""

from __future__ import annotations

import logging
import socket
import threading
import uuid
from collections.abc import Callable

from .domain import Job
from .errors import GraphMindError, JobLeaseError
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
        return self.metadata.reconcile()

    def run_once(self) -> Job | None:
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
                        if not self.metadata.heartbeat(job.job_id, self.owner, self.lease_seconds):
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
                self.metadata.finish_job(job.job_id, self.owner, succeeded=False, error_code=exc.code)
                raise
            except Exception:
                self.metadata.finish_job(
                    job.job_id, self.owner, succeeded=False, error_code="internal_error"
                )
                logger.exception("Unhandled ingestion job failure", extra={"job_id": job.job_id})
                raise
            finally:
                stop_heartbeat.set()
                heartbeat.join(timeout=max(1.0, self.lease_seconds / 3 + 1))
            if lease_lost.is_set():
                self.metadata.finish_job(
                    job.job_id,
                    self.owner,
                    succeeded=False,
                    error_code=JobLeaseError.code,
                )
                raise JobLeaseError("The ingestion writer lease was lost during processing")
            self.metadata.finish_job(job.job_id, self.owner, succeeded=True)
            return self.metadata.job(job.job_id)
        finally:
            self.metadata.release_writer(self.owner)
