"""Step 3: masked transcript → meeting request and complaint, stored for the CRM step.

The extraction is produced once and stored; a retry or re-run reuses it, so what is sent to the CRM never
changes between attempts. Times are resolved here, against the call's own time (app/timeparse.py).
"""

from psycopg.types.json import Jsonb

from app.db import tenant_tx
from app.extraction import PROMPT_VERSION, Extraction, Extractor
from app.masking import find_pattern_pii, mask_patterns
from app.pipeline import PermanentError, StepFn
from app.queue import Job
from app.steps.mask import SPEAKER_LABEL
from app.timeparse import Requested, resolve

NOTHING = Extraction(meeting=None, complaint=None)


def _clean_summary(summary: str) -> str:
    """Last check before text leaves the system: the summary was written from masked text, but if anything
    shaped like personal data appears, mask it."""
    summary = summary.strip()[:300]
    return mask_patterns(summary) if find_pattern_pii(summary) else summary


def make_extract_step(extractor: Extractor) -> StepFn:
    def extract(conn, job: Job) -> None:
        with tenant_tx(conn, job.tenant_id):
            row = conn.execute(
                """
                SELECT t.turns, t.no_speech, r.recorded_at, tn.timezone,
                       EXISTS (SELECT 1 FROM extractions e WHERE e.recording_id = r.id) AS done
                FROM recordings r
                JOIN transcripts t ON t.tenant_id = r.tenant_id AND t.recording_id = r.id
                JOIN tenants tn ON tn.id = r.tenant_id
                WHERE r.id = %s
                """,
                [job.recording_id],
            ).fetchone()
        if row is None:
            raise PermanentError("no masked transcript to read")
        if row["done"]:
            return

        if row["no_speech"]:
            provider, model, result = "none", "none", NOTHING
        else:
            # The extractor reads the masked dialogue with likely mishearings flagged (see app/masking.py).
            dialogue = "\n".join(f"{SPEAKER_LABEL[t['speaker']]}: {t.get('marked') or t['text']}" for t in row["turns"])
            # The slow third-party call happens outside any transaction.
            provider, model, result = extractor.provider, extractor.model, extractor.extract(dialogue)

        meeting = None
        if result.meeting is not None:
            try:
                requested = resolve(result.meeting.day.model_dump(), result.meeting.time.model_dump(),
                                    row["recorded_at"], row["timezone"])
                problem = None
            except (KeyError, TypeError, ValueError):
                requested, problem = Requested("none"), "the requested time could not be read"
            if result.meeting.status == "withdrawn":
                problem = "the caller withdrew the request or was unsure, and asked to be called back"
            meeting = (requested, problem, result.meeting)

        with tenant_tx(conn, job.tenant_id):
            conn.execute(
                """
                INSERT INTO extractions (tenant_id, recording_id, provider, model, prompt_version, result)
                VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                """,
                [job.tenant_id, job.recording_id, provider, model, PROMPT_VERSION, Jsonb(result.model_dump())],
            )
            if meeting:
                requested, problem, m = meeting
                conn.execute(
                    """
                    INSERT INTO meetings (tenant_id, recording_id, status, requested_kind, requested_start,
                                          requested_end, requested_quote, follow_up_reason, topic, format)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                    """,
                    [job.tenant_id, job.recording_id, "needs_follow_up" if problem else "pending",
                     requested.kind, requested.start, requested.end, m.quote, problem, _clean_summary(m.topic),
                     m.format],
                )
            if result.complaint is not None:
                conn.execute(
                    """
                    INSERT INTO complaints (tenant_id, recording_id, status, summary)
                    VALUES (%s, %s, 'pending', %s) ON CONFLICT DO NOTHING
                    """,
                    [job.tenant_id, job.recording_id, _clean_summary(result.complaint.summary)],
                )

    return extract
