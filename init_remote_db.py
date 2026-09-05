# -*- coding: utf-8 -*-
"""
scripts/init_remote_db.py
==========================

À lancer UNE FOIS en local, avant le premier déploiement (et à chaque fois
que le schéma change), pour créer les tables sur la base Turso distante.

Vercel n'exécute jamais les blocs `if __name__ == '__main__':` de
entreprise.py / minigithub.py (Vercel importe seulement l'objet `app`), donc
personne n'appelle `init_db()` en production : il faut le faire depuis
votre machine, une fois, contre la base distante.

Utilisation :
    export TURSO_DATABASE_URL="libsql://votre-base.turso.io"
    export TURSO_AUTH_TOKEN="votre-token"
    cd vercel_migration
    python scripts/init_remote_db.py
"""

import os
import sys

if not os.environ.get("TURSO_DATABASE_URL"):
    sys.exit(
        "TURSO_DATABASE_URL n'est pas défini. Exportez TURSO_DATABASE_URL et "
        "TURSO_AUTH_TOKEN avant de lancer ce script (voir .env.example)."
    )

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import minigithub
import entreprise

print("→ Création des tables Mini GitHub + comptes de démo...")
minigithub.init_db()

print("→ Création des tables CorpSuite...")
entreprise.init_db()

print("OK : la base Turso distante est prête.")
