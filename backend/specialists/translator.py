from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from typing import Any


_MEMORY_CACHE: dict[tuple[str, str, str], dict[str, str]] = {}


_LABEL_TRANSLATIONS: dict[tuple[str, str, str], str] = {
    ("ja", "en", "氏名"): "full name",
    ("ja", "en", "名前"): "name",
    ("ja", "en", "メール"): "email",
    ("ja", "en", "メールアドレス"): "email address",
    ("ja", "en", "電話番号"): "phone number",
    ("ja", "en", "履歴書"): "resume",
    ("ja", "en", "利用規約に同意しますか"): "agree to terms",
    ("ja", "en", "同意しますか"): "agree",
    ("ja", "en", "はい"): "yes",
    ("ja", "en", "いいえ"): "no",
    ("ko", "en", "이름"): "name",
    ("ko", "en", "이메일"): "email",
    ("ko", "en", "전화번호"): "phone number",
    ("ko", "en", "예"): "yes",
    ("ko", "en", "아니요"): "no",
    ("fr", "en", "nom"): "name",
    ("fr", "en", "prénom"): "first name",
    ("fr", "en", "courriel"): "email",
    ("fr", "en", "adresse e-mail"): "email address",
    ("fr", "en", "téléphone"): "phone number",
    ("fr", "en", "oui"): "yes",
    ("fr", "en", "non"): "no",
    ("es", "en", "nombre"): "name",
    ("es", "en", "correo electrónico"): "email address",
    ("es", "en", "teléfono"): "phone number",
    ("es", "en", "sí"): "yes",
    ("es", "en", "si"): "yes",
    ("es", "en", "no"): "no",
    ("pt", "en", "nome"): "name",
    ("pt", "en", "e-mail"): "email",
    ("pt", "en", "telefone"): "phone number",
    ("pt", "en", "sim"): "yes",
    ("pt", "en", "não"): "no",
    ("pt", "en", "nao"): "no",
}

_EN_TO_LANG = {("en", dst, value): source for (dst, _en, source), value in _LABEL_TRANSLATIONS.items() if _en == "en"}


class Translator:
    """Deterministic translation facade used by form-label code paths.

    The built-in dictionary intentionally covers common application labels and
    yes/no options so tests are stable without network credentials. External
    providers can be plugged in behind this facade later without changing the
    form answerer contract.
    """

    def __init__(self, store: Any | None = None):
        self.store = store
        self.translator_name = "builtin"

    @staticmethod
    def detect(text: str) -> str:
        value = str(text or "").strip().lower()
        if not value:
            return "und"
        if re.search(r"[\uac00-\ud7af]", value):
            return "ko"
        if re.search(r"[\u3040-\u30ff]", value):
            return "ja"
        if re.search(r"[\u4e00-\u9fff]", value):
            return "ja"
        if any(token in value for token in ("courriel", "prénom", "téléphone", "adresse e-mail")):
            return "fr"
        if any(token in value for token in ("correo", "teléfono", "nombre completo", "sí")):
            return "es"
        if any(token in value for token in ("telefone", "não", "currículo")):
            return "pt"
        if re.search(r"[éèêàùçœ]", value):
            return "fr"
        if re.search(r"[ñ¿¡]", value):
            return "es"
        if re.search(r"[ãõáâêô]", value):
            return "pt"
        return "en"

    @staticmethod
    def available() -> bool:
        return True

    @staticmethod
    def available_backends() -> list[str]:
        backends = ["builtin"]
        if os.getenv("JOBPILOT_DEEPL_KEY"):
            backends.append("deepl")
        if os.getenv("JOBPILOT_GCP_TRANSLATE_KEY"):
            backends.append("google_cloud_translate_v3")
        return backends

    def translate(self, text: str, src: str, dst: str = "en") -> str:
        source = (src or "und").lower()
        target = (dst or "en").lower()
        value = str(text or "").strip()
        if not value or source == target:
            return value
        text_hash = _sha256(value)
        cached = self._cache_get(source, target, text_hash)
        if cached is not None:
            return cached
        translated = self._translate_builtin(value, source, target)
        self._cache_put(source, target, text_hash, translated)
        return translated

    def translate_options(self, opts: list[str], src: str, dst: str = "en") -> list[str]:
        return [self.translate(opt, src, dst) for opt in opts]

    def back_translate_bleu(self, en_text: str, dst: str) -> float:
        target = (dst or "en").lower()
        if target == "en":
            return 1.0
        forward = self.translate(en_text, "en", target)
        back = self.translate(forward, target, "en")
        return _simple_bleu(en_text, back)

    def _translate_builtin(self, text: str, src: str, dst: str) -> str:
        exact = _LABEL_TRANSLATIONS.get((src, dst, text))
        if exact is not None:
            return exact
        exact_lower = _LABEL_TRANSLATIONS.get((src, dst, text.lower()))
        if exact_lower is not None:
            return exact_lower
        exact_en = _EN_TO_LANG.get((src, dst, text.lower()))
        if exact_en is not None:
            return exact_en
        parts = re.split(r"(\s*/\s*|\s*,\s*|\s+or\s+)", text, flags=re.IGNORECASE)
        if len(parts) > 1:
            return "".join(_LABEL_TRANSLATIONS.get((src, dst, part.strip().lower()), part) for part in parts)
        return text

    def _cache_get(self, src: str, dst: str, text_hash: str) -> str | None:
        if self.store is not None and hasattr(self.store, "get_translation_cache"):
            record = self.store.get_translation_cache(src=src, dst=dst, text_hash=text_hash)
            if record:
                return str(record["translated_text"])
        record = _MEMORY_CACHE.get((src, dst, text_hash))
        return record["translated_text"] if record else None

    def _cache_put(self, src: str, dst: str, text_hash: str, translated_text: str) -> None:
        _MEMORY_CACHE[(src, dst, text_hash)] = {"translated_text": translated_text, "translator": self.translator_name}
        if self.store is not None and hasattr(self.store, "upsert_translation_cache"):
            self.store.upsert_translation_cache(
                src=src,
                dst=dst,
                text_hash=text_hash,
                translated_text=translated_text,
                translator=self.translator_name,
            )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _simple_bleu(reference: str, candidate: str) -> float:
    ref_tokens = _tokens(reference)
    cand_tokens = _tokens(candidate)
    if not ref_tokens and not cand_tokens:
        return 1.0
    if not ref_tokens or not cand_tokens:
        return 0.0
    overlap = sum((Counter(cand_tokens) & Counter(ref_tokens)).values())
    precision = overlap / max(len(cand_tokens), 1)
    brevity = 1.0 if len(cand_tokens) >= len(ref_tokens) else math.exp(1 - len(ref_tokens) / max(len(cand_tokens), 1))
    return max(0.0, min(1.0, precision * brevity))


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").lower())
