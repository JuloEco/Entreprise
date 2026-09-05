# -*- coding: utf-8 -*-
"""
app.py — Point d'entrée unique pour Vercel.

Vercel ne peut pas lancer deux serveurs Flask sur deux ports comme en local
(`python entreprise.py` + `python minigithub.py`). On combine donc les deux
applications Flask en une seule application WSGI, montée sur un seul
domaine, à des préfixes différents :

    /            -> CorpSuite   (entreprise.py)
    /github/...  -> Mini GitHub (minigithub.py)

C'est ce qui permet aux deux systèmes de continuer à partager la même
session de connexion (même secret_key, même cookie, même domaine) sans
changer la logique métier des deux fichiers d'origine.

En local, vous pouvez toujours lancer les deux apps séparément comme avant
(`python entreprise.py` sur :5001, `python minigithub.py` sur :5000) pour le
développement au quotidien ; ce fichier n'est utilisé que par Vercel (et par
`python app.py` si vous voulez tester la version combinée en local).
"""

from werkzeug.middleware.dispatcher import DispatcherMiddleware

import entreprise
import minigithub

# ---------------------------------------------------------------------------
# Création des tables au démarrage (obligatoire sur Vercel)
# ---------------------------------------------------------------------------
# Sur Vercel, personne n'appelle jamais `python entreprise.py` /
# `python minigithub.py` ni `scripts/init_remote_db.py` : le fichier exécuté
# est celui-ci, et Vercel se contente d'importer l'objet `app` sans jamais
# passer par un bloc `if __name__ == '__main__':`. Résultat, si les tables
# n'ont pas été créées à la main au préalable sur la base distante, la toute
# première requête plante avec `sqlite3.OperationalError: no such table`.
#
# `init_db()` (dans les deux fichiers) n'utilise que des
# `CREATE TABLE IF NOT EXISTS`, et son `seed_data()` ne réinsère les comptes
# de démo que si la table `users` est vide : on peut donc l'appeler à chaque
# démarrage d'instance serverless sans risque de doublons ou d'écrasement de
# données existantes.
try:
    minigithub.init_db()
    entreprise.init_db()
except Exception as _init_err:  # pragma: no cover
    # On ne bloque pas le démarrage de l'app : si la base distante (Turso)
    # est momentanément injoignable, on préfère laisser Vercel réessayer sur
    # la requête suivante plutôt que planter tout le déploiement. L'erreur
    # reste visible dans les logs Vercel.
    print(f"[app.py] Échec de l'initialisation de la base au démarrage : {_init_err}")

application = DispatcherMiddleware(
    entreprise.app,          # monté à la racine "/"
    {
        "/github": minigithub.app,
    },
)

# Vercel (comme la plupart des serveurs WSGI) sait servir un objet nommé
# `app`. Flask lui-même n'a pas besoin d'être appelé ici : DispatcherMiddleware
# EST déjà une application WSGI valide.
app = application

if __name__ == "__main__":
    # Pratique pour tester la version "combinée" en local avant de déployer.
    from werkzeug.serving import run_simple
    run_simple("127.0.0.1", 3000, application, use_reloader=True, use_debugger=True)
