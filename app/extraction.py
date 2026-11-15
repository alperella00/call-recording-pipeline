"""Read a masked call transcript and report the caller's meeting request and complaint.

The extractor only *reads*: it reports what the caller said ("tomorrow", "18:00"), never computes dates or
slots (app/timeparse.py and the CRM step do that). It only ever receives masked text.

Extractors:
- ClaudeExtractor: Claude Haiku via the official SDK with structured outputs (used when ANTHROPIC_API_KEY is set).
- RuleExtractor: keyword and pattern rules, so the pipeline still works with no LLM key at all.
"""

import os
import re
from typing import Literal, Protocol

import anthropic
from pydantic import BaseModel, Field

from app.masking import UNSURE
from app.pipeline import PermanentError, TransientError

PROMPT_VERSION = "v3"

Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class Day(BaseModel):
    kind: Literal["none", "today", "tomorrow", "day_after_tomorrow", "weekday", "date"]
    weekday: Weekday | None = Field(description="Only for kind=weekday.")
    next_week: bool = Field(description="True for 'haftaya X' / 'önümüzdeki hafta X'.")
    day_of_month: int | None = Field(description="Only for kind=date.")
    month: int | None = Field(description="Only for kind=date, 1-12.")


class Time(BaseModel):
    kind: Literal["none", "exact", "range", "part_of_day"]
    hour: int | None = Field(description="24-hour clock, for exact and range (start of the range).")
    minute: int | None
    end_hour: int | None = Field(description="Only for kind=range.")
    part_of_day: Literal["morning", "noon", "afternoon", "evening"] | None


MeetingFormat = Literal["online", "in_person", "unknown"]


class Meeting(BaseModel):
    status: Literal["requested", "withdrawn"]
    topic: str = Field(description="What the caller wants to talk about, one short sentence in Turkish; '' if unclear.")
    format: MeetingFormat
    day: Day
    time: Time
    quote: str = Field(description="The caller's own words giving the time, copied from the transcript; '' if none.")


class Complaint(BaseModel):
    summary: str = Field(description="One short sentence in Turkish.")


class Extraction(BaseModel):
    meeting: Meeting | None
    complaint: Complaint | None


SYSTEM_PROMPT = """You read the transcript of a Turkish phone call between a company's voice assistant ("Agent") and a \
customer ("Caller"). Personal data is already masked as [NAME], [EMAIL], [PHONE] and similar. Report two things.

The transcript was produced by automatic speech recognition from low-quality phone audio. Words can be \
misheard as other words that sound similar, so read for what the speaker most plausibly said, using the rest \
of the conversation (for example what the agent repeats back to confirm). Words followed by ⟨?⟩ are the ones \
the recogniser was least sure about: do not take them literally when the context points elsewhere.

meeting: did the CALLER ask to meet the company's team (a meeting, demo, appointment, "görüşme")? null if not.
- Report the caller's final request. Ignore times the caller later changed and times the agent said it cannot \
offer. The agent stating its general opening hours is not a request.
- A request counts even when the call ends before a day or time is agreed, if the caller has asked for an \
appointment or meeting in their own words ("randevu oluşturmak için", "toplantı ayarlayabilir miyiz"). Then day \
and time are none. The agent offering a meeting, and the caller not objecting, is not a request.
- status "withdrawn" if the caller withdrew the request or said it is not certain and only wants to be called back.
- day: report it as said, do not compute dates. "bugün" = today, "yarın" = tomorrow, "öbür gün" = \
day_after_tomorrow, a weekday name = weekday (next_week true for "haftaya" / "önümüzdeki hafta"), an explicit \
calendar date = date. none if no day was given.
- time: use the 24-hour clock. "akşam altı" = 18, "öğleden sonra üç" = 15, and in this business context \
"saat bir", "saat iki" ... "saat beş" without "sabah" are afternoon hours (13-17). "12-1 civarı" is a range \
(hour 12, end_hour 13). "sabah", "öğle", "öğleden sonra", "akşam" without a number are part_of_day \
(morning, noon, afternoon, evening). none if no time was given.
- A time that describes when a problem happened ("bu sabah saat 10'da oldu") is not a meeting request.
- topic: what the meeting is for, so the team can prepare: one short sentence in Turkish (at most 150 \
characters) with the caller's business and the product or need they want to discuss, e.g. "Lojistik firması; \
çalışanlar için kurumsal diş taraması". Include the company name if the caller \
gave one. Never include personal data or placeholders such as [NAME]. '' if the call does not say.
- format: "online" or "in_person" ("yüz yüze", at the office) if the caller said which they want, otherwise \
"unknown". The agent offering both is not a choice.

complaint: does the caller report a problem with a product or service they already use, an invoice, or an \
incident, and expect it to be handled? null if not. A prospect who is only asking about the company is not \
complaining, even when they criticise or get annoyed with the assistant they are talking to during that call: \
doubts, objections and frustration in a sales or information call are not complaints.
- summary: one short sentence in Turkish (at most 200 characters) saying what the problem is and what the \
caller wants. Include the company name if the caller gave one. Never include personal data or placeholders \
such as [NAME]."""


class Extractor(Protocol):
    provider: str
    model: str

    def extract(self, dialogue: str) -> Extraction: ...


class ClaudeExtractor:
    provider = "anthropic"

    def __init__(self, model: str, client: anthropic.Anthropic | None = None):
        self.model = model
        # Retries belong to the job queue (backoff, attempt limit, visible status), not to the SDK.
        self.client = client or anthropic.Anthropic(max_retries=0, timeout=60)

    def extract(self, dialogue: str) -> Extraction:
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": dialogue}],
                output_format=Extraction,
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise PermanentError(f"LLM rejected the API key (HTTP {exc.status_code})") from None
        except anthropic.RateLimitError as exc:
            retry_after = exc.response.headers.get("retry-after")
            raise TransientError("LLM rate limited (HTTP 429)",
                                 retry_after=float(retry_after) if retry_after else None) from None
        except anthropic.BadRequestError as exc:
            raise PermanentError(f"LLM refused the request (HTTP {exc.status_code})") from None
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientError(f"LLM unavailable (HTTP {exc.status_code})") from None
            raise PermanentError(f"LLM error (HTTP {exc.status_code})") from None
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise TransientError(f"LLM unreachable ({type(exc).__name__})") from None

        if response.stop_reason == "refusal":
            raise PermanentError("LLM declined to read this transcript (refusal)")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise TransientError(f"LLM returned no complete result (stop_reason={response.stop_reason})")
        return response.parsed_output


# --- Rule-based fallback ----------------------------------------------------------------------------

TR_WEEKDAYS = {"pazartesi": "monday", "salı": "tuesday", "çarşamba": "wednesday", "perşembe": "thursday",
               "cuma": "friday", "cumartesi": "saturday", "pazar": "sunday"}
HOUR_WORDS = {"bir": 1, "iki": 2, "üç": 3, "dört": 4, "beş": 5, "altı": 6, "yedi": 7, "sekiz": 8, "dokuz": 9,
              "on": 10, "on bir": 11, "on iki": 12}
MEETING_WORDS = re.compile(r"(?i)toplantı|demo|randevu|görüşme|tanışma")
MEETING_CONFIRMED = re.compile(r"(?i)(toplantı|görüşme)\w*\s.*(not ediyorum|not aldım|talebinizi aldım)|"
                               r"(not ediyorum|not aldım).*(toplantı|görüşme)")
ONLINE = re.compile(r"(?i)online|çevrim ?içi|görüntülü")
IN_PERSON = re.compile(r"(?i)yüz yüze|ofisinize|ofisinizde")
WITHDRAWN = re.compile(r"(?i)vazgeçtim|kesin değil")
DECLINED = re.compile(r"(?i)(toplantıya|görüşmeye)?\s*gerek yok|istemiyorum")
COMPLAINT_TOPICS = {
    "fatura": "fatura itirazı", "iade": "iade talebi", "çalışmıyor": "hizmet çalışmıyor",
    "hata": "üründe hata", "sorun": "sorun bildirimi", "iptal": "sözleşme iptali", "kaba": "kaba davranış",
    "ses yükselt": "kaba davranış", "şikayet": "şikayet",
}
COMPLAINT_LOGGED = re.compile(r"(?i)kayda al|ilgili ekib")
# The agent saying in so many words that it logged a complaint ("Şikayetinizi kayda aldım") is enough on its own.
AGENT_LOGGED_COMPLAINT = re.compile(r"(?i)şikayet\w*\s+(?:kayda|not)")


class RuleExtractor:
    provider = "rules"
    model = PROMPT_VERSION

    def extract(self, dialogue: str) -> Extraction:
        dialogue = dialogue.replace(UNSURE, "")  # the rules don't use the mishearing flags
        lines = [l.split(": ", 1) for l in dialogue.splitlines() if ": " in l]
        caller = " ".join(text for who, text in lines if who != "Agent").lower()
        agent = " ".join(text for who, text in lines if who == "Agent")
        complaint = self._complaint(caller, agent)
        return Extraction(meeting=self._meeting(lines, caller, agent, complaint is not None), complaint=complaint)

    def _meeting(self, lines, caller: str, agent: str, is_complaint: bool) -> Meeting | None:
        if not (MEETING_CONFIRMED.search(agent) or MEETING_WORDS.search(caller)):
            return None
        if is_complaint and not MEETING_WORDS.search(caller):
            # In a complaint, the agent noting "the conversation at 10 this morning" is about the incident, not a
            # meeting request. Only the caller asking for one counts.
            return None
        if DECLINED.search(caller) and not MEETING_CONFIRMED.search(agent):
            return None  # "şimdilik toplantıya gerek yok": a refusal, not a request to follow up
        status = "withdrawn" if WITHDRAWN.search(caller) else "requested"
        # The last caller turn that mentions a day or a time is taken as the final request.
        day, time_, quote = Day(kind="none", weekday=None, next_week=False, day_of_month=None, month=None), None, ""
        for who, text in lines:
            if who == "Agent":
                continue
            found_day, found_time = self._day(text.lower()), self._time(text.lower())
            if found_day:
                day = found_day
            if found_time:
                time_ = found_time
            if found_day or found_time:
                quote = text
        time_ = time_ or Time(kind="none", hour=None, minute=None, end_hour=None, part_of_day=None)
        return Meeting(status=status, topic="", format=self._format(caller), day=day, time=time_, quote=quote)

    @staticmethod
    def _format(caller: str) -> str:
        """Only what the caller asked for; the rules can't summarise a topic, so that stays ''."""
        online, in_person = ONLINE.search(caller), IN_PERSON.search(caller)
        if online and not in_person:
            return "online"
        if in_person and not online:
            return "in_person"
        return "unknown"

    @staticmethod
    def _day(text: str) -> Day | None:
        base = dict(weekday=None, next_week=False, day_of_month=None, month=None)
        if "öbür gün" in text:
            return Day(kind="day_after_tomorrow", **base)
        if "yarın" in text:
            return Day(kind="tomorrow", **base)
        if "bugün" in text:
            return Day(kind="today", **base)
        for word, weekday in sorted(TR_WEEKDAYS.items(), key=lambda kv: -len(kv[0])):  # cumartesi before cuma
            if re.search(rf"\b{word}", text):
                return Day(kind="weekday", **{**base, "weekday": weekday, "next_week": "haftaya" in text})
        return None

    @staticmethod
    def _time(text: str) -> Time | None:
        hour = None
        if m := re.search(r"\b(\d{1,2})(?:[.:](\d{2}))?(?:'?(?:de|da|te|ta))?\b", text):
            if re.search(r"saat|akşam|sabah|öğle", text):
                hour = int(m.group(1))
        if hour is None:
            for word, value in sorted(HOUR_WORDS.items(), key=lambda kv: -len(kv[0])):
                if re.search(rf"(saat|akşam|sabah)\s+{word}\b", text):
                    hour = value
                    break
        if hour is not None and not 0 <= hour <= 23:
            hour = None  # "24 saattir": a duration, not a time
        if hour is not None:
            if hour < 12 and ("akşam" in text or "öğleden sonra" in text or 1 <= hour <= 7):
                hour += 12
            return Time(kind="exact", hour=hour, minute=0, end_hour=None, part_of_day=None)
        for word, part in (("öğleden sonra", "afternoon"), ("sabah", "morning"), ("öğle", "noon"), ("akşam", "evening")):
            if word in text:
                return Time(kind="part_of_day", hour=None, minute=None, end_hour=None, part_of_day=part)
        return None

    @staticmethod
    def _complaint(caller: str, agent: str) -> Complaint | None:
        topics = []
        for word, topic in COMPLAINT_TOPICS.items():
            if word in caller and topic not in topics:
                topics.append(topic)
        if AGENT_LOGGED_COMPLAINT.search(agent):
            topics = topics or ["şikayet"]
        elif not topics or not COMPLAINT_LOGGED.search(agent):
            return None
        return Complaint(summary="Kural tabanlı tespit (LLM yok): " + ", ".join(topics) + ".")


def from_env() -> Extractor:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ClaudeExtractor(os.environ.get("LLM_MODEL", "claude-haiku-5-5"))
    return RuleExtractor()
