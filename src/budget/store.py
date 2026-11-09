"""Base de données SQLite : un seul fichier par utilisateur.

Les montants sont stockés en cents (entiers) pour éviter les erreurs d'arrondi.
Le schéma porte un numéro de version (PRAGMA user_version) et se met à jour
automatiquement à l'ouverture.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA_V1 = """
CREATE TABLE accounts (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL CHECK (kind IN ('imported', 'manual')),
    nature          TEXT NOT NULL CHECK (nature IN ('asset', 'liability')),
    currency        TEXT NOT NULL DEFAULT 'CAD',
    opening_cents   INTEGER NOT NULL DEFAULT 0,
    opening_date    TEXT,
    external_id     TEXT,              -- n° de compte lu dans un QFX
    closed          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE imports (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    file_name       TEXT NOT NULL,
    file_sha256     TEXT NOT NULL,
    format          TEXT NOT NULL,
    statement_date  TEXT,
    period_start    TEXT,
    period_end      TEXT,
    closing_cents   INTEGER,
    reconciled      INTEGER,           -- 1 si les contrôles du relevé concordaient
    imported_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE categories (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    parent_id       INTEGER REFERENCES categories(id),
    kind            TEXT NOT NULL DEFAULT 'expense' CHECK (kind IN ('expense', 'income')),
    UNIQUE (parent_id, name)
);

CREATE TABLE transactions (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    date            TEXT NOT NULL,
    posted_date     TEXT,
    amount_cents    INTEGER NOT NULL,
    payee           TEXT NOT NULL DEFAULT '',
    memo            TEXT NOT NULL DEFAULT '',
    label_clean     TEXT NOT NULL DEFAULT '',
    fitid           TEXT,
    import_id       INTEGER REFERENCES imports(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'to_categorize'
                    CHECK (status IN ('to_categorize', 'proposed', 'confirmed')),
    transfer_id     INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    raw             TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX tx_fitid ON transactions(account_id, fitid) WHERE fitid IS NOT NULL;
CREATE INDEX tx_lookup ON transactions(account_id, amount_cents, date);

CREATE TABLE splits (
    id              INTEGER PRIMARY KEY,
    transaction_id  INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    category_id     INTEGER REFERENCES categories(id),
    amount_cents    INTEGER NOT NULL,
    memo            TEXT NOT NULL DEFAULT ''
);

CREATE TABLE rules (
    id              INTEGER PRIMARY KEY,
    priority        INTEGER NOT NULL DEFAULT 100,
    conditions      TEXT NOT NULL,     -- JSON
    action          TEXT NOT NULL,     -- JSON
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE budgets (
    id              INTEGER PRIMARY KEY,
    category_id     INTEGER NOT NULL REFERENCES categories(id),
    month           TEXT NOT NULL,     -- AAAA-MM
    amount_cents    INTEGER NOT NULL,
    UNIQUE (category_id, month)
);
"""

MIGRATIONS = {1: SCHEMA_V1}


def to_cents(amount: Decimal) -> int:
    return int((Decimal(amount) * 100).to_integral_value())


def from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


def connect(path: str | Path) -> sqlite3.Connection:
    """Ouvre (ou crée) la base et applique les migrations en attente."""
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    for v in range(version + 1, SCHEMA_VERSION + 1):
        with db:
            db.executescript(MIGRATIONS[v])
            db.execute(f"PRAGMA user_version = {v}")
    return db


def create_account(db, name: str, kind: str = "imported", nature: str = "asset",
                   opening: Decimal = Decimal(0), opening_date: str | None = None,
                   external_id: str | None = None) -> int:
    with db:
        cur = db.execute(
            "INSERT INTO accounts (name, kind, nature, opening_cents, opening_date, external_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (name, kind, nature, to_cents(opening), opening_date, external_id),
        )
    return cur.lastrowid


def accounts_with_balance(db) -> list[sqlite3.Row]:
    return db.execute("""
        SELECT a.*, a.opening_cents + COALESCE(SUM(t.amount_cents), 0) AS balance_cents,
               COUNT(t.id) AS tx_count, MAX(t.date) AS last_date
        FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id
        WHERE a.closed = 0
        GROUP BY a.id ORDER BY a.nature, a.name
    """).fetchall()
