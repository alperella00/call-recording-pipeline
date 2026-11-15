"""Flagging words speech-to-text was unsure about, without ever un-masking anything."""

from app.masking import UNSURE, mask_transcript
from app.stt import parse_elevenlabs


def content(*turns):
    """(speaker, [(word, logprob), ...]) pairs -> normalised STT result. Agent on channel 1, caller on 0."""
    words, t = {0: [], 1: []}, 0.0
    for speaker, ws in (("agent", [("BrightSmile'a", 0), ("hoş", 0), ("geldiniz.", 0)]), *turns):
        t += 10
        for i, (w, lp) in enumerate(ws):
            words[1 if speaker == "agent" else 0].append({"text": w, "start": t + i * 0.3, "end": t + i * 0.3 + 0.2,
                                                          "logprob": lp})
    return {"no_speech": False, "channels": [
        {"index": ch, "text": " ".join(w["text"] for w in ws), "words": ws} for ch, ws in words.items()]}


def caller_turn(c):
    return [t for t in mask_transcript(c, "Asya") if t.speaker == "caller"][0]


def test_low_confidence_words_are_flagged_only_in_the_extractors_copy():
    t = caller_turn(content(("caller", [("Yarın", 0), ("altın", -0.56), ("dört", -0.40), ("olur.", -0.01)])))
    assert t.text == "Yarın altın dört olur."
    assert t.marked == f"Yarın altın{UNSURE} dört{UNSURE} olur."


def test_a_misheard_name_stays_masked():
    t = caller_turn(content(("caller", [("Ozan", -0.9), ("Kılıç", -0.9), ("adım.", 0)])))
    assert t.text == "[NAME] adım." and t.marked == "[NAME] adım."


def test_an_unsure_digit_cannot_break_a_phone_number_out_of_its_mask():
    digits = [("0532", 0), ("123", -0.8), ("45", 0), ("67", 0)]
    t = caller_turn(content(("caller", [("numaram", 0), *digits])))
    assert t.text == "numaram [PHONE]" and t.marked == "numaram [PHONE]"


def test_parser_keeps_logprob():
    payload = {"text": "Doğru.", "words": [{"text": "Doğru.", "start": 0, "end": 0.4, "type": "word", "logprob": -0.3}]}
    assert parse_elevenlabs(payload)["channels"][0]["words"][0]["logprob"] == -0.3
