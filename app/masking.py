"""Personal data masking for Turkish call transcripts.

Input: the speech-to-text result, one transcript per audio channel (agent and caller on separate channels).
Output: a masked dialogue, `[{"speaker": "agent"|"caller"|"unknown", "start": seconds, "text": ...}]`.

What is masked, and how:
- E-mail addresses, IBANs, card numbers, phone numbers, national ID numbers: by pattern. Checksums (Luhn for
  cards, the TCKN algorithm for ID numbers) only choose the label: any run of 10+ digits is masked anyway,
  because speech-to-text can mishear a digit and break a checksum.
- People's names: learned from the call itself, then masked everywhere in that call. A name is learned from
  "adım X" / "ismim X" / "X adım", from the caller's answer right after the agent asks for their name, and from
  "X Bey" / "X Hanım". There is no dictionary of first names on purpose: "Deniz" is a common first name and
  also part of many company names.
- Company names are kept: the team reading the CRM needs to know which customer called. A company is a phrase
  ending in a sector word ("Aksoy Lojistik", "NOVA yazılım") or the caller's short answer to "what is your
  company's name?". Name masking skips those phrases, so "Kerem Can Aksoy, Aksoy Lojistik adına" becomes
  "[NAME], Aksoy Lojistik adına".
- The voice assistant's persona name (a tenant setting, e.g. "Asya") is not a person and is not masked.

When unsure whether a word is a company or a person, it is masked: losing a company name once is cheaper than
leaking a person's name.
"""

import re
from dataclasses import dataclass, field

UPPER = "A-ZÇĞİÖŞÜ"
WORD = r"[A-Za-zÇĞİÖŞÜçğıöşüâîû]+"
CAP_WORD = rf"[{UPPER}][A-Za-zÇĞİÖŞÜçğıöşüâîû]*"

# --- Patterns ---------------------------------------------------------------------------------------

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
IBAN = re.compile(r"\b[Tt][Rr]\s?\d{2}(?:\s?\d{4}){5}\s?\d{2}\b")
# Digits may be grouped with spaces, dashes or dots: speech-to-text writes "38.492.057.110" for a spoken ID.
CARD = re.compile(r"(?<!\d)\d(?:[ .-]?\d){12,18}(?!\d)")
PHONE = re.compile(
    r"(?<![\d+])(?:"
    r"(?:\+90[ -]?|0)?\(?5\d{2}\)?[ -]?\d{3}[ -]?\d{2}[ -]?\d{2}"     # mobile, with or without 0 / +90
    r"|(?:\+90[ -]?|0)\(?[2-4]\d{2}\)?[ -]?\d{3}[ -]?\d{2}[ -]?\d{2}"  # landline, needs 0 / +90
    r"|0?850[ -]?\d{3}[ -]?\d{2}[ -]?\d{2}|444[ -]?\d[ -]?\d{3}"      # 0850 and 444 numbers
    r")(?!\d)"
)
ID_NUMBER = re.compile(r"(?<!\d)[1-9](?:[ .-]?\d){10}(?!\d)")
LONG_NUMBER = re.compile(r"(?<!\d)\d(?:[ .-]?\d){9,}(?!\d)")  # anything with 10+ digits left over

SECTOR_WORDS = (
    r"Oto\s+Servis|Otomotiv|Lojistik|Yazılım|Emlak|Otel|Hastanesi|Hastane|Sigorta|Holding|Teknoloji|"
    r"Bilişim|Danışmanlık|İnşaat|Tekstil|Turizm|Telekom|Gıda|Ltd\.?|Şti\.?|A\.Ş\."
)
# One or two capitalised words followed by a sector word, plus an optional Turkish suffix ("Yazılım'da").
COMPANY = re.compile(rf"(?:{CAP_WORD}\s+){{1,2}}(?i:{SECTOR_WORDS})(?:'\w+)?")

NAME_INTRO = [
    re.compile(rf"(?i:\b(?:benim\s+)?(?:adım|ismim))\s+({CAP_WORD}(?:\s+{CAP_WORD}){{0,2}})"),  # adım Ozan Kılıç
    re.compile(rf"({CAP_WORD}(?:\s+{CAP_WORD}){{0,2}})\s+(?i:adım)\b"),                          # Ozan Kılıç adım
    re.compile(rf"\b(?i:ben)\s+({CAP_WORD}(?:\s+{CAP_WORD}){{0,2}})"),                            # ben Murat Demir
]
HONORIFIC = re.compile(rf"({CAP_WORD})\s+(?:Bey|Hanım)\b")                                       # Ozan Bey
ASKS_NAME = re.compile(r"(?i)\b(?:ad(?:ınızı|ınız)?\s*(?:ve\s*)?soyad|adınızı|isminizi|ad soyad)")
# Only the question itself: "şirketinizin adı/adını", "şirket adınız/adınızı". Not "şirket adına"
# ("on behalf of the company"), which once made a caller's name look like a company name.
ASKS_COMPANY = re.compile(r"(?i)şirketinizin\s+adı|şirket\s+adınız")

FILLERS = {"eee", "ee", "hmm", "ya", "şey", "yani", "evet", "tabii", "tabi", "ben", "benim"}
NOT_NAMES = {"bey", "hanım", "merhaba", "merhabalar", "teşekkürler", "teşekkür", "evet", "hayır", "tamam",
             "peki", "rica", "sayın", "brightsmile", "online", "bugün", "yarın"}

LABELS = {"email": "[EMAIL]", "iban": "[IBAN]", "card": "[CARD]", "phone": "[PHONE]",
          "id_number": "[ID_NUMBER]", "number": "[NUMBER]", "name": "[NAME]"}


# --- Helpers ----------------------------------------------------------------------------------------

def fold(word: str) -> str:
    """Case- and accent-insensitive key, Turkish-aware: 'NOVA' == 'Nova', 'Sönmez' == 'Sonmez', 'İ' == 'i'."""
    lowered = word.replace("I", "ı").replace("İ", "i").lower()
    return lowered.translate(str.maketrans("çğıöşüâîû", "cgiosuaiu"))


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def tckn_ok(digits: str) -> bool:
    d = [int(c) for c in digits]
    return (
        len(d) == 11 and d[0] != 0
        and ((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10 == d[9]
        and sum(d[:10]) % 10 == d[10]
    )


def mask_patterns(text: str) -> str:
    """Mask everything that has a recognisable shape. Order matters: e-mails can contain digits, IBANs and
    cards are longer than phone numbers."""
    text = EMAIL.sub(LABELS["email"], text)
    text = IBAN.sub(LABELS["iban"], text)
    text = CARD.sub(lambda m: LABELS["card"] if luhn_ok(re.sub(r"\D", "", m.group())) else m.group(), text)
    text = PHONE.sub(LABELS["phone"], text)
    text = ID_NUMBER.sub(lambda m: LABELS["id_number"] if tckn_ok(re.sub(r"\D", "", m.group())) else m.group(), text)
    text = LONG_NUMBER.sub(LABELS["number"], text)
    return text


def find_pattern_pii(text: str) -> list[str]:
    """Kinds of shaped personal data still present in `text`. Used as a last check before text leaves the
    system (e.g. a complaint summary sent to the CRM)."""
    checks = {"email": EMAIL, "iban": IBAN, "phone": PHONE, "number": LONG_NUMBER}
    return [kind for kind, pattern in checks.items() if pattern.search(text)]


# --- Dialogue ---------------------------------------------------------------------------------------

@dataclass
class Turn:
    speaker: str
    start: float
    text: str
    marked: str = ""   # the masked text with likely mishearings flagged, for the extractor only
    uncertain: list[str] = field(default_factory=list, repr=False)  # raw words; never stored


# Words speech-to-text was least sure about. On the real calls, logprob <= -0.3 is the least certain ~1% of
# words, and it includes every mishearing found by listening ("altın" for "yarın", "Ordu" for "doğru"). The threshold
# was chosen on the same 21 calls, so it is a starting point, not a tuned value.
LOW_CONFIDENCE_LOGPROB = -0.3
UNSURE = "⟨?⟩"


# A pause longer than this ends a speaker's stretch of speech. Measured on the real calls: 90% of the gaps
# between one speaker's words are under 0.5 s, while a pause for the other speaker is typically over 7 s.
SEGMENT_GAP_SECONDS = 1.0


def _segments(words: list[dict]) -> list[list[dict]]:
    """Split one channel's words into stretches of continuous speech."""
    segments: list[list[dict]] = []
    for w in words:
        if segments and w["start"] - segments[-1][-1]["end"] <= SEGMENT_GAP_SECONDS:
            segments[-1].append(w)
        else:
            segments.append([w])
    return segments


def build_turns(content: dict) -> list[Turn]:
    """Order the channels' stretches of speech by time into turns. Whole stretches are ordered, not single
    words: when both people talk at once, the agent's sentence and the caller's interjection each stay intact
    instead of being interleaved word by word.

    The agent's channel is the one that greets the caller ("... hoş geldiniz"); if none does, the one that
    speaks first (call centres answer first)."""
    channels = content.get("channels", [])
    if not channels:
        return []
    if len(channels) == 1:
        speakers = {channels[0]["index"]: "unknown"}
    else:
        greeting = [c["index"] for c in channels if "hoş geldiniz" in c.get("text", "").lower()]
        first = min(channels, key=lambda c: c["words"][0]["start"] if c["words"] else float("inf"))["index"]
        agent = greeting[0] if greeting else first
        speakers = {c["index"]: ("agent" if c["index"] == agent else "caller") for c in channels}

    def unsure(seg):
        return [w["text"] for w in seg if (w.get("logprob") is not None and w["logprob"] <= LOW_CONFIDENCE_LOGPROB)]

    stretches = sorted(
        (seg[0]["start"], speakers[c["index"]], " ".join(w["text"] for w in seg), unsure(seg))
        for c in channels for seg in _segments(c["words"])
    )
    turns: list[Turn] = []
    for start, speaker, text, uncertain in stretches:
        if turns and turns[-1].speaker == speaker:
            turns[-1].text += " " + text
            turns[-1].uncertain += uncertain
        else:
            turns.append(Turn(speaker, start, text, uncertain=list(uncertain)))
    return turns


def _flag_uncertain(masked: str, uncertain: list[str]) -> str:
    """Flag the words speech-to-text was unsure about in the *masked* text. Done after masking on purpose: a flag
    inside a phone number ("0532 123⟨?⟩ 45 67") would break its pattern and leak it. Only words that survived
    masking can be found, so a misheard name or number stays masked and is never written back."""
    pos = 0
    for word in uncertain:
        core = word.strip(".,?!;:")
        if not core:
            continue
        m = re.compile(rf"(?<!\w){re.escape(core)}(?!\w)").search(masked, pos)
        if m:
            masked = masked[:m.end()] + UNSURE + masked[m.end():]
            pos = m.end() + len(UNSURE)
    return masked


def _short_answer(text: str) -> list[str] | None:
    """The caller's answer as words, without fillers ("Eee") and cut-off words ("De--"), if it is short enough
    to be just a name or a company."""
    tokens = [t for t in re.findall(rf"{WORD}(?:--)?", text) if not t.endswith("--")]
    tokens = [t for t in tokens if fold(t) not in FILLERS]
    return tokens if 1 <= len(tokens) <= 4 else None


def _capitalised(tokens: list[str]) -> list[str]:
    return [t for t in tokens if re.match(rf"[{UPPER}]", t)]


def find_companies(turns: list[Turn]) -> set[str]:
    """Company phrases (folded) mentioned in the call."""
    companies = set()
    for i, turn in enumerate(turns):
        for m in COMPANY.finditer(turn.text):
            companies.add(fold(re.sub(r"'\w+$", "", m.group())))
        if turn.speaker == "agent" and ASKS_COMPANY.search(turn.text) and i + 1 < len(turns):
            answer = _short_answer(turns[i + 1].text)
            if answer:
                # All the words: "NOVA yazılım" must protect the phrase, never "NOVA" on its own.
                companies.add(fold(" ".join(answer)))
    return companies


# For reading the company back (CRM note), also "hangi şirket adına kayıt oluşturalım?" ("on behalf of which
# company?"). Masking can't use this wider form: there "şirket adına" in a statement once protected a name.
ASKS_COMPANY_FOR_NOTE = re.compile(rf"(?i){ASKS_COMPANY.pattern}|hangi\s+şirket\s+adına")


def caller_company(turns: list[dict]) -> str | None:
    """The company the caller named when the agent asked for it (the last answer, if asked twice), from the
    MASKED turns. Only an answer that is short, starts with a capital somewhere and has no mask label counts;
    anything else is None: a missing company is better than a wrong one in the CRM."""
    found = None
    for ask, answer in zip(turns, turns[1:]):
        if ask["speaker"] == "agent" and answer["speaker"] == "caller" and ASKS_COMPANY_FOR_NOTE.search(ask["text"]):
            tokens = _short_answer(answer["text"])
            if tokens and "[" not in answer["text"] and _capitalised(tokens):
                found = " ".join(tokens)
    return found


def find_names(turns: list[Turn], companies: set[str], assistant_name: str | None) -> set[str]:
    """Words (folded) that are people's names in this call. A name that is also part of a company name
    ("Aksoy" in "Aksoy Lojistik") is still a name; mask_names just leaves the company phrase alone."""
    found: set[str] = set()

    def add(phrase: str) -> None:
        for word in re.findall(WORD, phrase):
            key = fold(word)
            if len(key) > 1 and key not in NOT_NAMES:
                found.add(key)

    for i, turn in enumerate(turns):
        if turn.speaker in ("caller", "unknown"):
            for pattern in NAME_INTRO:
                for m in pattern.finditer(turn.text):
                    if fold(m.group(1)) not in companies:   # "Ben Türk Telekom'dan arıyorum"
                        add(m.group(1))
        for m in HONORIFIC.finditer(turn.text):
            add(m.group(1))
        if turn.speaker == "agent" and ASKS_NAME.search(turn.text) and i + 1 < len(turns):
            answer = _short_answer(turns[i + 1].text)
            if answer and fold(" ".join(answer)) not in companies:
                add(" ".join(_capitalised(answer)))

    if assistant_name:
        found.discard(fold(assistant_name))
    return found


def mask_names(text: str, names: set[str], companies: set[str]) -> str:
    """Replace every occurrence of a learned name, except inside company phrases."""
    protected = []
    for m in COMPANY.finditer(text):
        protected.append(m.span())
    for company in companies:
        for m in re.finditer(r"\b" + r"\s+".join(map(re.escape, company.split())) + r"\b", fold(text)):
            protected.append(m.span())  # fold() keeps length, so spans line up with the original text

    def replace(m: re.Match) -> str:
        if fold(m.group(1)) not in names or any(a <= m.start() < b for a, b in protected):
            return m.group()
        return LABELS["name"] + (m.group(2) or "")

    masked = re.sub(rf"({WORD})('\w+)?", replace, text)
    return re.sub(r"\[NAME\](?:\s+\[NAME\])+", LABELS["name"], masked)  # "[NAME] [NAME]" -> "[NAME]"


def mask_transcript(content: dict, assistant_name: str | None = None) -> list[Turn]:
    turns = build_turns(content)
    companies = find_companies(turns)
    names = find_names(turns, companies, assistant_name)
    masked = []
    for t in turns:
        text = mask_names(mask_patterns(t.text), names, companies)
        masked.append(Turn(t.speaker, t.start, text, marked=_flag_uncertain(text, t.uncertain)))
    return masked
