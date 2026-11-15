"""Extractors: the Claude adapter's error handling (no real API calls) and the rule-based fallback."""

from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.extraction import ClaudeExtractor, Complaint, Extraction, RuleExtractor
from app.pipeline import PermanentError, TransientError

OK = Extraction(meeting=None, complaint=Complaint(summary="Fatura itirazı."))


def claude_answering(result=None, error=None, stop_reason="end_turn"):
    def parse(**kwargs):
        if error:
            raise error
        return SimpleNamespace(stop_reason=stop_reason, parsed_output=result)
    return ClaudeExtractor("claude-haiku-5-5", client=SimpleNamespace(messages=SimpleNamespace(parse=parse)))


def api_error(cls, status, headers=None):
    response = httpx.Response(status, headers=headers, request=httpx.Request("POST", "https://api.anthropic.com"))
    return cls("error", response=response, body=None)


def test_returns_the_parsed_result():
    assert claude_answering(OK).extract("Caller: ...") == OK


@pytest.mark.parametrize("cls, status", [(anthropic.AuthenticationError, 401), (anthropic.PermissionDeniedError, 403)])
def test_rejected_key_fails_permanently(cls, status):
    with pytest.raises(PermanentError, match=f"HTTP {status}"):
        claude_answering(error=api_error(cls, status)).extract("x")


def test_rate_limit_retries_after_the_given_delay():
    with pytest.raises(TransientError) as exc:
        claude_answering(error=api_error(anthropic.RateLimitError, 429, {"retry-after": "12"})).extract("x")
    assert exc.value.retry_after == 12


@pytest.mark.parametrize("status", [500, 529])
def test_server_errors_and_overload_are_retried(status):
    with pytest.raises(TransientError):
        claude_answering(error=api_error(anthropic.APIStatusError, status)).extract("x")


def test_network_errors_are_retried():
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    with pytest.raises(TransientError):
        claude_answering(error=error).extract("x")


def test_refusal_fails_permanently_and_truncation_is_retried():
    with pytest.raises(PermanentError, match="refusal"):
        claude_answering(OK, stop_reason="refusal").extract("x")
    with pytest.raises(TransientError):
        claude_answering(None, stop_reason="max_tokens").extract("x")


# --- Rules -----------------------------------------------------------------------------------------

def rules(*turns):
    return RuleExtractor().extract("\n".join(f"{who}: {text}" for who, text in turns))


def test_rules_find_the_callers_last_requested_time():
    r = rules(("Agent", "Hangi gün ve saat uygun?"), ("Caller", "Yarın akşam saat sekizde olabilir."),
              ("Agent", "Akşam 8 toplantı saatlerimizin dışında."), ("Caller", "Akşam altı olsun o zaman. Yarın."),
              ("Agent", "Yarın saat 18'de online toplantınızı not ediyorum."))
    assert r.meeting.status == "requested"
    assert (r.meeting.day.kind, r.meeting.time.hour) == ("tomorrow", 18)


def test_rules_take_the_format_only_from_the_caller():
    asks = ("Agent", "Toplantı online mı yüz yüze mi olsun?")   # the agent offering both is not a choice
    assert rules(asks, ("Caller", "Toplantı yüz yüze olsun, ofisinize gelirim.")).meeting.format == "in_person"
    assert rules(asks, ("Caller", "Online olsun, toplantı yarın 10'da.")).meeting.format == "online"
    assert rules(asks, ("Caller", "Toplantı yarın 10'da olsun.")).meeting.format == "unknown"
    assert rules(asks, ("Caller", "Toplantı yarın 10'da olsun.")).meeting.topic == ""


def test_rules_count_a_complaint_the_agent_says_it_logged():
    # The caller never uses a problem word from the list, the agent logs a complaint
    r = rules(("Caller", "Siparişim hâlâ gelmedi, ücretimi geri istiyorum."),
              ("Agent", "Şikayetinizi kayda aldım, ilgili ekibimiz sizi arayacak."))
    assert r.complaint is not None and r.meeting is None


def test_rules_do_not_count_a_problem_the_agent_solved_on_the_call():
    r = rules(("Caller", "Ödeme yaparken hata alıyorum."),
              ("Agent", "Şu an bir sorun görünmüyor, bir saat sonra tekrar deneyebilirsiniz."))
    assert r.complaint is None


def test_rules_mark_a_withdrawn_request():
    r = rules(("Agent", "Toplantı talebinizi not ediyorum."), ("Caller", "Ya tamam, ben vazgeçtim."))
    assert r.meeting.status == "withdrawn"


def test_rules_treat_declining_a_meeting_as_no_request():
    r = rules(("Agent", "Toplantı ayarlayalım mı?"), ("Caller", "Şimdilik toplantıya gerek yok."))
    assert r.meeting is None


def test_rules_find_a_logged_complaint():
    r = rules(("Caller", "Faturayı yanlış kesmişsiniz, iade istiyorum."), ("Agent", "Talebinizi kayda alıyorum."))
    assert r.complaint.summary == "Kural tabanlı tespit (LLM yok): fatura itirazı, iade talebi."
    assert r.meeting is None


def test_rules_dont_read_the_time_of_a_complained_incident_as_a_meeting():
    # The agent notes when the incident happened, the caller never asks for a meeting.
    r = rules(("Caller", "Asistanınız müşterime kaba davranmış, sözleşmeyi iptal etmek istiyorum."),
              ("Agent", "Olayı bu sabah saat 10 civarında yapılan görüşme olarak not ediyorum."),
              ("Caller", "Vazgeçtim e-postadan, telefonla arayın."),
              ("Agent", "Şikayetinizi ilgili ekibe iletiyorum."))
    assert r.complaint is not None and r.meeting is None


def test_rules_still_find_a_meeting_the_caller_asks_for_in_a_complaint_call():
    r = rules(("Caller", "Fatura hatalı. Bir de yarın saat 14'te görüşme istiyorum."),
              ("Agent", "Fatura şikayetinizi kayda aldım, yarın 14 için görüşmenizi not ediyorum."))
    assert r.complaint is not None and r.meeting.time.hour == 14


def test_a_duration_is_not_read_as_a_time():
    r = rules(("Caller", "Toplantı istiyorum, sistem 24 saattir çalışmıyor."))
    assert r.meeting.time.kind == "none"


def test_rules_find_nothing_in_a_plain_information_call():
    r = rules(("Caller", "Sadece bilgi almak istiyorum."), ("Agent", "Tabii, yardımcı olayım."))
    assert r == Extraction(meeting=None, complaint=None)
