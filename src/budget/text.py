"""Nettoyage des libellés, pour comparer des transactions venues de formats différents."""
from __future__ import annotations

import re
import unicodedata


def clean_label(text: str) -> str:
    """Libellé normalisé : majuscules sans accents, numéros de magasin et de référence retirés.

    « IGA EXTRA #8021 » et « Iga Extra 8021 » donnent tous deux « IGA EXTRA ».
    """
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).upper()
    t = re.sub(r"#\s*\d+", " ", t)            # numéro de magasin : #8021
    t = re.sub(r"\b[A-Z]*\d[\dA-Z\-]{2,}\b", " ", t)   # références : 0321, P3D7C215D9, 450-658-6332
    t = re.sub(r"[^A-Z0-9&' ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def similarity(a: str, b: str) -> float:
    """Part des mots communs entre deux libellés nettoyés (0 à 1)."""
    wa, wb = set(clean_label(a).split()), set(clean_label(b).split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))
