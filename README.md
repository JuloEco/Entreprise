# CorpSuite + Mini GitHub — déploiement Vercel

## Ce qui a changé par rapport à vos deux fichiers d'origine

Vos deux apps Flask ne pouvaient pas tourner telles quelles sur Vercel, pour
deux raisons structurelles (pas des détails de configuration) :

1. **Deux serveurs sur deux ports** — Vercel exécute du code Python comme
   des fonctions serverless à la demande, pas comme deux process qui
   tournent en continu sur `:5000` et `:5001`. Il n'y a pas de "port" à
   ouvrir.
2. **SQLite en fichier local** — `minigithub.db` vivait sur le disque local.
   Sur Vercel, le disque est en lecture seule (sauf `/tmp`, qui est effacé
   entre les invocations). Un fichier SQLite classique ne peut donc plus
   servir de source de vérité partagée.

Les fichiers fournis ici règlent les deux problèmes en touchant le moins
possible à la logique métier :

| Fichier | Rôle |
|---|---|
| `entreprise.py` | Votre `app.py` (CorpSuite), renommé pour ne pas entrer en conflit avec le point d'entrée Vercel, avec `secret_key`, `MINIGITHUB_URL` et la connexion DB rendus configurables. |
| `minigithub.py` | Votre `github.py`, même traitement. |
| `db_common.py` | Nouveau. Fournit la connexion DB : Turso (base SQLite hébergée) en prod, simple fichier SQLite en local — sans toucher au reste du code SQL. |
| `app.py` | Nouveau. Point d'entrée Vercel : combine les deux apps Flask en une seule, sous un même domaine (`/` pour CorpSuite, `/github` pour Mini GitHub), pour que la session de connexion reste partagée. |
| `scripts/init_remote_db.py` | À lancer une fois en local pour créer les tables sur la base distante. |
| `vercel.json`, `.python-version`, `requirements.txt` | Config de déploiement. |

Le reste — toutes vos routes, templates, permissions, PR, diffs — est
**inchangé**.

## Étapes de déploiement

### 1. Créer la base Turso

```bash
npm install -g turso   # ou voir https://docs.turso.tech/cli/installation
turso auth login
turso db create corpsuite
turso db show --url corpsuite
turso db tokens create corpsuite
```

Notez l'URL et le token.

### 2. Créer les tables sur la base distante (une seule fois)

```bash
export TURSO_DATABASE_URL="libsql://....turso.io"
export TURSO_AUTH_TOKEN="...."
cd vercel_migration
pip install -r requirements.txt
python scripts/init_remote_db.py
```

Relancez ce script à chaque évolution du schéma (nouvelles tables/colonnes).

### 3. Configurer les variables d'environnement sur Vercel

Dans les réglages du projet Vercel (Settings → Environment Variables) :

- `SHARED_SECRET_KEY` — une valeur aléatoire (`python -c "import secrets; print(secrets.token_hex(32))"`)
- `TURSO_DATABASE_URL`
- `TURSO_AUTH_TOKEN`
- `GEMINI_API_KEY` (optionnel, pour les résumés IA des pull requests)

### 4. Déployer

```bash
vercel deploy
```

Vercel détecte automatiquement `app.py` à la racine comme point d'entrée
Python (via `requirements.txt`).

## Développement local

Rien ne change : vous pouvez toujours lancer séparément
`python entreprise.py` (port 5001) et `python minigithub.py` (port 5000)
sans définir `TURSO_DATABASE_URL` — `db_common.py` retombe alors sur un
fichier `minigithub.db` local, exactement comme avant.

Pour tester la version combinée (celle réellement utilisée sur Vercel) :

```bash
python app.py     # http://127.0.0.1:3000  (/  et /github)
```

## Points à vérifier vous-même avant mise en prod

- **API `libsql`** : annoncée compatible `sqlite3` (row_factory, curseurs,
  `lastrowid`) par Turso, mais je n'ai pas pu la tester contre une vraie
  base Turso depuis cet environnement (pas d'accès réseau à Turso ici).
  Faites un test complet création de compte → création d'entreprise →
  création de projet après le premier déploiement.
- **Données de démo** : dans le `github.py` que vous m'avez fourni, la
  fonction `seed_data()` calcule `pw = hash_password('password123')` mais
  ne contient plus les `INSERT` des comptes alice/bob/charlie (section
  vide dans le fichier reçu) — à compléter si vous en avez besoin, ce
  n'est pas lié à la migration Vercel.
- **Limite de durée** : le plan gratuit Vercel limite l'exécution à 10s
  (60s en Pro). Le `maxDuration: 30` dans `vercel.json` suppose un plan
  payant ; réduisez à 10 si vous êtes sur le plan gratuit.
