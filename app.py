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
