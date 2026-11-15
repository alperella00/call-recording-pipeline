"""The pipeline steps, in the order of app.pipeline.STEPS.

Each step is `step(conn, job)`: it reads its input from what the previous step stored, does its work, and
stores its output. Steps must be idempotent (see app/pipeline.py).
"""

from app import extraction, stt
from app.pipeline import StepFn
from app.steps.crm import make_crm_step
from app.steps.extract import make_extract_step
from app.steps.mask import mask
from app.steps.transcribe import make_transcribe_step


def build_steps() -> dict[str, StepFn]:
    return {
        "transcribe": make_transcribe_step(stt.from_env()),
        "mask": mask,
        "extract": make_extract_step(extraction.from_env()),
        "crm": make_crm_step(),
    }
