# -*- coding: utf-8 -*-
"""
db_common.py — Connexion base de données partagée entre `entreprise.py` et
`minigithub.py`, compatible Vercel (serverless).

Trois backends possibles, choisis automatiquement selon les variables
d'environnement disponibles (ordre de priorité) :

1. **Postgres (recommandé — ex : Neon)** — si `DATABASE_URL` (ou
   `POSTGRES_URL`) est défini. C'est une vraie base de données persistante,
   partagée par toutes les invocations serverless de Vercel. C'est le mode
   à utiliser en production.
2. **Turso** (conservé pour compatibilité) — si `TURSO_DATABASE_URL` est
   défini et qu'aucune base Postgres n'est configurée.
3. **SQLite local** — repli utilisé en développement local, quand aucune
   des deux variables ci-dessus n'est présente. Sur Vercel, ce mode ne doit
   JAMAIS être utilisé en production : `/tmp` n'est pas persistant entre
   les invocations.

Comment le reste du code (`entreprise.py`, `minigithub.py`) reste inchangé
--------------------------------------------------------------------------
Tout le code applicatif est écrit en SQL "façon SQLite" : placeholders `?`,
`INTEGER PRIMARY KEY AUTOINCREMENT`, `INSERT OR REPLACE`. Plutôt que de
réécrire des centaines de requêtes en syntaxe Postgres, ce module fournit
un petit adaptateur qui traduit ces requêtes à la volée quand le backend
actif est Postgres :

- `?`                                  ->  `%s`
- `INTEGER PRIMARY KEY AUTOINCREMENT`  ->  `SERIAL PRIMARY KEY`
- `INSERT OR REPLACE INTO`             ->  `INSERT INTO` (voir note plus bas)
- `cursor.lastrowid` est émulé en ajoutant discrètement `RETURNING id` aux
  requêtes INSERT (toutes les tables de l'appli ont une colonne `id`).

Note sur `INSERT OR REPLACE` : dans le code existant, ces requêtes visent
des tables sans contrainte UNIQUE sur les colonnes métier (repo_id+user_id,
etc.) — le "OR REPLACE" ne s'est donc jamais déclenché, y compris sous
SQLite (l'`id` auto-incrémenté est toujours unique, un nouveau conflit
n'arrive jamais). Traduire simplement vers `INSERT INTO` reproduit donc à
l'identique le comportement actuel de l'application, sans effet de bord,
sur les deux moteurs.

Connexion retournée
--------------------
Dans tous les cas, `open_connection()` retourne un objet exposant la même
petite API que `sqlite3.Connection` utilisée dans le reste du code :
`.execute()`, `.cursor()`, `.commit()`, `.rollback()`, `.close()`, avec des
lignes accessibles à la fois par nom (`row['col']`) et par index
(`row[0]`), comme `sqlite3.Row`.
"""

import os
import sqlite3

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

# Dialecte SQL actif. "postgres" quand une base Postgres est configurée,
# "sqlite" sinon (couvre à la fois Turso/libsql — qui parle le dialecte
# SQLite — et le simple fichier SQLite local).
DIALECT = "postgres" if DATABASE_URL else "sqlite"

# Sur Vercel, seul /tmp est inscriptible (et non persistant entre les
# invocations : c'est voulu, la source de vérité est Postgres ou Turso).
LOCAL_REPLICA_PATH = "/tmp/minigithub.db" if os.environ.get("VERCEL") else "minigithub.db"


# ---------------------------------------------------------------------------
# Adaptateur Postgres — donne à psycopg2 la même API que sqlite3.Connection
# ---------------------------------------------------------------------------

class _Row:
    """Ligne accessible par nom (row['col']) ET par index (row[0]), comme
    sqlite3.Row, à partir d'un tuple brut renvoyé par psycopg2."""

    __slots__ = ("_values", "_index")

    def __init__(self, values, index):
        self._values = values
        self._index = index

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def keys(self):
        return list(self._index.keys())

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __contains__(self, key):
        return key in self._index

    def __repr__(self):
        return repr(dict(zip(self._index.keys(), self._values)))


def _translate_sql(sql):
    """Traduit une requête écrite en dialecte SQLite vers Postgres."""
    out = sql
    out = out.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    out = out.replace("INSERT OR REPLACE INTO", "INSERT INTO")
    out = out.replace("INSERT OR IGNORE INTO", "INSERT INTO")
    # Traduction naïve des placeholders : sans risque ici, aucune requête de
    # l'application n'a de "?" littéral dans une valeur ou un commentaire.
    out = out.replace("?", "%s")
    return out


class _PGCursor:
    def __init__(self, raw_cursor):
        self._cur = raw_cursor
        self._lastrowid = None

    def execute(self, sql, params=()):
        translated = _translate_sql(sql)
        stripped = translated.strip()
        upper = stripped.upper()
        self._lastrowid = None

        if upper.startswith("INSERT") and "RETURNING" not in upper:
            # Émule cursor.lastrowid (toutes les tables de l'appli ont un
            # id auto-incrémenté nommé "id").
            with_returning = stripped.rstrip(";") + " RETURNING id"
            try:
                self._cur.execute(with_returning, params)
                row = self._cur.fetchone()
                if row is not None:
                    self._lastrowid = row[0]
                return self
            except Exception:
                # Table sans colonne "id" (aucune ici en pratique) : on
                # retombe sur un INSERT classique sans lastrowid.
                pass

        self._cur.execute(translated, params)
        return self

    def _row_index(self):
        cols = [d[0] for d in self._cur.description] if self._cur.description else []
        return {name: i for i, name in enumerate(cols)}

    def fetchone(self):
        raw = self._cur.fetchone()
        if raw is None:
            return None
        return _Row(tuple(raw), self._row_index())

    def fetchall(self):
        index = self._row_index()
        return [_Row(tuple(r), index) for r in self._cur.fetchall()]

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount

    def close(self):
        self._cur.close()


class _PGConnection:
    """Enveloppe une connexion psycopg2 pour exposer la même API que
    sqlite3.Connection (execute/cursor/commit/rollback/close)."""

    def __init__(self, raw_conn):
        self._conn = raw_conn

    def cursor(self):
        return _PGCursor(self._conn.cursor())

    def execute(self, sql, params=()):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        # La connexion est ouverte en autocommit=True : chaque instruction
        # est déjà persistée immédiatement. Les appels existants à
        # db.commit() dans entreprise.py / minigithub.py deviennent des
        # no-op inoffensifs, sans qu'il faille les retirer du code.
        pass

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


def _open_postgres():
    import psycopg2  # pip install psycopg2-binary

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return _PGConnection(conn)


def _open_turso():
    import libsql  # pip install libsql

    conn = libsql.connect(
        LOCAL_REPLICA_PATH,
        sync_url=TURSO_URL,
        auth_token=TURSO_TOKEN,
    )
    conn.sync()  # récupère les écritures faites par d'autres invocations

    # On fait en sorte qu'un simple `db.commit()` (déjà présent partout
    # dans le code existant) resynchronise aussi vers Turso, pour que
    # la donnée soit immédiatement relue de façon cohérente.
    _real_commit = conn.commit

    def _commit_and_sync():
        _real_commit()
        conn.sync()

    conn.commit = _commit_and_sync
    conn.row_factory = sqlite3.Row
    return conn


def _open_sqlite():
    conn = sqlite3.connect(LOCAL_REPLICA_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def open_connection():
    """Ouvre et retourne une connexion prête à l'emploi (row_factory inclus).

    Priorité : Postgres (DATABASE_URL) > Turso (TURSO_DATABASE_URL) > SQLite
    local (développement uniquement).
    """
    if DATABASE_URL:
        return _open_postgres()
    if TURSO_URL:
        return _open_turso()
    return _open_sqlite()
