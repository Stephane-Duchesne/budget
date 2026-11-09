"""Lecteur des relevés PDF de carte Visa Desjardins.

Port en Python de StatementToQif.ps1 : mêmes motifs, même règle de signe, même
déduction de l'année et même contrôle des totaux du sommaire.

Différences avec le script :
- le texte n'est pas obtenu par `pdftotext -layout` (programme externe) mais
  reconstruit à partir de la position des mots sur la page (pdfplumber) ;
- le marchand d'un achat par carte est découpé par position (25 caractères
  mesurés sur la page) et non par nombre de caractères dans le texte, ce qui
  évite d'y inclure la première lettre de la ville.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal
from pathlib import Path

import pdfplumber

from budget.models import Check, ReadResult, Transaction

FORMAT = "visa-desjardins-pdf"

# Ligne de transaction :
#   <jour> <mois> <jour inscr.> <mois inscr.> [<n° de référence>] <libellé> [<taux> %] <montant> [CR]
# Les deux paires de dates sont exigées, ce qui écarte les lignes d'en-tête
# du type « 26 02 2025 ... 2 069,52 » (échéance + nouveau solde).
TX_PATTERN = re.compile(
    r"^\s*(?P<d>\d{1,2})\s+(?P<m>\d{1,2})\s+"
    r"(?P<pd>\d{1,2})\s+(?P<pm>\d{1,2})\s+"
    r"(?:\d{6,}\s+)?"
    r"(?P<desc>.+?)\s+"
    r"(?P<rate>\d+(?:[.,]\d+)?\s*%\s+)?"
    r"(?P<amt>-?\d{1,3}(?:[  ]\d{3})*,\d{2})\s*(?P<cr>CR)?\s*$"
)

# « DATE DU RELEVÉ  Jour 06 Mois 01 Année 2026 »
STATEMENT_DATE_PATTERN = re.compile(
    r"DATE DU RELEV.\s+Jour\s+(?P<d>\d{1,2})\s+Mois\s+(?P<m>\d{1,2})\s+Ann.e\s+(?P<y>\d{4})"
)

# Lignes du « SOMMAIRE DES TRANSACTIONS COURANTES ». Les débits s'additionnent ;
# « Paiements / crédits » est le total des crédits.
SUMMARY_PATTERN = re.compile(
    r"^\s*(?P<label>Achats / d.bits|Avances d'argent|Frais de cr.dit|"
    r"Mensualit. financement Accord D|Paiements / cr.dits)\s+[+-]\s+"
    r"(?P<amt>\d{1,3}(?:[  ]\d{3})*,\d{2})(?=\s|$)"
)

# Une ligne qui commence comme une transaction mais ne correspond pas au motif
# est signalée, jamais ignorée en silence.
LOOKS_LIKE_TX = re.compile(r"^\s*\d{1,2}\s+\d{1,2}\s+\d{1,2}\s+\d{1,2}\s+\S")

# Largeur du champ « marchand » sur les achats par carte (Desjardins : 25 caractères).
PAYEE_WIDTH = 25

# Deux caractères dont le haut diffère de moins de cette valeur (en points) sont sur la même ligne.
LINE_TOLERANCE = 2.0


def parse_amount(text: str) -> Decimal:
    """« 1 234,56 » -> Decimal('1234.56')."""
    return Decimal(re.sub(r"[  ]", "", text).replace(",", "."))


class Word:
    """Un mot du relevé, avec ses caractères et sa position dans la ligne reconstruite."""
    __slots__ = ("text", "x0", "x1", "chars", "offset")

    def __init__(self, w: dict, offset: int):
        self.text = w["text"]
        self.x0 = w["x0"]
        self.x1 = w["x1"]
        self.chars = w["chars"]
        self.offset = offset


class Line:
    """Une ligne reconstruite : son texte et les mots qui la composent."""
    __slots__ = ("text", "words")

    def __init__(self, text: str, words: list[Word]):
        self.text = text
        self.words = words

    def word_at(self, offset: int) -> Word | None:
        return next((w for w in self.words if w.offset == offset), None)


def _char_width(c: dict) -> float:
    return max(c["x1"] - c["x0"], 0.1)


def page_lines(page) -> list[Line]:
    """Reconstruit les lignes d'une page à partir des mots et de leur position.

    L'écart entre deux mots est converti en nombre d'espaces selon la largeur des
    caractères voisins : sur les lignes en chasse fixe, on retrouve exactement les
    colonnes du relevé (comme pdftotext -layout) ; ailleurs, un grand écart donne
    au moins deux espaces, ce qui sépare les colonnes.
    """
    words = page.extract_words(return_chars=True, keep_blank_chars=False, use_text_flow=False)
    rows: list[tuple[float, list[dict]]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        for top, row in rows:
            if abs(top - w["top"]) <= LINE_TOLERANCE:
                row.append(w)
                break
        else:
            rows.append((w["top"], [w]))

    lines = []
    for _, row in sorted(rows, key=lambda r: r[0]):
        row.sort(key=lambda w: w["x0"])
        text, placed = "", []
        for i, w in enumerate(row):
            if i:
                prev = row[i - 1]
                gap = w["x0"] - prev["x1"]
                width = _char_width(prev["chars"][-1])
                text += " " * max(1, round(gap / width))
            placed.append(Word(w, len(text)))
            text += w["text"]
        lines.append(Line(unicodedata.normalize("NFC", text), placed))
    return lines


def positional_payee(line: Line, desc_offset: int, desc_text: str) -> str:
    """Marchand = les PAYEE_WIDTH premiers caractères du libellé, mesurés sur la page.

    Le champ marchand des achats par carte a une largeur fixe ; la ville suit
    immédiatement. Le découpage par position évite d'avaler la première lettre de
    la ville quand le nom remplit tout le champ (« DEPANNEUR MOISE-VINCENT S »).
    """
    first = line.word_at(desc_offset)
    if first is None or not first.chars:
        return re.split(r"\s{2,}", desc_text)[0][:PAYEE_WIDTH]
    pitch = _char_width(first.chars[0])
    limit = first.x0 + PAYEE_WIDTH * pitch - pitch / 2
    parts, prev_x1 = [], None
    for w in line.words:
        if w.offset < desc_offset:
            continue
        if w.x0 >= limit:
            break
        # Un mot qui déborde du champ après un grand écart est la ville, décalée
        # d'un cran (ex. « TORBOX      TORBOX.APP ») : on s'arrête. Sans écart, c'est
        # un nom collé à la ville (« LA COCHONNE RICHAMBLY ») : on le coupe.
        crosses = w.x1 > limit + pitch / 2
        after_gap = prev_x1 is not None and w.x0 - prev_x1 >= 2.5 * pitch
        if crosses and after_gap:
            break
        parts.append("".join(c["text"] for c in w.chars if c["x0"] < limit))
        prev_x1 = w.x1
    return unicodedata.normalize("NFC", " ".join(p for p in parts if p))


def extract_lines(path: str | Path) -> list[Line]:
    with pdfplumber.open(path) as pdf:
        lines: list[Line] = []
        for page in pdf.pages:
            lines.extend(page_lines(page))
    return lines


def _resolve_year(ref: date, month: int, day: int, forced_year: int | None) -> date:
    """Les transactions précèdent la date du relevé : une date postérieure relève de l'année précédente."""
    if forced_year:
        return date(forced_year, month, day)
    d = date(ref.year, month, day)
    return d.replace(year=d.year - 1) if d > ref else d


def parse_lines(lines: list[Line], year: int | None = None) -> ReadResult:
    result = ReadResult(format=FORMAT)
    texts = [ln.text for ln in lines]

    m = STATEMENT_DATE_PATTERN.search("\n".join(texts))
    if m:
        ref = date(int(m["y"]), int(m["m"]), int(m["d"]))
        result.statement_date = ref
    elif year:
        ref = date(year, 12, 31)
    else:
        ref = date.today()
        result.warnings.append(
            "Date du relevé introuvable : on suppose que le relevé date des 12 derniers mois. "
            "Indiquez l'année si les dates sont fausses."
        )

    for ln in lines:
        line = ln.text
        t = TX_PATTERN.match(line)
        if not t:
            if LOOKS_LIKE_TX.match(line):
                result.warnings.append(f"Ligne ignorée (à vérifier) : {line.strip()}")
            continue
        try:
            tx_date = _resolve_year(ref, int(t["m"]), int(t["d"]), year)
            posted = _resolve_year(ref, int(t["pm"]), int(t["pd"]), year)
        except ValueError:
            result.warnings.append(f"Date impossible, ligne ignorée : {line.strip()}")
            continue

        amount = parse_amount(t["amt"])
        # Achat = dépense (négatif). CR ou signe moins = crédit (positif).
        is_credit = bool(t["cr"]) or amount < 0
        value = abs(amount) if is_credit else -amount

        raw_desc = t["desc"].strip()
        memo = re.sub(r"\s{2,}", " ", raw_desc)
        # Marchand = première colonne du libellé, pour que les marchands connus se retrouvent d'un relevé à l'autre.
        if t["rate"]:
            # Achat par carte : champ marchand de largeur fixe, suivi de la ville.
            payee = positional_payee(ln, t.start("desc"), raw_desc)
        else:
            payee = re.split(r"\s{2,}", raw_desc)[0]
        result.transactions.append(Transaction(
            date=tx_date, amount=value, payee=payee.strip(), memo=memo,
            posted_date=posted, raw=line.strip(),
        ))

    if not result.transactions:
        result.warnings.append("Aucune transaction trouvée : la mise en page du relevé a peut-être changé.")

    charges = -sum((t.amount for t in result.transactions if t.amount < 0), Decimal(0))
    credits = sum((t.amount for t in result.transactions if t.amount > 0), Decimal(0))
    expected_debits: Decimal | None = None
    expected_credits: Decimal | None = None
    for line in texts:
        s = SUMMARY_PATTERN.match(line)
        if not s:
            continue
        v = parse_amount(s["amt"])
        if s["label"].startswith("Paiements"):
            if expected_credits is None:
                expected_credits = v
        else:
            expected_debits = (expected_debits or Decimal(0)) + v
    result.checks = [
        Check("Achats / débits", charges, expected_debits),
        Check("Paiements / crédits", credits, expected_credits),
    ]
    return result


def read(path: str | Path, year: int | None = None) -> ReadResult:
    """Lit un relevé PDF Visa Desjardins."""
    return parse_lines(extract_lines(path), year=year)


def sniff(path: str | Path) -> bool:
    """Vrai si le fichier ressemble à un relevé Visa Desjardins."""
    if Path(path).suffix.lower() != ".pdf":
        return False
    try:
        with pdfplumber.open(path) as pdf:
            first = pdf.pages[0].extract_text() or ""
    except Exception:
        return False
    return "DESJARDINS" in first.upper() and "VISA" in first.upper()
