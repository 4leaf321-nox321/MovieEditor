"""Korean text to MeloTTS model inputs.

A port of MeloTTS's melo/text/korean.py and utils.get_text_for_tts_infer (MIT License,
Copyright (c) 2024 MyShell.ai) that runs without torch or transformers: the BERT tokenizer
is loaded from tokenizer.json and grapheme-to-phoneme uses the vendored g2pkk rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jamo import hangul_to_jamo

from .ko_dictionary import english_dictionary, etc_dictionary

# Same character class as MeloTTS (Chinese characters are dropped), written as escapes.
_CJK = re.compile(
    "[\u2e80-\u2e99\u2e9b-\u2ef3\u2f00-\u2fd5\u3005\u3007\u3021-\u3029\u3038-\u303a\u303b"
    "\u3400-\u4db5\u4e00-\u9fc3\u8c48-\u9db4\u4fae-\u983b\u4e26-\u9f8e]"
)
_ETC = re.compile("|".join(re.escape(key) for key in etc_dictionary))


def normalize(text: str) -> str:
    text = _CJK.sub("", text.strip())
    if any(key in text for key in etc_dictionary):
        text = _ETC.sub(lambda m: etc_dictionary[m.group()], text)
    text = re.sub("([A-Za-z]+)", lambda m: english_dictionary.get(m.group(), m.group()), text)
    return text.lower()


def distribute_phone(n_phone: int, n_word: int) -> list[int]:
    """Spread n_phone phones over n_word word pieces, one at a time to the emptiest piece."""
    per_word = [0] * n_word
    for _ in range(n_phone):
        per_word[per_word.index(min(per_word))] += 1
    return per_word


@dataclass
class ModelInput:
    phone_ids: list[int]
    tone_ids: list[int]
    language_ids: list[int]
    token_ids: list[int]  # BERT input, with [CLS] and [SEP]
    word2ph: list[int]  # phones per BERT token, used to stretch BERT features over phones


class KoreanFrontend:
    def __init__(self, tokenizer, g2p, config: dict):
        self.tokenizer = tokenizer
        self.g2p = g2p
        self.symbol_to_id = {s: i for i, s in enumerate(config["symbols"])}
        self.punctuation = set(config["punctuation"])
        self.tone_start = config["tone_start"]
        self.language_id = config["language_id"]
        self.add_blank = config["add_blank"]

    def _phonemes(self, word: str) -> list[str]:
        jamo = "".join(hangul_to_jamo(self.g2p(normalize(word))))
        # MeloTTS fails on symbols outside its inventory (e.g. stray signs); skip them instead.
        return [p for p in jamo if p in self.symbol_to_id]

    def __call__(self, text: str) -> ModelInput:
        norm = normalize(text)
        groups: list[list[str]] = []
        for token in self.tokenizer.encode(norm, add_special_tokens=False).tokens:
            if token.startswith("#") and groups:
                groups[-1].append(token.replace("#", ""))
            else:
                groups.append([token])

        phones, word2ph = [], []
        for group in groups:
            word = "".join(group)
            if word == "[UNK]":
                phones.append("_")
                word2ph.append(1)
            elif word in self.punctuation:
                phones.append(word)
                word2ph.append(1)
            else:
                ph = self._phonemes(word)
                word2ph += distribute_phone(len(ph), len(group))
                phones += ph
        phones = ["_"] + phones + ["_"]
        word2ph = [1] + word2ph + [1]

        ids = [self.symbol_to_id[p] for p in phones]
        tones = [self.tone_start] * len(ids)
        langs = [self.language_id] * len(ids)
        if self.add_blank:
            ids, tones, langs = (_intersperse(seq) for seq in (ids, tones, langs))
            word2ph = [w * 2 for w in word2ph]
            word2ph[0] += 1

        token_ids = self.tokenizer.encode(norm).ids
        if len(token_ids) != len(word2ph):
            raise ValueError(f"MeloTTS 입력 정렬 오류: 토큰 {len(token_ids)}개, 음소 묶음 {len(word2ph)}개")
        return ModelInput(ids, tones, langs, token_ids, word2ph)


def _intersperse(seq: list[int]) -> list[int]:
    out = [0] * (2 * len(seq) + 1)
    out[1::2] = seq
    return out
