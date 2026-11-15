"""Step 1: speech-to-text. Audio file → raw transcript (temporary, see migrations/003_transcripts.sql)."""

from pathlib import Path

from psycopg.types.json import Jsonb

from app.db import tenant_tx
from app.pipeline import Finished, PermanentError, StepFn
from app.queue import Job
from app.stt import Transcriber


def make_transcribe_step(transcriber: Transcriber) -> StepFn:
    def transcribe(conn, job: Job) -> None:
        with tenant_tx(conn, job.tenant_id):
            rec = conn.execute(
                """
                SELECT r.audio_path, r.audio_sha256, r.language, r.duplicate_of,
                       EXISTS (SELECT 1 FROM raw_transcripts t
                               WHERE t.tenant_id = r.tenant_id AND t.recording_id = r.id)
                    OR EXISTS (SELECT 1 FROM transcripts t
                               WHERE t.tenant_id = r.tenant_id AND t.recording_id = r.id) AS done
                FROM recordings r WHERE r.id = %s
                """,
                [job.recording_id],
            ).fetchone()
        if rec["done"]:
            return  # a previous run stored the result before the worker died: don't pay for it twice
        if rec["duplicate_of"]:
            # The same audio was already uploaded (same sha256, see app/ingest.py). Its original is transcribed,
            # masked and sent to the CRM; this copy stays visible as a duplicate and costs nothing.
            raise Finished()

        audio = Path(rec["audio_path"])
        if not audio.is_file() or rec["audio_sha256"] is None:
            raise PermanentError("audio file not found")

        # The slow third-party call happens outside any transaction.
        result = transcriber.transcribe(audio, rec["audio_sha256"], rec["language"])

        with tenant_tx(conn, job.tenant_id):
            conn.execute(
                """
                INSERT INTO raw_transcripts (tenant_id, recording_id, provider, model, content)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, recording_id) DO NOTHING
                """,
                [job.tenant_id, job.recording_id, transcriber.provider, transcriber.model, Jsonb(result)],
            )

    return transcribe
