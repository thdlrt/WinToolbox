"""Offline American-English pronunciations from the bundled, unmodified CMUdict.

ARPABET encodes lexical stress but not syllable boundaries or connected-speech
allophones. IPA here is a broad, conservative conversion, not a speech score or
an independently edited phonetic dictionary. No network or model is used.
"""
from __future__ import annotations

import functools
import re
import sys
import unicodedata
from pathlib import Path


SOURCE = "CMUdict · 美式"
LABEL = "美式音标（词典转换）"
NOTE = "来自词典的美式读音；音节边界未由原词典标注，重音位置为保守转换，不表示连读或语流中的全部变化。"
DICTIONARY = Path(__file__).with_name("data") / "cmudict" / "cmudict.dict"

VOWELS = {
    "AA": "ɑ", "AE": "æ", "AH": "ʌ", "AO": "ɔ", "AW": "aʊ",
    "AY": "aɪ", "EH": "ɛ", "ER": "ɝ", "EY": "eɪ", "IH": "ɪ",
    "IY": "iː", "OW": "oʊ", "OY": "ɔɪ", "UH": "ʊ", "UW": "uː",
}
CONSONANTS = {
    "B": "b", "CH": "tʃ", "D": "d", "DH": "ð", "F": "f", "G": "ɡ",
    "HH": "h", "JH": "dʒ", "K": "k", "L": "l", "M": "m", "N": "n",
    "NG": "ŋ", "P": "p", "R": "ɹ", "S": "s", "SH": "ʃ", "T": "t",
    "TH": "θ", "V": "v", "W": "w", "Y": "j", "Z": "z", "ZH": "ʒ",
}
# Legal/common English syllable onsets, used only to place stress marks. CMUdict
# itself contains no syllabification. Do not infer extra vowels or consonants.
ONSETS = {(phone,) for phone in CONSONANTS if phone != "NG"}
ONSETS.update(tuple(value.split()) for value in (
    "P L", "P R", "P Y", "B L", "B R", "B Y", "T R", "T W", "T Y",
    "D R", "D W", "D Y", "K L", "K R", "K W", "K Y", "G L", "G R",
    "G W", "G Y", "F L", "F R", "F Y", "V R", "V Y", "TH R", "TH W",
    "SH R", "SH W", "S L", "S M", "S N", "S P", "S T", "S K", "S W",
    "S F", "S Y", "HH Y", "HH W", "S P L", "S P R", "S P Y",
    "S T R", "S T Y", "S K L", "S K R", "S K W", "S K Y",
))
_VARIANT = re.compile(r"\(\d+\)$")
_WORD = re.compile(r"^'?[a-z0-9]+(?:['-][a-z0-9]+)*'?$", re.ASCII)


def normalize_word(word: str) -> str:
    """Normalize a single token while retaining meaningful apostrophes."""
    if not isinstance(word, str):
        return ""
    value = unicodedata.normalize("NFKC", word).strip().lower()
    value = value.translate(str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "–": "-", "‑": "-"}))
    # Strip surrounding punctuation, not internal punctuation or contractions.
    while value:
        before = value
        while value and not (value[0].isalnum() or value[0] == "'"):
            value = value[1:]
        while value and not (value[-1].isalnum() or value[-1] == "'"):
            value = value[:-1]
        if len(value) > 2 and value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        if value == before:
            break
    return value if len(value) <= 128 and _WORD.fullmatch(value) else ""


def arpabet_to_ipa(phones: tuple[str, ...] | list[str]) -> str:
    """Convert dictionary phonemes; reject unknown symbols instead of guessing."""
    bases, stress, sounds = [], [], []
    for phone in phones:
        match = re.fullmatch(r"([A-Z]+)([012]?)", phone)
        if not match:
            raise ValueError(f"Unknown ARPABET symbol: {phone}")
        base, digit = match.groups()
        if base in VOWELS:
            sound = "ə" if base == "AH" and digit == "0" else "ɚ" if base == "ER" and digit == "0" else VOWELS[base]
        elif base in CONSONANTS and not digit:
            sound = CONSONANTS[base]
        else:
            raise ValueError(f"Unknown ARPABET symbol: {phone}")
        bases.append(base)
        stress.append(digit)
        sounds.append(sound)
    vowels = [index for index, base in enumerate(bases) if base in VOWELS]
    marks = {}
    if len(vowels) > 1:
        for number, index in enumerate(vowels):
            if stress[index] not in ("1", "2"):
                continue
            if number == 0:
                onset = 0
            else:
                previous = vowels[number - 1]
                onset = index
                for candidate in range(max(previous + 1, index - 3), index):
                    if tuple(bases[candidate:index]) in ONSETS:
                        onset = candidate
                        break
            marks[onset] = "ˈ" if stress[index] == "1" else "ˌ"
    return "".join(marks.get(index, "") + sound for index, sound in enumerate(sounds))


@functools.lru_cache(maxsize=1)
def _entries() -> dict[str, tuple[tuple[str, ...], ...]]:
    """Load once on first lookup; importing the module does not parse 130k rows."""
    values = {}
    try:
        stream = DICTIONARY.open(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("离线发音词典缺失，请重新安装或更新工具箱") from exc
    with stream:
        for line in stream:
            line = line.partition("#")[0].strip()
            if not line or line.startswith(";;;"):
                continue
            fields = line.split()
            if len(fields) < 2:
                continue
            word = _VARIANT.sub("", fields[0]).lower()
            phones = tuple(sys.intern(phone) for phone in fields[1:])
            variants = values.setdefault(word, [])
            if phones not in variants:
                variants.append(phones)
    return {word: tuple(variants) for word, variants in values.items()}


def lookup(word: str) -> dict:
    """Return every distinct dictionary pronunciation; unknown words stay empty.

    IPA strings omit slash delimiters so callers can format them consistently.
    No pronunciation is inferred for a word missing from the bundled dictionary.
    """
    normalized = normalize_word(word)
    pronunciations = []
    for phones in _entries().get(normalized, ()) if normalized else ():
        ipa = arpabet_to_ipa(phones)
        if ipa and ipa not in pronunciations:
            pronunciations.append(ipa)
    return {"word": normalized, "ipa": pronunciations, "found": bool(pronunciations),
            "source": SOURCE, "label": LABEL, "note": NOTE}
