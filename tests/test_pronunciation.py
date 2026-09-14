"""Offline CMUdict integrity, token lookup and conservative IPA conversion."""
import hashlib
import json
import socket

import pytest

from toolbox import pronunciation


@pytest.mark.parametrize("token, expected", [
    ("HELLO!", "hello"), ("(Hello),", "hello"), ("‘Hello,’", "hello"),
    ("DON’T...", "don't"), ("\"Don't!\"", "don't"), ("'bout", "'bout"),
    ("  computer\n", "computer"), ("state-of-the-art", "state-of-the-art"),
])
def test_token_normalization_preserves_contractions(token, expected):
    assert pronunciation.normalize_word(token) == expected
    assert pronunciation.lookup(token)["word"] == expected


def test_known_word_and_multiple_dictionary_readings():
    assert pronunciation.lookup("computer")["ipa"] == ["kəmˈpjuːtɚ"]
    assert pronunciation.lookup("hello")["ipa"] == ["həˈloʊ", "hɛˈloʊ"]
    assert pronunciation.lookup("read")["ipa"] == ["ɹɛd", "ɹiːd"]
    assert pronunciation.lookup("don't")["ipa"] == ["doʊnt", "doʊn"]
    result = pronunciation.lookup("record")
    assert len(result["ipa"]) == 3 and len(set(result["ipa"])) == 3
    assert result["found"] and result["source"] == "CMUdict · 美式"
    assert result["label"] == "美式音标（词典转换）" and "重音" in result["note"]


@pytest.mark.parametrize("word", ["wintoolboxzzzzinvented", "hello world", "", None, "/../../", "x" * 129])
def test_unknown_or_invalid_input_never_gets_an_invented_pronunciation(word):
    result = pronunciation.lookup(word)
    assert result["ipa"] == [] and result["found"] is False


def test_stress_reduction_and_unknown_arpabet():
    convert = pronunciation.arpabet_to_ipa
    assert convert(["AH0"]) == "ə" and convert(["AH1"]) == "ʌ"
    assert convert(["ER0"]) == "ɚ" and convert(["ER1"]) == "ɝ"
    assert convert(["K", "AE1", "T"]) == "kæt"  # No redundant monosyllable stress.
    assert convert(["K", "AH0", "M", "P", "Y", "UW1", "T", "ER0"]) == "kəmˈpjuːtɚ"
    assert convert(["P", "R", "OW0", "N", "AH2", "N", "S", "IY0", "EY1", "SH", "AH0", "N"]) == "pɹoʊˌnʌnsiːˈeɪʃən"
    with pytest.raises(ValueError, match="Unknown ARPABET"):
        convert(["K", "NOTAPHONE", "T"])


def test_dictionary_files_match_pinned_upstream_hashes_and_license():
    root = pronunciation.DICTIONARY.parent
    metadata = json.loads((root / "SOURCE.json").read_text(encoding="utf-8"))
    assert metadata["revision"] == "74790861f652b15e4ac49015a90074ad62a27690"
    assert metadata["license"] == "BSD-2-Clause"
    for name, record in metadata["files"].items():
        data = (root / name).read_bytes()
        assert len(data) == record["bytes"]
        assert hashlib.sha256(data).hexdigest() == record["sha256"]
    assert "Carnegie Mellon University" in (root / "LICENSE").read_text(encoding="utf-8")


def test_every_bundled_variant_converts_and_dictionary_cache_is_reused(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline lookup attempted a network connection")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    entries = pronunciation._entries()
    assert len(entries) > 120000
    for variants in entries.values():
        for phones in variants:
            assert pronunciation.arpabet_to_ipa(phones)
    assert pronunciation._entries() is entries
    assert pronunciation.lookup("Hello!")["found"]


def test_missing_dictionary_is_an_installation_error_not_a_guessed_result(tmp_path, monkeypatch):
    pronunciation._entries.cache_clear()
    monkeypatch.setattr(pronunciation, "DICTIONARY", tmp_path / "missing.dict")
    try:
        with pytest.raises(RuntimeError, match="离线发音词典缺失"):
            pronunciation.lookup("hello")
    finally:
        pronunciation._entries.cache_clear()
