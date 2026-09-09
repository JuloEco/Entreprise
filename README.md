# CorpSuite + Mini GitHub — déploiement Vercel

## 1. Base de données : Postgres (Neon) recommandé

`db_common.py` choisit automatiquement son backend, dans cet ordre :

1. **Postgres** (recommandé — ex : [Neon](https://neon.tech)) si `DATABASE_URL`
   (ou `POSTGRES_URL`) est défini. Vraie base persistante, partagée par
   toutes les invocations serverless de Vercel.
2. **Turso** (conservé pour compatibilité) si `TURSO_DATABASE_URL` est
   défini et qu'aucune base Postgres n'est configurée.
3. **SQLite local** en dernier recours, pour le développement sur votre
   machine uniquement. **Ne jamais utiliser ce mode en production sur
   Vercel** : `/tmp` n'est pas persistant entre les invocations.

Le reste du code (`entreprise.py`, `minigithub.py`) est resté écrit en SQL
"façon SQLite" (placeholders `?`, `INTEGER PRIMARY KEY AUTOINCREMENT`,
`INSERT OR REPLACE`) : `db_common.py` traduit ces requêtes à la volée vers
la syntaxe Postgres quand c'est ce backend qui est actif. Vous n'avez donc
rien à changer dans la logique métier existante.

**Schéma dédié `corpsuite`** — toutes les tables de l'application sont
créées dans un schéma Postgres dédié (`corpsuite`), pas dans `public`. Ça
évite toute collision si la même base Neon est aussi utilisée par un autre
projet à vous (par ex. si `users` existe déjà ailleurs avec un schéma
incompatible). **Dans l'explorateur de tables de Neon (ou via `psql`),
pensez à sélectionner le schéma `corpsuite`** pour voir les tables de cette
application — par défaut ces interfaces affichent `public`, qui restera
vide.

### Créer la base Neon

1. Créez un projet sur [neon.tech](https://neon.tech) (offre gratuite
   suffisante pour démarrer).
2. Copiez la chaîne de connexion fournie dans le tableau de bord Neon
   (elle ressemble à
   `postgresql://user:password@ep-xxx.neon.tech/dbname?sslmode=require`).
3. Ajoutez-la comme variable d'environnement `DATABASE_URL` dans les
   réglages du projet Vercel (Settings → Environment Variables).

### Créer les tables

`app.py` appelle déjà `init_db()` pour les deux applications à chaque
démarrage (cold start) : **les tables se créent automatiquement au premier
déploiement**, aucune étape manuelle n'est requise. Le script
`scripts/init_remote_db.py` reste disponible si vous préférez créer les
tables avant même le premier déploiement, ou forcer leur mise à jour après
un changement de schéma :

```bash
export DATABASE_URL="postgresql://user:password@ep-xxx.neon.tech/dbname?sslmode=require"
pip install -r requirements.txt
python scripts/init_remote_db.py
```

## 2. Nouvelles fonctionnalités CorpSuite

### Licenciement d'employés

- N'importe quel PDG peut licencier n'importe quel employé de son
  entreprise (sauf lui-même), depuis la page **Équipe**.
- Toute autre personne a besoin de la compétence **« Licencier des
  employés »**, accordée à son poste depuis **Postes → (un poste) →
  Permissions**.

### Personnalisation : qui peut licencier qui ?

Sur la page de gestion d'un poste (**Postes → (un poste)**), une fois la
compétence « Licencier des employés » cochée, une seconde section permet de
choisir **quels autres postes** ce poste a le droit de licencier. Par
exemple : le poste « Manager » peut être autorisé à licencier « Stagiaire »
mais pas « Développeur senior ». Le PDG n'est jamais concerné par cette
matrice : il peut toujours tout faire, et ne peut jamais être licencié par
ce biais.

### Suppression d'entreprise

Sur la page **Paramètres** d'une entreprise, dans la zone dangereuse, il
est possible de supprimer définitivement l'entreprise (protégé par la
permission **« Supprimer l'entreprise »**, comme toute autre permission —
le PDG l'a toujours). La suppression :

- efface les postes, permissions, comptes employés, projets et dépôts de
  code associés ;
- détache proprement les éventuelles filiales, qui redeviennent des
  entreprises indépendantes plutôt que d'être supprimées en cascade ;
- exige de retaper le nom exact de l'entreprise, pour éviter les
  suppressions accidentelles.

### Statuts d'entreprise (filiale / société mère)

Toujours sur la page **Paramètres**, une entreprise peut désormais être
déclarée filiale d'une autre entreprise de la plateforme (menu déroulant).
La fiche publique et le tableau de bord affichent alors la société mère et
la liste des filiales. Une protection empêche de créer une boucle de
filiation (ex : une filiale ne peut pas devenir la société mère de sa
propre société mère). Il s'agit d'une déclaration simple, sans processus
d'acceptation croisée entre les deux entreprises.

### Messagerie privée & discussion de projet

La messagerie compte désormais trois niveaux, comme prévu dans les
propositions d'évolution :

1. **Discussion d'espace** (déjà existante) — page **Messagerie**, visible
   par toute l'équipe.
2. **Messages privés** — nouvelle icône ✉️ dans l'en-tête (ou page
   **/messages**) : liste des conversations, démarrage d'une nouvelle
   conversation par identifiant, et bouton **« Envoyer un message »** sur
   le profil de chaque personne. Chacun ne peut supprimer que ses propres
   messages.
3. **Discussion de projet** — un onglet **Discussion** sur chaque carte de
   projet ouvre un fil de discussion propre à ce projet, séparé de la
   discussion générale de l'espace.

### Fichiers et documents

Chaque espace dispose désormais d'une page **Fichiers** (accessible depuis
la navigation, ou depuis un projet précis) permettant d'envoyer et de
télécharger des documents, images, audio, vidéo, archives ou code (3 Mo
maximum par fichier, stocké encodé dans la base de données — aucun disque
persistant n'étant disponible en environnement serverless). Un fichier peut
être rattaché à un projet précis ou rester un fichier général de l'espace.
Chacun peut supprimer les fichiers qu'il a envoyés ; la nouvelle compétence
**« Gérer les fichiers de l'espace »** (Postes → permissions) permet à un
poste de modérer l'ensemble des fichiers, y compris ceux envoyés par
d'autres.

### Système de modules de projet

Un projet n'est plus enfermé dans une seule catégorie : c'est un ensemble
composable de capacités (« modules »), conformément à la vision « le code
devient une fonctionnalité parmi d'autres ». Les modules disponibles sont
**Tâches**, **Discussion**, **Fichiers** et **Code**.

- À la création d'un projet, la catégorie choisie pré-coche un preset de
  modules (ex : *Art* pré-coche Fichiers/Discussion/Tâches, sans Code) ;
  la sélection reste entièrement modifiable avant validation.
- Chaque projet a désormais une page **Modules** (icône 🧩 sur sa carte)
  permettant d'activer ou de désactiver ses capacités à tout moment.
  Seuls le créateur du projet ou une personne disposant de la permission
  « Supprimer un projet » (qui fait déjà office de gestion de projet)
  peuvent modifier ces réglages ; les autres membres consultent la page
  en lecture seule.
- Un module désactivé masque le lien correspondant sur la carte projet
  **et** bloque l'accès direct à la page (redirection avec message
  explicite) — ce n'est donc pas qu'un habillage visuel.
- Désactiver un module ne supprime aucune donnée : les tâches, messages ou
  fichiers existants sont conservés et réapparaissent si le module est
  réactivé.
- Le dépôt Mini GitHub reste créé automatiquement à la création d'un
  projet (l'infrastructure sous-jacente), mais son onglet **Code**
  n'apparaît que si le module correspondant est activé — rendre la
  création du dépôt elle-même totalement optionnelle est une évolution
  ultérieure possible, pas encore faite.
- Les projets créés avant l'introduction de ce système gardent
  automatiquement toutes leurs capacités actuelles activées (migration de
  compatibilité au démarrage de l'application).

### Type de projet à la création

Le formulaire de création demande désormais **« Quel type de projet
créez-vous ? »**, sous forme de 8 cartes cliquables plutôt qu'un simple
menu déroulant de catégorie :

```
💻 Développement   🎮 Jeu vidéo   🎨 Création   🎵 Musique
✍️ Écriture        🏫 École       🤖 Recherche IA   🧩 Libre
```

Chaque carte affiche, à titre indicatif, l'éventail de capacités visé par
cette vision (ex : *Jeu vidéo* → « Tâches · Bugs · Assets · Maps · Code »).
Cliquer sur une carte pré-coche automatiquement les modules **déjà
implémentés** parmi eux (Tâches, Discussion, Fichiers, Code) dans la
section « Capacités activées » juste en dessous — entièrement modifiable
avant de valider. Les capacités mentionnées mais pas encore construites
(Bugs, Assets, Maps, Galerie, Versions, Morceaux, Paroles, Chapitres,
Personnages, Univers, Documents, Sources, Calendrier, Expériences,
Résultats, Dataset...) sont la feuille de route : elles arriveront comme
nouveaux modules au fur et à mesure, sans rien casser dans les projets
existants. Le type **🧩 Libre** ne pré-coche rien : c'est à vous de
choisir les modules voulus.

Sélectionner **💻 Développement** ne construit rien de nouveau : Code,
Issues et Pull Requests existent déjà via Mini GitHub (`/github`).

## 3. Nouvelles fonctionnalités Mini GitHub

### Renommer / supprimer un dépôt

Depuis la page d'un dépôt, un nouveau bouton **« Paramètres »** (visible par
l'administrateur du dépôt) permet :

- de **renommer** le dépôt (les anciens liens cessent de fonctionner) ;
- de le **supprimer définitivement** (code, branches, pull requests,
  collaborateurs), après avoir retapé son nom pour confirmer. Si le dépôt
  provient d'un projet CorpSuite, le lien projet ↔ dépôt est également
  retiré.

## 4. Étapes de déploiement

### a. Configurer les variables d'environnement sur Vercel

Dans les réglages du projet Vercel (Settings → Environment Variables) :

- `SHARED_SECRET_KEY` — une valeur aléatoire (`python -c "import secrets; print(secrets.token_hex(32))"`)
- `DATABASE_URL` — votre chaîne de connexion Neon (recommandé)
- `GEMINI_API_KEY` (optionnel, pour les résumés IA des pull requests)

### b. Déployer

```bash
vercel deploy
```

Vercel détecte automatiquement `app.py` à la racine comme point d'entrée
Python (via `requirements.txt`).

## 5. Développement local

Rien ne change : vous pouvez toujours lancer séparément
`python entreprise.py` (port 5001) et `python minigithub.py` (port 5000)
sans définir `DATABASE_URL` ni `TURSO_DATABASE_URL` — `db_common.py`
retombe alors sur un fichier `minigithub.db` local, exactement comme avant.

Pour tester la version combinée (celle réellement utilisée sur Vercel) :

```bash
python app.py     # http://127.0.0.1:3000  (/  et /github)
```

Ce lancement local (`python app.py`) passe maintenant par l'API de Flask
(`Flask.run`) plutôt que par un appel direct à `werkzeug.serving.run_simple` :
`requirements.txt` n'épingle donc plus `Werkzeug` séparément — Flask gère
sa propre dépendance à Werkzeug. Note : combiner les deux applications
Flask en une seule (`DispatcherMiddleware`) reste nécessaire et est la
méthode que Flask lui-même recommande pour ce cas ("Application
Dispatching") ; il n'existe pas d'alternative purement Flask pour monter
deux applications complètes à des préfixes différents sans passer par cet
outil de Werkzeug.

## 6. Points à vérifier vous-même avant mise en prod

- **Neon en veille** : sur l'offre gratuite, une base Neon inactive se met
  en pause et prend quelques centaines de ms à se "réveiller" à la première
  requête après une période d'inactivité — normal, pas un bug.
- **Filiation d'entreprise** : la déclaration filiale/société mère est
  unilatérale (pas de confirmation demandée à l'autre entreprise). À adapter
  si vous avez besoin d'un vrai processus d'acceptation.
- **Limite de durée** : le plan gratuit Vercel limite l'exécution à 10s
  (60s en Pro). Le `maxDuration: 30` dans `vercel.json` suppose un plan
  payant ; réduisez à 10 si vous êtes sur le plan gratuit.
- **Turso** : si vous migrez depuis un déploiement Turso existant vers
  Postgres, pensez à exporter vos données avant de basculer `DATABASE_URL`
  — les deux backends ne partagent pas les mêmes données.
