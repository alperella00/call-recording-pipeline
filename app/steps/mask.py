"""Step 2: mask personal data. Raw transcript → masked transcript; the raw one is deleted in the same transaction."""

from psycopg.types.json import Jsonb

from app.db import tenant_tx
from app.masking import mask_transcript
from app.pipeline import PermanentError
from app.queue import Job

SPEAKER_LABEL = {"agent": "Agent", "caller": "Caller", "unknown": "Speaker"}


def mask(conn, job: Job) -> None:
    with tenant_tx(conn, job.tenant_id):
        raw = conn.execute(
            "SELECT provider, model, content FROM raw_transcripts WHERE recording_id = %s", [job.recording_id]
        ).fetchone()
        done = conn.execute(
            "SELECT 1 FROM transcripts WHERE recording_id = %s", [job.recording_id]
        ).fetchone()
        assistant = conn.execute("SELECT assistant_name FROM tenants").fetchone()["assistant_name"]

    if done:
        return
    if raw is None:
        raise PermanentError("no raw transcript to mask")

    turns = mask_transcript(raw["content"], assistant)
    text = "\n".join(f"{SPEAKER_LABEL[t.speaker]}: {t.text}" for t in turns)

    with tenant_tx(conn, job.tenant_id):
        conn.execute(
            """
            INSERT INTO transcripts (tenant_id, recording_id, provider, model, no_speech, turns, text)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, recording_id) DO NOTHING
            """,
            [job.tenant_id, job.recording_id, raw["provider"], raw["model"], raw["content"]["no_speech"],
             Jsonb([{"speaker": t.speaker, "start": t.start, "text": t.text, "marked": t.marked} for t in turns]),
             text],
        )
        conn.execute("DELETE FROM raw_transcripts WHERE recording_id = %s", [job.recording_id])
