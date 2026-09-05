# -*- coding: utf-8 -*-
"""
db_common.py — Connexion base de données partagée entre `entreprise.py` et
`minigithub.py`, compatible Vercel (serverless).

Pourquoi ce module existe
-------------------------
Sur Vercel, chaque requête peut être exécutée par une instance de fonction
différente, et le disque (sauf /tmp) est en lecture seule et non persistant.
Un simple fichier `minigithub.db` ne peut donc plus servir de base de
données partagée comme en local.

On utilise ici Turso (base compatible SQLite, hébergée), via le paquet
officiel `libsql`, en mode "réplique embarquée" :
- les lectures se font sur une copie locale dans /tmp (rapide),
- les écritures partent directement vers la base distante Turso,
- `conn.sync()` récupère les dernières écritures avant de lire.

Si aucune variable TURSO_DATABASE_URL n'est définie (ex: développement en
local), on retombe sur un simple fichier sqlite3 classique, exactement
comme dans les scripts d'origine.

⚠️ À vérifier de votre côté avant mise en prod : l'API de `libsql` est
annoncée "compatible sqlite3" (row_factory, cursor, lastrowid, etc.), mais
n'a pas pu être testée en conditions réelles ici (pas d'accès réseau à
Turso depuis cet environnement). Faites un test d'écriture/lecture complet
après déploiement.
"""

import os
import sqlite3

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

# Sur Vercel, seul /tmp est inscriptible (et non persistant entre les
# invocations : c'est voulu, la source de vérité est Turso).
LOCAL_REPLICA_PATH = "/tmp/minigithub.db" if os.environ.get("VERCEL") else "minigithub.db"


def open_connection():
    """Ouvre et retourne une connexion prête à l'emploi (row_factory inclus)."""
    if TURSO_URL:
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
    else:
        conn = sqlite3.connect(LOCAL_REPLICA_PATH)
        conn.execute("PRAGMA foreign_keys = ON")

    conn.row_factory = sqlite3.Row
    return conn
