"""Masking rules, on invented sentences."""

import pytest

from app import masking
from app.masking import build_turns, find_pattern_pii, luhn_ok, mask_patterns, mask_transcript, tckn_ok


def call(*turns, assistant="Asya"):
    """Build an STT result from (speaker, text) pairs: agent on channel 1, caller on channel 0.
    Like every real call, it opens with the agent's greeting (which is how the agent channel is recognised);
    the greeting is not part of the returned texts."""
    turns = (("agent", "BrightSmile'a hoş geldiniz."), *turns)
    words = {0: [], 1: []}
    for i, (speaker, text) in enumerate(turns):
        channel = 1 if speaker == "agent" else 0
        words[channel] += [{"text": w, "start": i + j / 100, "end": i + j / 100} for j, w in enumerate(text.split())]
    content = {"no_speech": False, "channels": [
        {"index": ch, "text": " ".join(w["text"] for w in ws), "words": ws} for ch, ws in words.items()
    ]}
    texts = [t.text for t in mask_transcript(content, assistant)]
    first = texts[0].removeprefix("BrightSmile'a hoş geldiniz.").strip()  # the greeting may merge with an agent turn
    return ([first] if first else []) + texts[1:]


# --- Shaped data -----------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("mailim tuna.avci@gmail.com olsun", "mailim [EMAIL] olsun"),
    ("numaram 0532 123 45 67", "numaram [PHONE]"),
    ("numaram +90 532 123 4567", "numaram [PHONE]"),
    ("numaram 05321234567", "numaram [PHONE]"),
    ("sabit hat 0212 555 12 34", "sabit hat [PHONE]"),
    ("kimlik numaram 10000000146", "kimlik numaram [ID_NUMBER]"),
    ("IBAN TR33 0006 1005 1978 6457 8413 26 hesabıma", "IBAN [IBAN] hesabıma"),
    ("kartım 4111 1111 1111 1111", "kartım [CARD]"),
    ("şu numara 12345678901", "şu numara [NUMBER]"),  # 11 digits, not a valid ID: masked anyway
    # speech-to-text groups a spoken ID with dots (seen on real speech-to-text output)
    ("Tabii, 38.492.057.110.", "Tabii, [ID_NUMBER]."),
    ("kartım 4539 1488 0343 646", "kartım [NUMBER]"),  # a digit lost by speech-to-text: no checksum, still masked
])
def test_shaped_personal_data_is_masked(text, expected):
    assert mask_patterns(text) == expected


@pytest.mark.parametrize("text", [
    "saat 14:30 olur mu", "saat 16.00'da", "20 Ekim'den 30 Ekim'e kadar", "ayda 10 bin dakika",
    "24 saattir çalışmıyor", "40'tan fazla şube", "yüzde 15 indirim", "1.250.000 lira", "saat 11.30'da",
])
def test_times_dates_and_amounts_are_left_alone(text):
    assert mask_patterns(text) == text


def test_checksums():
    assert tckn_ok("10000000146") and not tckn_ok("10000000147")
    assert luhn_ok("4111111111111111") and not luhn_ok("4111111111111112")


def test_leak_check_finds_what_is_left():
    assert "iban" in find_pattern_pii("İade TR33 0006 1005 1978 6457 8413 26 hesabına")
    assert find_pattern_pii("Fatura itirazı, [IBAN] hesabına iade isteniyor") == []


# --- Names -----------------------------------------------------------------------------------------

def test_name_learned_from_introduction_is_masked_everywhere():
    out = call(("caller", "Merhaba, Ozan Kılıç adım."), ("agent", "Teşekkürler Ozan Bey, Kılıç soyadıyla kaydettim."))
    assert out == ["Merhaba, [NAME] adım.", "Teşekkürler [NAME] Bey, [NAME] soyadıyla kaydettim."]


def test_ben_followed_by_a_name_is_an_introduction():
    # Only the first name was caught (from "Selim Bey"), the surname leaked
    out = call(("caller", "Merhaba, ben Murat Demir. Toplantı istiyorum."), ("agent", "Elbette Selim Bey."))
    assert out == ["Merhaba, ben [NAME]. Toplantı istiyorum.", "Elbette [NAME] Bey."]


def test_ben_followed_by_a_company_is_not_a_name():
    out = call(("caller", "Ben Türk Telekom'dan arıyorum."), ("agent", "Türk Telekom için not aldım."))
    assert out == ["Ben Türk Telekom'dan arıyorum.", "Türk Telekom için not aldım."]


def test_answer_to_the_name_question_is_a_name_even_with_fillers_and_cut_words():
    out = call(("agent", "Adınızı ve soyadınızı öğrenebilir miyim?"), ("caller", "Eee, Kerem Ak-- Kerem Aksoy."),
               ("agent", "Talebinizi Kerem Aksoy'den aldım."))
    assert "Kerem" not in " ".join(out) and "Aksoy" not in " ".join(out)
    assert out[2] == "Talebinizi [NAME]'den aldım."


def test_on_behalf_of_the_company_is_not_a_question_about_the_company_name():
    # "şirket adına" (on behalf of the company) was read as "what is your company's name",
    # so the caller's name in the next turn was protected as a company and leaked.
    out = call(("agent", "Talebi şirket adına kaydedebilirim. Adınızı ve soyadınızı paylaşır mısınız?"),
               ("caller", "Kerem Ak-- Kerem Aksoy."), ("agent", "Talebinizi Aksoy Otomotiv adına Kerem Aksoy'den aldım."))
    assert out[1] == "[NAME] Ak-- [NAME]."
    assert out[2] == "Talebinizi Aksoy Otomotiv adına [NAME]'den aldım."


def test_company_containing_the_callers_surname_is_kept():
    out = call(("agent", "Ad soyadınızı öğrenebilir miyim?"), ("caller", "Kerem Can Aksoy."),
               ("agent", "Kerem Can Aksoy, Aksoy Lojistik adına talebinizi aldım."))
    assert out[2] == "[NAME], Aksoy Lojistik adına talebinizi aldım."


def test_company_starting_with_the_callers_first_name_is_kept_even_in_capitals():
    out = call(("agent", "Şirketinizin adı nedir?"), ("caller", "NOVA yazılım."),
               ("agent", "Nova Bey, şirket adınızı tam alabilir miyim?"), ("caller", "NOVA yazılım."),
               ("agent", "Nova Yazılım'da sorun yaşadığınızı not ettim."))
    assert out[1] == "NOVA yazılım."
    assert out[2].startswith("[NAME] Bey")
    assert out[4] == "Nova Yazılım'da sorun yaşadığınızı not ettim."


def test_spelling_variants_from_speech_to_text_are_the_same_name():
    out = call(("caller", "İsmim NOVA Sönmez."), ("agent", "Teşekkürler, Nova Sonmez olarak kaydettim."))
    assert out == ["İsmim [NAME].", "Teşekkürler, [NAME] olarak kaydettim."]


def test_a_company_word_that_is_also_a_first_name_is_not_masked_without_a_cue():
    # "Deniz" is a first name too; nobody says it is their name here.
    out = call(("caller", "Deniz Emlak'tan arıyorum, Ozan Kılıç adım."))
    assert out == ["Deniz Emlak'tan arıyorum, [NAME] adım."]


def test_the_assistants_persona_name_is_not_masked():
    out = call(("agent", "Ben Asya."), ("caller", "Merhaba Asya Hanım."))
    assert out == ["Ben Asya.", "Merhaba Asya Hanım."]


def test_email_is_masked_before_names_so_no_part_of_it_survives():
    out = call(("caller", "Ozan Kılıç adım, mailim tuna.avci@gmail.com"))
    assert out == ["[NAME] adım, mailim [EMAIL]"]


# --- Dialogue --------------------------------------------------------------------------------------

def test_turns_follow_time_and_the_greeting_marks_the_agent():
    content = {"no_speech": False, "channels": [
        {"index": 0, "text": "Merhaba.", "words": [{"text": "Merhaba.", "start": 2.0, "end": 2.5}]},
        {"index": 1, "text": "BrightSmile'a hoş geldiniz. Buyrun.",
         "words": [{"text": "BrightSmile'a", "start": 0.0, "end": 0.4}, {"text": "hoş", "start": 0.5, "end": 0.6},
                   {"text": "geldiniz.", "start": 0.7, "end": 1.0}, {"text": "Buyrun.", "start": 3.0, "end": 3.2}]},
    ]}
    assert [(t.speaker, t.text) for t in build_turns(content)] == [
        ("agent", "BrightSmile'a hoş geldiniz."), ("caller", "Merhaba."), ("agent", "Buyrun.")]


def test_talking_over_each_other_keeps_both_sentences_whole():
    # The caller interrupted mid-sentence and the words were interleaved one by one.
    def words(text, start, step=0.3):
        return [{"text": w, "start": start + i * step, "end": start + i * step + 0.2} for i, w in enumerate(text.split())]
    agent = words("BrightSmile'a hoş geldiniz.", 0) + words("İsterseniz tedavi seçeneklerini doktorumuz anlatsın mı?", 10)
    caller = words("Ben sadece fiyat soracaktım.", 10.15)
    content = {"no_speech": False, "channels": [
        {"index": 1, "text": "BrightSmile'a hoş geldiniz. ...", "words": agent},
        {"index": 0, "text": "...", "words": caller},
    ]}
    assert [(t.speaker, t.text) for t in build_turns(content)] == [
        ("agent", "BrightSmile'a hoş geldiniz. İsterseniz tedavi seçeneklerini doktorumuz anlatsın mı?"),
        ("caller", "Ben sadece fiyat soracaktım."),
    ]


def test_no_speech_gives_no_turns():
    assert build_turns({"no_speech": True, "channels": []}) == []


# --- The caller's company, for the CRM note (read from the masked turns) ----------------------------------

def _t(*pairs):
    return [{"speaker": s, "text": t} for s, t in pairs]


@pytest.mark.parametrize("turns, company", [
    # Asked once, answered with a filler
    (_t(("agent", "Şirketinizin adını öğrenebilir miyim?"), ("caller", "Eee, Deniz Otomotiv.")), "Deniz Otomotiv"),
    # Asked twice, the second answer is the clean one
    (_t(("agent", "Şirketinizin adı nedir?"), ("caller", "Ha bir de NOVA yazılım."),
        ("agent", "[NAME] Bey, şirket adınızı tam olarak alabilir miyim?"), ("caller", "NOVA yazılım.")),
     "NOVA yazılım"),
    # "On behalf of which company?"
    (_t(("agent", "[NAME] Bey, toplantı için hangi şirket adına kayıt oluşturalım?"), ("caller", "Deniz Yazılım.")),
     "Deniz Yazılım"),
    # Never asked, a long answer, or an answer that got masked: no company rather than a wrong one
    (_t(("caller", "Biz bir kafeyiz."), ("agent", "Anladım.")), None),
    (_t(("agent", "Şirketinizin adı nedir?"), ("caller", "Şöyle anlatayım, biz aslında küçük bir aile işletmesiyiz.")),
     None),
    (_t(("agent", "Şirket adınız nedir?"), ("caller", "[NAME].")), None),
])
def test_caller_company(turns, company):
    assert masking.caller_company(turns) == company
