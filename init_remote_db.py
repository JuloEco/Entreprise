# -*- coding: utf-8 -*-
"""
scripts/init_remote_db.py
==========================

À lancer UNE FOIS en local, avant le premier déploiement (et à chaque fois
que le schéma change), pour créer les tables sur la base distante — Postgres
(Neon) ou Turso selon ce qui est configuré dans votre environnement.

En pratique, ce n'est plus strictement nécessaire : `app.py` appelle déjà
`init_db()` pour les deux applications à chaque démarrage (cold start), donc
les tables se créent toutes seules au premier déploiement. Ce script reste
utile pour :
  - créer les tables en amont, avant même le premier déploiement ;
  - forcer la mise à jour du schéma après une évolution du code, sans
    attendre une invocation Vercel.

Utilisation (Postgres / Neon) :
    export DATABASE_URL="postgresql://user:password@ep-xxx.neon.tech/dbname?sslmode=require"
    cd vercel_migration
    pip install -r requirements.txt
    python scripts/init_remote_db.py

Utilisation (Turso, si vous utilisez encore ce backend) :
    export TURSO_DATABASE_URL="libsql://votre-base.turso.io"
    export TURSO_AUTH_TOKEN="votre-token"
    cd vercel_migration
    python scripts/init_remote_db.py
"""

import os
import sys

if not os.environ.get("DATABASE_URL") and not os.environ.get("TURSO_DATABASE_URL"):
    sys.exit(
        "Aucune base distante configurée. Exportez DATABASE_URL (Postgres / "
        "Neon, recommandé) ou TURSO_DATABASE_URL + TURSO_AUTH_TOKEN (Turso) "
        "avant de lancer ce script (voir .env.example)."
    )

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_common
import minigithub
import entreprise

print(f"→ Backend actif : {db_common.DIALECT}")

print("→ Création des tables Mini GitHub + comptes de démo...")
minigithub.init_db()

print("→ Création des tables CorpSuite...")
entreprise.init_db()

print("OK : la base distante est prête.")
