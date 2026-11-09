"""Catégorisation automatique, locale et déterministe (aucun service externe).

Trois sources, dans l'ordre :
1. les règles (les tiennes d'abord, puis celles reprises des bénéficiaires mémorisés de Quicken) ;
2. les marchands connus : la catégorie que ce marchand reçoit d'habitude dans l'historique confirmé ;
3. une suggestion statistique (bayésien naïf sur les mots du libellé), seulement si elle est nette.

Une catégorie trouvée est « proposée » : elle compte dans les rapports, mais reste à confirmer.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass

from budget.text import clean_label

MERCHANT_MIN_SHARE = 0.75     # part minimale des transactions d'un marchand dans la même catégorie
STAT_MIN_PROB = 0.70          # probabilité minimale d'une suggestion statistique
STAT_MIN_EXAMPLES = 20        # en dessous, l'historique est trop mince pour une statistique

SOURCES = {"rule": "Règle", "quicken": "Mémorisé Quicken", "merchant": "Marchand connu", "stat": "Suggestion"}


def merchant_key(payee: str, memo: str = "") -> str:
    """Clé d'un marchand. Les relevés Desjardins réservent 25 caractères au marchand ; la ville suit."""
    words = clean_label((payee or memo)[:25]).split()
    if len(words) > 1 and len(words[-1]) == 1:   # mot coupé par la limite de 25 caractères
        words.pop()
    return " ".join(words)


def same_merchant(a: str, b: str) -> bool:
    """Même marchand : clés égales, ou l'une prolonge l'autre (au moins deux mots en commun)."""
    if a == b:
        return bool(a)
    wa, wb = a.split(), b.split()
    n = min(len(wa), len(wb))
    return n >= 2 and wa[:n] == wb[:n]


@dataclass
class Suggestion:
    category_id: int
    source: str
    detail: str = ""


def category_paths(db) -> dict[int, str]:
    rows = db.execute("SELECT id, name, parent_id FROM categories").fetchall()
    by_id = {r["id"]: r for r in rows}

    def path(i):
        parts = []
        while i is not None:
            parts.append(by_id[i]["name"])
            i = by_id[i]["parent_id"]
        return " : ".join(reversed(parts))
    return {i: path(i) for i in by_id}


class Categorizer:
    def __init__(self, db):
        self.db = db
        self.rules = []
        for r in db.execute("SELECT * FROM rules WHERE active = 1 ORDER BY priority, id"):
            self.rules.append((r["id"], json.loads(r["conditions"]), json.loads(r["action"]), r["source"]))
        self._learn()

    def _learn(self) -> None:
        """Historique confirmé, une seule catégorie, hors virements."""
        self.merchants: dict[str, Counter] = defaultdict(Counter)
        self.words: dict[int, Counter] = defaultdict(Counter)
        self.docs: Counter = Counter()
        rows = self.db.execute("""
            SELECT t.payee, t.memo, t.amount_cents, MIN(s.category_id) AS cat
            FROM transactions t JOIN splits s ON s.transaction_id = t.id
            WHERE t.status = 'confirmed' AND t.transfer_id IS NULL AND s.category_id IS NOT NULL
            GROUP BY t.id HAVING COUNT(s.id) = 1
        """).fetchall()
        for r in rows:
            key = merchant_key(r["payee"], r["memo"])
            if key:
                self.merchants[key][r["cat"]] += 1
            self.words[r["cat"]].update(self._tokens(r["payee"], r["memo"], r["amount_cents"]))
            self.docs[r["cat"]] += 1
        self.vocab = {w for c in self.words.values() for w in c}
        self.total_docs = sum(self.docs.values())
        self.word_totals = {c: sum(v.values()) for c, v in self.words.items()}

    @staticmethod
    def _tokens(payee: str, memo: str, amount_cents: int) -> list[str]:
        words = clean_label(payee or memo).split()
        return [w for w in words if len(w) > 1] + ["+ENTREE" if amount_cents > 0 else "+SORTIE"]

    def suggest(self, payee: str, memo: str, amount_cents: int, account_id: int | None = None) -> Suggestion | None:
        label = clean_label(payee or memo)
        key = merchant_key(payee, memo)
        if not label:
            return None
        for rule_id, cond, action, source in self.rules:
            if _matches(cond, label, key, amount_cents, account_id):
                return Suggestion(action["category_id"], source or "rule", str(rule_id))
        counts = self.merchants.get(key)
        if counts:
            cat, n = counts.most_common(1)[0]
            if n / sum(counts.values()) >= MERCHANT_MIN_SHARE:
                return Suggestion(cat, "merchant", f"{n}/{sum(counts.values())}")
        return self._bayes(payee, memo, amount_cents)

    def _bayes(self, payee, memo, amount_cents) -> Suggestion | None:
        if self.total_docs < STAT_MIN_EXAMPLES:
            return None
        tokens = [t for t in self._tokens(payee, memo, amount_cents) if t in self.vocab]
        if len(tokens) < 2:      # le seul signe du montant ne suffit pas
            return None
        v = len(self.vocab)
        scores = {}
        for cat, n in self.docs.items():
            s = math.log(n / self.total_docs)
            wc, total = self.words[cat], self.word_totals[cat]
            for t in tokens:
                s += math.log((wc[t] + 1) / (total + v))
            scores[cat] = s
        best = max(scores, key=scores.get)
        m = scores[best]
        prob = 1 / sum(math.exp(s - m) for s in scores.values())
        if prob < STAT_MIN_PROB:
            return None
        return Suggestion(best, "stat", f"{prob:.0%}")


def _matches(cond: dict, label: str, key: str, amount_cents: int, account_id: int | None) -> bool:
    if "merchant" in cond and not same_merchant(cond["merchant"], key):
        return False
    if "contains" in cond and f" {cond['contains']} " not in f" {label} ":
        return False
    if "account_id" in cond and cond["account_id"] != account_id:
        return False
    if "sign" in cond and (amount_cents > 0) != (cond["sign"] == "in"):
        return False
    return True


def add_rule(db, conditions: dict, category_id: int, source: str = "rule", priority: int = 100) -> int:
    """Ajoute une règle, sauf si une règle identique existe déjà (renvoie alors son id)."""
    cond = json.dumps(conditions, sort_keys=True, ensure_ascii=False)
    row = db.execute("SELECT id FROM rules WHERE conditions = ?", (cond,)).fetchone()
    action = json.dumps({"category_id": category_id})
    if row:
        if source == "rule":
            db.execute("UPDATE rules SET action = ?, source = 'rule', priority = ? WHERE id = ?",
                       (action, priority, row["id"]))
        return row["id"]
    return db.execute("INSERT INTO rules (priority, conditions, action, source) VALUES (?,?,?,?)",
                      (priority, cond, action, source)).lastrowid


def apply(db, transaction_ids: list[int] | None = None, categorizer: Categorizer | None = None) -> int:
    """Propose une catégorie aux transactions à catégoriser (toutes, ou celles données). Renvoie le nombre."""
    cz = categorizer or Categorizer(db)
    sql = ("SELECT id, account_id, payee, memo, amount_cents FROM transactions"
           " WHERE status = 'to_categorize' AND transfer_id IS NULL"
           " AND NOT EXISTS (SELECT 1 FROM splits s WHERE s.transaction_id = transactions.id)")
    args: list = []
    if transaction_ids is not None:
        if not transaction_ids:
            return 0
        sql += f" AND id IN ({','.join('?' * len(transaction_ids))})"
        args = list(transaction_ids)
    n = 0
    with db:
        for r in db.execute(sql, args).fetchall():
            s = cz.suggest(r["payee"], r["memo"], r["amount_cents"], r["account_id"])
            if s is None:
                continue
            db.execute("INSERT INTO splits (transaction_id, category_id, amount_cents) VALUES (?,?,?)",
                       (r["id"], s.category_id, r["amount_cents"]))
            db.execute("UPDATE transactions SET status = 'proposed', category_source = ? WHERE id = ?",
                       (s.source, r["id"]))
            n += 1
    return n


def set_category(db, transaction_id: int, category_id: int | None) -> None:
    """Catégorie choisie par l'utilisateur : remplace les ventilations et confirme."""
    with db:
        t = db.execute("SELECT amount_cents FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
        db.execute("DELETE FROM splits WHERE transaction_id = ?", (transaction_id,))
        if category_id is None:
            db.execute("UPDATE transactions SET status = 'to_categorize', category_source = NULL WHERE id = ?",
                       (transaction_id,))
            return
        db.execute("INSERT INTO splits (transaction_id, category_id, amount_cents) VALUES (?,?,?)",
                   (transaction_id, category_id, t["amount_cents"]))
        db.execute("UPDATE transactions SET status = 'confirmed', category_source = 'user' WHERE id = ?",
                   (transaction_id,))


def confirm(db, transaction_ids: list[int]) -> int:
    if not transaction_ids:
        return 0
    with db:
        return db.execute(
            f"UPDATE transactions SET status = 'confirmed' WHERE status = 'proposed'"
            f" AND id IN ({','.join('?' * len(transaction_ids))})", transaction_ids).rowcount


STARTER = {
    "Revenus": ["Salaire", "Intérêts", "Remboursements", "Autres revenus"],
    "Logement": ["Hypothèque ou loyer", "Électricité", "Assurance habitation", "Taxes municipales", "Entretien"],
    "Alimentation": ["Épicerie", "Restaurant"],
    "Transport": ["Essence", "Entretien auto", "Assurance auto", "Transport en commun", "Stationnement"],
    "Santé": ["Pharmacie", "Soins"],
    "Communications": ["Internet", "Cellulaire", "Abonnements"],
    "Loisirs": ["Sorties", "Voyages", "Sports"],
    "Achats": ["Vêtements", "Maison", "Électronique", "Cadeaux"],
    "Frais bancaires": ["Frais", "Intérêts payés"],
}


def create_starter_categories(db) -> int:
    n = 0
    with db:
        for parent, children in STARTER.items():
            kind = "income" if parent == "Revenus" else "expense"
            row = db.execute("SELECT id FROM categories WHERE name = ? AND parent_id IS NULL", (parent,)).fetchone()
            pid = row["id"] if row else db.execute(
                "INSERT INTO categories (name, kind) VALUES (?, ?)", (parent, kind)).lastrowid
            for c in children:
                if not db.execute("SELECT 1 FROM categories WHERE name = ? AND parent_id = ?", (c, pid)).fetchone():
                    db.execute("INSERT INTO categories (name, parent_id, kind) VALUES (?,?,?)", (c, pid, kind))
                    n += 1
    return n


def find_or_create_category(db, typed: str, create: bool = False) -> int | None:
    """Catégorie à partir du texte tapé (« Santé : Pharmacie » ou « Santé:Pharmacie »)."""
    parts = [p.strip() for p in typed.split(":") if p.strip()]
    if not parts:
        return None
    wanted = " : ".join(parts).casefold()
    for cid, path in category_paths(db).items():
        if path.casefold() == wanted:
            return cid
    if not create:
        return None
    parent = None
    with db:
        for part in parts:
            row = db.execute("SELECT id FROM categories WHERE name = ? AND parent_id IS ?", (part, parent)).fetchone()
            parent = row["id"] if row else db.execute(
                "INSERT INTO categories (name, parent_id) VALUES (?, ?)", (part, parent)).lastrowid
    return parent


def recategorize(db, transaction_id: int, category_id: int) -> list[int]:
    """Choix de l'utilisateur, à la Quicken : confirme, mémorise le marchand (règle) et
    applique aux autres transactions non confirmées du même marchand. Renvoie leurs id."""
    t = db.execute("SELECT payee, memo FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    set_category(db, transaction_id, category_id)
    key = merchant_key(t["payee"], t["memo"])
    if not key:
        return []
    with db:
        add_rule(db, {"merchant": key}, category_id)
    others = [r["id"] for r in db.execute(
        "SELECT id, payee, memo FROM transactions WHERE status != 'confirmed' AND transfer_id IS NULL AND id != ?",
        (transaction_id,)) if same_merchant(key, merchant_key(r["payee"], r["memo"]))]
    with db:
        for i in others:
            db.execute("DELETE FROM splits WHERE transaction_id = ?", (i,))
            db.execute("INSERT INTO splits (transaction_id, category_id, amount_cents)"
                       " SELECT id, ?, amount_cents FROM transactions WHERE id = ?", (category_id, i))
            db.execute("UPDATE transactions SET status = 'proposed', category_source = 'rule' WHERE id = ?", (i,))
    return others
