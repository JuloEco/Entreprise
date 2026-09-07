# -*- coding: utf-8 -*-
"""
CorpSuite — Plateforme de gestion de mini-entreprise
=====================================================

Ce module cohabite avec `github.py` (Mini GitHub Pro) : les deux applications
partagent le même fichier de base de données SQLite (`minigithub.db`).

- `github.py` gère les dépôts de code, branches, fichiers, pull requests.
- `entreprise.py` (ce fichier) ajoute la couche "entreprise" par dessus :
  comptes, postes, permissions, messagerie interne, et création de projets
  qui génèrent automatiquement un dépôt Git dans Mini GitHub.

Comment lancer les deux ensemble :
    Terminal 1 :  python github.py       -> http://127.0.0.1:5000
    Terminal 2 :  python entreprise.py   -> http://127.0.0.1:5001

Les comptes créés ici fonctionnent aussi sur Mini GitHub (même table
`users`, même hachage de mot de passe), et chaque entreprise créée ici
ajoute automatiquement une succursale dans Mini GitHub.
"""

import os
import sqlite3
import hashlib
import secrets
import string
from functools import wraps
from datetime import datetime
from flask import (
    Flask, render_template_string, request, redirect,
    url_for, session, flash, g
)

import db_common

app = Flask(__name__)
# Doit être IDENTIQUE au secret_key de minigithub.py pour que la session
# (connexion) soit partagée entre les deux applications sur Vercel.
app.secret_key = os.environ.get('SHARED_SECRET_KEY', 'dev-key-change-me')
# En local : les deux apps tournent sur des ports séparés -> URL absolue.
# Sur Vercel : les deux apps sont servies sous le même domaine, Mini GitHub
# étant monté sous le préfixe /github (voir le app.py combiné) -> chemin relatif.
MINIGITHUB_URL = os.environ.get('MINIGITHUB_URL', '/github')

# ---------------------------------------------------------------------------
# Base de données
# ---------------------------------------------------------------------------

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = db_common.open_connection()
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def hash_password(password):
    # Même algorithme que Mini GitHub -> les comptes sont utilisables partout
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

PERMISSIONS = {
    'create_account':     {'label': 'Créer des comptes employés',        'icon': 'user-plus'},
    'delete_account':     {'label': 'Supprimer des comptes employés',    'icon': 'user-x'},
    'create_poste':       {'label': 'Créer des postes',                  'icon': 'briefcase'},
    'manage_permissions': {'label': 'Gérer les permissions des postes',  'icon': 'shield'},
    'assign_poste':       {'label': "Attribuer un poste à un employé",   'icon': 'user-cog'},
    'create_project':     {'label': 'Créer des projets (dépôts de code)','icon': 'folder-plus'},
    'delete_project':     {'label': 'Supprimer des projets',             'icon': 'trash-2'},
    'manage_messaging':   {'label': "Modérer la messagerie d'espace",'icon': 'message-square'},
    'edit_company':       {'label': "Modifier la fiche de l'espace",      'icon': 'edit-3'},
    'fire_employee':      {'label': 'Licencier des employés',            'icon': 'user-minus'},
    'delete_company':     {'label': "Supprimer l'espace",            'icon': 'flame'},
}

def init_db():
    with app.app_context():
        db = get_db()
        cur = db.cursor()

        # --- Tables partagées avec Mini GitHub (créées seulement si absentes) ---
        cur.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            avatar_color TEXT NOT NULL DEFAULT '#6366f1',
            branch_office_id INTEGER
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS branch_offices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            location TEXT NOT NULL,
            description TEXT,
            admin_id INTEGER NOT NULL
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS repositories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            owner_id INTEGER NOT NULL,
            branch_office_id INTEGER,
            is_private INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id INTEGER NOT NULL,
            branch_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            content TEXT NOT NULL,
            language TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # Colonnes ajoutées après coup (bases existantes) : on tente l'ALTER
        # TABLE et on ignore l'erreur si la colonne existe déjà. Le except
        # générique (plutôt que sqlite3.OperationalError) et le rollback()
        # sont nécessaires pour rester compatible avec Postgres, où une
        # instruction en échec doit être suivie d'un rollback avant de
        # pouvoir continuer sur la même connexion.
        try:
            cur.execute("ALTER TABLE users ADD COLUMN must_setup_account INTEGER DEFAULT 0")
            db.commit()
        except Exception:
            db.rollback()

        # Colonnes du profil de créateur (Priorité 4) : identité personnelle
        # orientée création, pas recrutement.
        for col_def in [
            "display_name TEXT", "bio TEXT", "skills TEXT", "interests TEXT",
            "available_for_projects INTEGER DEFAULT 0"
        ]:
            try:
                cur.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
                db.commit()
            except Exception:
                db.rollback()

        # --- Tables propres à CorpSuite ---
        cur.execute('''CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            logo TEXT DEFAULT '🏢',
            homepage_text TEXT,
            branch_office_id INTEGER NOT NULL,
            pdg_user_id INTEGER NOT NULL,
            parent_company_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (branch_office_id) REFERENCES branch_offices (id),
            FOREIGN KEY (pdg_user_id) REFERENCES users (id),
            FOREIGN KEY (parent_company_id) REFERENCES companies (id)
        )''')
        # Idem pour les bases créées avant l'ajout du statut de filiation.
        try:
            cur.execute("ALTER TABLE companies ADD COLUMN parent_company_id INTEGER")
            db.commit()
        except Exception:
            db.rollback()
        # Type d'espace (Priorité 6) : un espace n'est pas forcément une
        # "entreprise" — groupe d'amis, studio de jeux, équipe scolaire...
        try:
            cur.execute("ALTER TABLE companies ADD COLUMN space_type TEXT DEFAULT 'entreprise'")
            db.commit()
        except Exception:
            db.rollback()
        cur.execute('''CREATE TABLE IF NOT EXISTS postes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            color TEXT DEFAULT '#6366f1',
            is_pdg INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (company_id) REFERENCES companies (id)
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS poste_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poste_id INTEGER NOT NULL,
            permission_key TEXT NOT NULL,
            FOREIGN KEY (poste_id) REFERENCES postes (id)
        )''')
        # Personnalisation de la compétence "licencier" : quels postes un
        # poste donné a-t-il le droit de licencier ? (le PDG, lui, peut
        # toujours tout licencier, sans passer par cette table).
        cur.execute('''CREATE TABLE IF NOT EXISTS poste_fire_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poste_id INTEGER NOT NULL,
            target_poste_id INTEGER NOT NULL,
            FOREIGN KEY (poste_id) REFERENCES postes (id),
            FOREIGN KEY (target_poste_id) REFERENCES postes (id)
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            poste_id INTEGER NOT NULL,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (company_id) REFERENCES companies (id),
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (poste_id) REFERENCES postes (id)
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            repo_id INTEGER NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (company_id) REFERENCES companies (id),
            FOREIGN KEY (repo_id) REFERENCES repositories (id),
            FOREIGN KEY (created_by) REFERENCES users (id)
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS company_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (company_id) REFERENCES companies (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )''')

        # Colonnes "projet universel" ajoutées après coup sur des bases
        # existantes : titre libre, catégorie, statut de cycle de vie,
        # emoji d'illustration, liens externes, visibilité.
        for col_def in [
            "title TEXT", "category TEXT DEFAULT 'Autre'", "status TEXT DEFAULT 'idee'",
            "image TEXT DEFAULT '🚀'", "links TEXT", "visibility TEXT DEFAULT 'prive'"
        ]:
            try:
                cur.execute(f"ALTER TABLE projects ADD COLUMN {col_def}")
                db.commit()
            except Exception:
                db.rollback()

        # --- Priorité 2 : système d'idées ---
        cur.execute('''CREATE TABLE IF NOT EXISTS ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            converted_project_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (company_id) REFERENCES companies (id),
            FOREIGN KEY (author_id) REFERENCES users (id)
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS idea_likes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idea_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            FOREIGN KEY (idea_id) REFERENCES ideas (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS idea_saves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idea_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            FOREIGN KEY (idea_id) REFERENCES ideas (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS idea_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idea_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (idea_id) REFERENCES ideas (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )''')

        # --- Priorité 3 : tableau de tâches ---
        cur.execute('''CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            company_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'a_faire',
            priority TEXT DEFAULT 'normale',
            assignee_id INTEGER,
            created_by INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects (id),
            FOREIGN KEY (company_id) REFERENCES companies (id),
            FOREIGN KEY (assignee_id) REFERENCES users (id),
            FOREIGN KEY (created_by) REFERENCES users (id)
        )''')
        # --- Priorité 5 : fil d'activité ---
        cur.execute('''CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            actor_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (company_id) REFERENCES companies (id),
            FOREIGN KEY (actor_id) REFERENCES users (id)
        )''')
        db.commit()

SPACE_TYPES = {
    'amis':        {'label': 'Groupe d\'amis',      'icon': '👥'},
    'studio':      {'label': 'Studio de jeux',      'icon': '🎮'},
    'ecole':       {'label': 'Équipe scolaire',     'icon': '🏫'},
    'collectif':   {'label': 'Collectif créatif',   'icon': '🎨'},
    'ia':          {'label': 'Équipe IA',           'icon': '🤖'},
    'entreprise':  {'label': 'Petite entreprise',   'icon': '🏢'},
    'communaute':  {'label': 'Projet communautaire','icon': '🚀'},
}

def log_activity(company_id, actor_id, activity_type, text):
    db = get_db()
    db.execute("INSERT INTO activities (company_id, actor_id, type, text) VALUES (?, ?, ?, ?)",
               (company_id, actor_id, activity_type, text))
    db.commit()

PROJECT_CATEGORIES = ['Code', 'Jeu', 'Art', 'École', 'IA', 'Musique', 'Écriture', 'Entrepreneuriat', 'Autre']
PROJECT_STATUSES = [
    ('idee', '💡 Idée'), ('planification', '📝 Planification'), ('construction', '🔨 Construction'),
    ('tests', '🧪 Tests'), ('publie', '🚀 Publié'),
]
TASK_STATUSES = [
    ('a_faire', '📋 À faire'), ('en_cours', '🔨 En cours'),
    ('a_tester', '🧪 À tester'), ('termine', '✅ Terminé'),
]

# ---------------------------------------------------------------------------
# Fonctions utilitaires
# ---------------------------------------------------------------------------

def random_username():
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(6))

def random_password():
    upper, lower, digits = string.ascii_uppercase, string.ascii_lowercase, string.digits
    symbols = "!@#$%&*-_+="
    pool = upper + lower + digits + symbols
    pw = [secrets.choice(upper), secrets.choice(lower), secrets.choice(digits), secrets.choice(symbols)]
    pw += [secrets.choice(pool) for _ in range(9)]
    secrets.SystemRandom().shuffle(pw)
    return ''.join(pw)

def get_company(company_id):
    return get_db().execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()

def get_membership(company_id):
    if not session.get('user_id'):
        return None
    return get_db().execute("""
        SELECT e.*, p.name AS poste_name, p.is_pdg, p.color AS poste_color
        FROM employees e JOIN postes p ON e.poste_id = p.id
        WHERE e.company_id = ? AND e.user_id = ?
    """, (company_id, session['user_id'])).fetchone()

def has_perm(company_id, key):
    m = get_membership(company_id)
    if not m:
        return False
    if m['is_pdg']:
        return True
    r = get_db().execute(
        "SELECT 1 FROM poste_permissions WHERE poste_id = ? AND permission_key = ?",
        (m['poste_id'], key)
    ).fetchone()
    return bool(r)

def can_fire(company_id, target_user_id):
    """Est-ce que la personne connectée a le droit de licencier
    `target_user_id` de `company_id` ?

    Le PDG peut toujours licencier n'importe qui (sauf lui-même). Toute
    autre personne doit disposer de la compétence 'fire_employee' ET son
    poste doit avoir été explicitement autorisé (via poste_fire_targets) à
    licencier le poste occupé par la cible. Le PDG, lui, ne peut jamais
    être licencié par ce biais.
    """
    if not session.get('user_id') or target_user_id == session.get('user_id'):
        return False
    actor = get_membership(company_id)
    if not actor:
        return False
    target = get_db().execute("""
        SELECT p.id AS poste_id, p.is_pdg FROM employees e JOIN postes p ON e.poste_id = p.id
        WHERE e.company_id = ? AND e.user_id = ?
    """, (company_id, target_user_id)).fetchone()
    if not target or target['is_pdg']:
        return False
    if actor['is_pdg']:
        return True
    if not has_perm(company_id, 'fire_employee'):
        return False
    allowed = get_db().execute(
        "SELECT 1 FROM poste_fire_targets WHERE poste_id = ? AND target_poste_id = ?",
        (actor['poste_id'], target['poste_id'])
    ).fetchone()
    return bool(allowed)

def get_company_children(company_id):
    return get_db().execute(
        "SELECT * FROM companies WHERE parent_company_id = ? ORDER BY name", (company_id,)
    ).fetchall()

def is_descendant(candidate_id, ancestor_id):
    """True si `candidate_id` est déjà une filiale (directe ou indirecte)
    de `ancestor_id` — auquel cas désigner `candidate_id` comme société
    mère de `ancestor_id` créerait une boucle de filiation."""
    current = candidate_id
    seen = set()
    while current is not None and current not in seen:
        seen.add(current)
        row = get_db().execute("SELECT parent_company_id FROM companies WHERE id = ?", (current,)).fetchone()
        current = row['parent_company_id'] if row else None
        if current == ancestor_id:
            return True
    return False

def get_my_companies():
    if not session.get('user_id'):
        return []
    return get_db().execute("""
        SELECT c.*, p.name AS poste_name, p.is_pdg, p.color AS poste_color
        FROM employees e JOIN companies c ON e.company_id = c.id JOIN postes p ON e.poste_id = p.id
        WHERE e.user_id = ? ORDER BY c.name
    """, (session['user_id'],)).fetchall()

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('user_id'):
            flash('Veuillez vous connecter pour continuer.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

def member_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login'))
        company_id = kwargs.get('company_id')
        if not get_membership(company_id):
            flash("Vous n'êtes pas membre de cette entreprise.", 'error')
            return redirect(url_for('my_companies'))
        return f(*args, **kwargs)
    return wrapper

def permission_required(key):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get('user_id'):
                return redirect(url_for('login'))
            company_id = kwargs.get('company_id')
            if not has_perm(company_id, key):
                flash("Permission refusée : votre poste ne dispose pas de cette autorisation.", 'error')
                return redirect(url_for('company_dashboard', company_id=company_id))
            return f(*args, **kwargs)
        return wrapper
    return decorator

@app.before_request
def enforce_account_setup():
    if session.get('user_id') and request.endpoint not in ('setup_account', 'logout', 'static'):
        u = get_db().execute("SELECT must_setup_account FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        if u and u['must_setup_account']:
            return redirect(url_for('setup_account'))

# ---------------------------------------------------------------------------
# Design system (gabarits communs)
# ---------------------------------------------------------------------------

APP_HEADER = """
<!DOCTYPE html>
<html lang="fr" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CorpSuite — Plateforme d'espaces créatifs</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/lucide@latest"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    fontFamily: { sans: ['Inter', 'sans-serif'] },
                    colors: {
                        csBg: '#0b0e14', csCard: '#12161f', csCard2: '#171c28',
                        csBorder: '#232a38', csText: '#dbe1ea', csMuted: '#8993a4',
                        csIndigo: '#6366f1', csIndigoHover: '#4f52e0'
                    }
                }
            }
        }
    </script>
    <style>
        body { font-feature-settings: 'ss01'; }
        .custom-scrollbar::-webkit-scrollbar { width: 6px; }
        .custom-scrollbar::-webkit-scrollbar-track { background: transparent; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: #232a38; border-radius: 3px; }
        .brand-gradient { background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%); }
    </style>
</head>
<body class="bg-csBg text-csText font-sans min-h-screen flex flex-col antialiased">
    <header class="bg-csCard/80 backdrop-blur border-b border-csBorder sticky top-0 z-40">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between gap-2">
            <div class="flex items-center gap-6 min-w-0">
                <a href="{{ url_for('landing') }}" class="flex items-center gap-2.5 text-white font-extrabold text-lg shrink-0">
                    <span class="brand-gradient w-8 h-8 rounded-lg flex items-center justify-center shadow-lg shadow-indigo-500/20 shrink-0">
                        <i data-lucide="building-2" class="w-4.5 h-4.5 text-white"></i>
                    </span>
                    <span>Corp<span class="text-csIndigo">Suite</span></span>
                </a>
                {% if company %}
                <div class="hidden md:flex items-center gap-1 text-sm font-medium border-l border-csBorder pl-6">
                    <a href="{{ url_for('company_dashboard', company_id=company.id) }}" class="px-3 py-2 rounded-lg hover:text-white hover:bg-csBorder/50 transition flex items-center gap-1.5">
                        <i data-lucide="layout-dashboard" class="w-4 h-4 text-csMuted"></i> Tableau de bord
                    </a>
                    <a href="{{ url_for('messagerie', company_id=company.id) }}" class="px-3 py-2 rounded-lg hover:text-white hover:bg-csBorder/50 transition flex items-center gap-1.5">
                        <i data-lucide="message-square" class="w-4 h-4 text-csMuted"></i> Messagerie
                    </a>
                    <a href="{{ url_for('equipe', company_id=company.id) }}" class="px-3 py-2 rounded-lg hover:text-white hover:bg-csBorder/50 transition flex items-center gap-1.5">
                        <i data-lucide="users" class="w-4 h-4 text-csMuted"></i> Équipe
                    </a>
                    <a href="{{ url_for('postes', company_id=company.id) }}" class="px-3 py-2 rounded-lg hover:text-white hover:bg-csBorder/50 transition flex items-center gap-1.5">
                        <i data-lucide="briefcase" class="w-4 h-4 text-csMuted"></i> Postes
                    </a>
                    <a href="{{ url_for('projets', company_id=company.id) }}" class="px-3 py-2 rounded-lg hover:text-white hover:bg-csBorder/50 transition flex items-center gap-1.5">
                        <i data-lucide="folder-git-2" class="w-4 h-4 text-csMuted"></i> Projets
                    </a>
                    <a href="{{ url_for('idees', company_id=company.id) }}" class="px-3 py-2 rounded-lg hover:text-white hover:bg-csBorder/50 transition flex items-center gap-1.5">
                        <i data-lucide="lightbulb" class="w-4 h-4 text-csMuted"></i> Idées
                    </a>
                    <a href="{{ url_for('activity_feed', company_id=company.id) }}" class="px-3 py-2 rounded-lg hover:text-white hover:bg-csBorder/50 transition flex items-center gap-1.5">
                        <i data-lucide="newspaper" class="w-4 h-4 text-csMuted"></i> Fil
                    </a>
                </div>
                {% endif %}
            </div>
            <div class="flex items-center gap-2 sm:gap-3">
                {% if session.get('user_id') %}
                    {% if my_companies and my_companies|length > 1 %}
                    <div class="relative group hidden sm:block">
                        <button class="text-xs font-semibold px-3 py-2 rounded-lg border border-csBorder hover:bg-csBorder/40 flex items-center gap-1.5 text-csMuted">
                            <i data-lucide="building" class="w-3.5 h-3.5"></i> Changer d'espace
                        </button>
                        <div class="absolute right-0 mt-1 w-56 bg-csCard2 border border-csBorder rounded-xl shadow-2xl hidden group-hover:block overflow-hidden">
                            {% for c in my_companies %}
                            <a href="{{ url_for('company_dashboard', company_id=c.id) }}" class="flex items-center gap-2 px-3.5 py-2.5 text-xs hover:bg-csBorder/40 text-csText">
                                <span>{{ c.logo }}</span> <span class="font-medium">{{ c.name }}</span>
                            </a>
                            {% endfor %}
                        </div>
                    </div>
                    {% endif %}
                    <div class="flex items-center gap-2 text-xs">
                        <a href="{{ url_for('profil', username=session.get('username')) }}" class="flex items-center gap-2 hover:opacity-80 transition">
                            <div class="w-7 h-7 rounded-full flex items-center justify-center font-bold text-white text-xs uppercase shrink-0" style="background-color: {{ session.get('avatar_color', '#6366f1') }}">
                                {{ session.get('username')[0] }}
                            </div>
                            <span class="font-medium text-white hidden sm:inline">{{ session.get('username') }}</span>
                        </a>
                    </div>
                    <a href="{{ url_for('logout') }}" class="text-csMuted hover:text-red-400 p-2 rounded-lg hover:bg-csBorder/40 transition hidden sm:inline-flex" title="Déconnexion">
                        <i data-lucide="log-out" class="w-4 h-4"></i>
                    </a>
                {% else %}
                    <a href="{{ url_for('login') }}" class="text-sm font-semibold text-csText hover:text-white px-2 sm:px-3 py-2">Connexion</a>
                    <a href="{{ url_for('found_company') }}" class="bg-csIndigo hover:bg-csIndigoHover text-white text-xs sm:text-sm font-semibold px-3 sm:px-4 py-2 rounded-lg transition shadow-lg shadow-indigo-500/20 whitespace-nowrap"><span class="sm:hidden">Fonder</span><span class="hidden sm:inline">Fonder mon espace</span></a>
                {% endif %}
                {% if company or session.get('user_id') %}
                <button id="mobileMenuBtn" onclick="document.getElementById('mobileMenu').classList.toggle('hidden'); document.getElementById('menuIconOpen').classList.toggle('hidden'); document.getElementById('menuIconClose').classList.toggle('hidden');" class="md:hidden p-2 rounded-lg hover:bg-csBorder/40 text-csText" aria-label="Ouvrir le menu">
                    <i data-lucide="menu" id="menuIconOpen" class="w-5 h-5"></i>
                    <i data-lucide="x" id="menuIconClose" class="w-5 h-5 hidden"></i>
                </button>
                {% endif %}
            </div>
        </div>
        <div id="mobileMenu" class="hidden md:hidden border-t border-csBorder bg-csCard px-4 py-3 space-y-1">
            {% if company %}
            <a href="{{ url_for('company_dashboard', company_id=company.id) }}" class="px-3 py-2.5 rounded-lg hover:text-white hover:bg-csBorder/50 transition flex items-center gap-2 text-sm font-medium">
                <i data-lucide="layout-dashboard" class="w-4 h-4 text-csMuted"></i> Tableau de bord
            </a>
            <a href="{{ url_for('messagerie', company_id=company.id) }}" class="px-3 py-2.5 rounded-lg hover:text-white hover:bg-csBorder/50 transition flex items-center gap-2 text-sm font-medium">
                <i data-lucide="message-square" class="w-4 h-4 text-csMuted"></i> Messagerie
            </a>
            <a href="{{ url_for('equipe', company_id=company.id) }}" class="px-3 py-2.5 rounded-lg hover:text-white hover:bg-csBorder/50 transition flex items-center gap-2 text-sm font-medium">
                <i data-lucide="users" class="w-4 h-4 text-csMuted"></i> Équipe
            </a>
            <a href="{{ url_for('postes', company_id=company.id) }}" class="px-3 py-2.5 rounded-lg hover:text-white hover:bg-csBorder/50 transition flex items-center gap-2 text-sm font-medium">
                <i data-lucide="briefcase" class="w-4 h-4 text-csMuted"></i> Postes
            </a>
            <a href="{{ url_for('projets', company_id=company.id) }}" class="px-3 py-2.5 rounded-lg hover:text-white hover:bg-csBorder/50 transition flex items-center gap-2 text-sm font-medium">
                <i data-lucide="folder-git-2" class="w-4 h-4 text-csMuted"></i> Projets
            </a>
            <a href="{{ url_for('idees', company_id=company.id) }}" class="px-3 py-2.5 rounded-lg hover:text-white hover:bg-csBorder/50 transition flex items-center gap-2 text-sm font-medium">
                <i data-lucide="lightbulb" class="w-4 h-4 text-csMuted"></i> Idées
            </a>
            <a href="{{ url_for('activity_feed', company_id=company.id) }}" class="px-3 py-2.5 rounded-lg hover:text-white hover:bg-csBorder/50 transition flex items-center gap-2 text-sm font-medium">
                <i data-lucide="newspaper" class="w-4 h-4 text-csMuted"></i> Fil
            </a>
            {% endif %}
            {% if session.get('user_id') and my_companies and my_companies|length > 1 %}
            <div class="pt-2 mt-2 border-t border-csBorder">
                <p class="px-3 text-[10px] uppercase font-semibold text-csMuted mb-1">Changer d'espace</p>
                {% for c in my_companies %}
                <a href="{{ url_for('company_dashboard', company_id=c.id) }}" class="px-3 py-2 rounded-lg hover:bg-csBorder/50 transition flex items-center gap-2 text-sm">
                    <span>{{ c.logo }}</span> <span class="font-medium">{{ c.name }}</span>
                </a>
                {% endfor %}
            </div>
            {% endif %}
            {% if session.get('user_id') %}
            <a href="{{ url_for('profil', username=session.get('username')) }}" class="px-3 py-2.5 rounded-lg hover:bg-csBorder/50 transition flex items-center gap-2 text-sm font-medium">
                <i data-lucide="user-round" class="w-4 h-4 text-csMuted"></i> Mon profil
            </a>
            <a href="{{ url_for('logout') }}" class="px-3 py-2.5 rounded-lg hover:bg-csBorder/50 transition flex items-center gap-2 text-sm font-medium text-red-400 border-t border-csBorder mt-2 pt-3">
                <i data-lucide="log-out" class="w-4 h-4"></i> Déconnexion
            </a>
            {% endif %}
        </div>
    </header>
    <main class="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                <div class="mb-6 space-y-2">
                    {% for category, msg in messages %}
                        <div class="p-3.5 rounded-xl border text-sm flex items-center gap-3 {% if category == 'error' %}bg-red-950/40 border-red-800 text-red-200{% else %}bg-emerald-950/40 border-emerald-800 text-emerald-200{% endif %}">
                            <i data-lucide="{% if category == 'error' %}alert-circle{% else %}check-circle-2{% endif %}" class="w-5 h-5 shrink-0"></i>
                            <span>{{ msg }}</span>
                        </div>
                    {% endfor %}
                </div>
            {% endif %}
        {% endwith %}
"""

APP_FOOTER = """
    </main>
    <footer class="bg-csCard border-t border-csBorder py-5 mt-auto">
        <div class="max-w-7xl mx-auto px-4 text-center text-xs text-csMuted flex flex-col sm:flex-row justify-between items-center gap-2">
            <div>CorpSuite &copy; 2026 — Comptes, postes, permissions &amp; messagerie d'espace</div>
            <div class="flex gap-4 items-center">
                <span class="flex items-center gap-1.5"><i data-lucide="link" class="w-3.5 h-3.5"></i> Relié à Mini GitHub Pro</span>
            </div>
        </div>
    </footer>
    <script>lucide.createIcons();</script>
</body>
</html>
"""

CARD = "bg-csCard border border-csBorder rounded-2xl"

# ---------------------------------------------------------------------------
# Landing / annuaire public
# ---------------------------------------------------------------------------

LANDING_TEMPLATE = APP_HEADER + """
<div class="text-center max-w-3xl mx-auto mb-14 mt-6">
    <div class="inline-flex items-center gap-2 text-xs font-semibold text-csIndigo bg-csIndigo/10 border border-csIndigo/30 px-3 py-1.5 rounded-full mb-5">
        <i data-lucide="sparkles" class="w-3.5 h-3.5"></i> Comptes · Postes · Permissions · Messagerie · Code
    </div>
    <h1 class="text-4xl sm:text-5xl font-extrabold text-white tracking-tight leading-tight">L'espace pour<br> imaginer et construire vos projets</h1>
    <p class="text-csMuted mt-5 text-base leading-relaxed">Créez votre espace, définissez des rôles avec des permissions précises, distribuez des comptes à vos membres et lancez des projets — code, jeu, art, école, musique... tout y a sa place.</p>
    <div class="flex items-center justify-center gap-3 mt-8">
        <a href="{{ url_for('found_company') }}" class="bg-csIndigo hover:bg-csIndigoHover text-white font-semibold px-6 py-3 rounded-xl transition shadow-lg shadow-indigo-500/25 flex items-center gap-2">
            <i data-lucide="rocket" class="w-4 h-4"></i> Fonder mon espace
        </a>
        <a href="{{ url_for('login') }}" class="border border-csBorder hover:bg-csBorder/40 text-csText font-semibold px-6 py-3 rounded-xl transition">J'ai déjà un compte</a>
    </div>
</div>

<h2 class="text-lg font-bold text-white mb-4 flex items-center gap-2"><i data-lucide="compass" class="w-5 h-5 text-csIndigo"></i> Espaces sur la plateforme</h2>
<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
    {% for c in companies %}
    <a href="{{ url_for('company_public', company_id=c.id) }}" class="__CARD__ p-5 hover:border-csIndigo/50 transition group">
        <div class="flex items-center gap-3 mb-3">
            <div class="w-11 h-11 rounded-xl bg-csCard2 border border-csBorder flex items-center justify-center text-xl">{{ c.logo }}</div>
            <div>
                <h3 class="font-bold text-white group-hover:text-csIndigo transition">{{ c.name }}</h3>
                <p class="text-[11px] text-csMuted">{{ c.member_count }} membre(s) · {{ c.project_count }} projet(s)</p>
            </div>
        </div>
        <span class="inline-block text-[10px] font-semibold px-2 py-0.5 rounded-full bg-csCard2 border border-csBorder text-csMuted mb-2">{{ space_types.get(c.space_type, space_types['entreprise']).icon }} {{ space_types.get(c.space_type, space_types['entreprise']).label }}</span>
        <p class="text-xs text-csMuted line-clamp-2">{{ c.description or "Aucune description." }}</p>
    </a>
    {% else %}
    <div class="col-span-full __CARD__ p-10 text-center text-csMuted">
        <i data-lucide="building" class="w-10 h-10 mx-auto mb-3 opacity-40"></i>
        <p>Aucun espace pour le moment. Soyez le premier à en créer un !</p>
    </div>
    {% endfor %}
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

COMPANY_PUBLIC_TEMPLATE = APP_HEADER + """
<div class="max-w-3xl mx-auto">
    <div class="__CARD__ p-8 mb-6">
        <div class="flex items-center gap-4">
            <div class="w-16 h-16 rounded-2xl bg-csCard2 border border-csBorder flex items-center justify-center text-3xl">{{ c.logo }}</div>
            <div>
                <h1 class="text-2xl font-bold text-white flex items-center gap-2">{{ c.name }}
                    <span class="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-csCard2 border border-csBorder text-csMuted align-middle">{{ space_types.get(c.space_type, space_types['entreprise']).icon }} {{ space_types.get(c.space_type, space_types['entreprise']).label }}</span>
                </h1>
                <p class="text-xs text-csMuted mt-1">{{ member_count }} membre(s) · {{ project_count }} projet(s) actif(s) · Fondée le {{ c.created_at.split(' ')[0] }}</p>
                {% if parent %}
                <p class="text-xs text-csMuted mt-1 flex items-center gap-1">
                    <i data-lucide="corner-left-up" class="w-3 h-3"></i> Filiale de
                    <a href="{{ url_for('company_public', company_id=parent.id) }}" class="text-csIndigo hover:underline font-medium">{{ parent.name }}</a>
                </p>
                {% endif %}
                {% if children %}
                <p class="text-xs text-csMuted mt-1 flex items-center gap-1 flex-wrap">
                    <i data-lucide="corner-right-down" class="w-3 h-3"></i> Société mère de :
                    {% for ch in children %}
                    <a href="{{ url_for('company_public', company_id=ch.id) }}" class="text-csIndigo hover:underline font-medium">{{ ch.name }}</a>{% if not loop.last %},{% endif %}
                    {% endfor %}
                </p>
                {% endif %}
            </div>
        </div>
        {% if c.description %}<p class="text-sm text-csText mt-6">{{ c.description }}</p>{% endif %}
        {% if c.homepage_text %}
        <div class="mt-6 pt-6 border-t border-csBorder text-sm text-csText leading-relaxed whitespace-pre-line">{{ c.homepage_text }}</div>
        {% endif %}
    </div>

    <div class="__CARD__ p-6">
        <h3 class="font-bold text-white text-sm mb-4 flex items-center gap-2"><i data-lucide="briefcase" class="w-4 h-4 text-csIndigo"></i> Postes de l'espace</h3>
        <div class="flex flex-wrap gap-2">
            {% for p in postes %}
            <span class="text-xs font-semibold px-3 py-1.5 rounded-full border" style="border-color: {{ p.color }}55; color: {{ p.color }}; background-color: {{ p.color }}15;">{{ p.name }}{% if p.is_pdg %} 👑{% endif %}</span>
            {% else %}
            <span class="text-xs text-csMuted">Aucun poste public pour le moment.</span>
            {% endfor %}
        </div>
    </div>
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

# ---------------------------------------------------------------------------
# Authentification
# ---------------------------------------------------------------------------

LOGIN_TEMPLATE = APP_HEADER + """
<div class="max-w-md mx-auto my-10 __CARD__ p-8 shadow-2xl">
    <div class="text-center mb-8">
        <div class="inline-flex p-3 rounded-2xl brand-gradient mb-3 shadow-lg shadow-indigo-500/20">
            <i data-lucide="lock-keyhole" class="w-8 h-8 text-white"></i>
        </div>
        <h1 class="text-2xl font-bold text-white">Connexion</h1>
        <p class="text-sm text-csMuted mt-1">Utilisez les identifiants fournis par votre PDG, ou les vôtres si vous avez fondé un espace.</p>
    </div>
    <form method="POST" class="space-y-4">
        <div>
            <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Identifiant</label>
            <input type="text" name="username" required autofocus class="w-full bg-csCard2 border border-csBorder rounded-lg px-3.5 py-2.5 text-white focus:outline-none focus:border-csIndigo text-sm">
        </div>
        <div>
            <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Mot de passe</label>
            <input type="password" name="password" required class="w-full bg-csCard2 border border-csBorder rounded-lg px-3.5 py-2.5 text-white focus:outline-none focus:border-csIndigo text-sm">
        </div>
        <button type="submit" class="w-full bg-csIndigo hover:bg-csIndigoHover text-white font-semibold py-2.5 rounded-lg text-sm transition shadow-lg shadow-indigo-500/20">Se connecter</button>
    </form>
    <div class="mt-6 pt-6 border-t border-csBorder text-center text-xs text-csMuted">
        Pas encore d'espace ? <a href="{{ url_for('found_company') }}" class="text-csIndigo hover:underline font-semibold">Créez le vôtre</a>
    </div>
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

# La "popup" de première connexion : bloque toute navigation tant que
# l'employé n'a pas choisi son identifiant et son mot de passe définitifs.
SETUP_ACCOUNT_TEMPLATE = APP_HEADER + """
<div class="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
    <div class="__CARD__ max-w-md w-full p-8 shadow-2xl border-csIndigo/40">
        <div class="text-center mb-6">
            <div class="inline-flex p-3 rounded-2xl brand-gradient mb-3 shadow-lg shadow-indigo-500/20">
                <i data-lucide="key-round" class="w-8 h-8 text-white"></i>
            </div>
            <h1 class="text-xl font-bold text-white">Bienvenue chez {{ company_name }} 👋</h1>
            <p class="text-sm text-csMuted mt-2">Vous vous êtes connecté avec un identifiant temporaire. Choisissez maintenant votre pseudo et votre mot de passe définitifs pour continuer.</p>
        </div>
        <form method="POST" class="space-y-4">
            <div>
                <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Nouveau pseudo</label>
                <input type="text" name="new_username" required minlength="3" placeholder="ex: julie.dev" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3.5 py-2.5 text-white focus:outline-none focus:border-csIndigo text-sm">
            </div>
            <div>
                <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Nouveau mot de passe</label>
                <input type="password" name="new_password" required minlength="6" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3.5 py-2.5 text-white focus:outline-none focus:border-csIndigo text-sm">
            </div>
            <div>
                <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Confirmer le mot de passe</label>
                <input type="password" name="confirm_password" required minlength="6" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3.5 py-2.5 text-white focus:outline-none focus:border-csIndigo text-sm">
            </div>
            <button type="submit" class="w-full bg-csIndigo hover:bg-csIndigoHover text-white font-semibold py-2.5 rounded-lg text-sm transition shadow-lg shadow-indigo-500/20">Activer mon compte</button>
        </form>
    </div>
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

FOUND_COMPANY_TEMPLATE = APP_HEADER + """
<div class="max-w-xl mx-auto __CARD__ p-8 shadow-2xl">
    <div class="flex items-center gap-3 mb-6">
        <div class="p-2.5 brand-gradient rounded-xl text-white shadow-lg shadow-indigo-500/20">
            <i data-lucide="rocket" class="w-6 h-6"></i>
        </div>
        <div>
            <h1 class="text-xl font-bold text-white">Créer un nouvel espace</h1>
            <p class="text-xs text-csMuted">Vous en deviendrez automatiquement le fondateur, avec tous les droits.</p>
        </div>
    </div>
    <form method="POST" class="space-y-5">
        <div class="grid grid-cols-3 gap-3">
            <div class="col-span-1">
                <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Logo (emoji)</label>
                <input type="text" name="logo" maxlength="4" placeholder="🏢" value="🏢" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-center text-lg focus:outline-none focus:border-csIndigo">
            </div>
            <div class="col-span-2">
                <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Nom de l'espace</label>
                <input type="text" name="company_name" required placeholder="ex: Nova Studio" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
            </div>
        </div>
        <div>
            <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Type d'espace</label>
            <select name="space_type" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
                {% for key, t in space_types.items() %}
                <option value="{{ key }}" {% if key == 'entreprise' %}selected{% endif %}>{{ t.icon }} {{ t.label }}</option>
                {% endfor %}
            </select>
        </div>
        <div>
            <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Description courte</label>
            <input type="text" name="description" placeholder="Ce que fait votre espace, en une phrase" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
        </div>
        <div>
            <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Page d'accueil (texte libre, optionnel)</label>
            <textarea name="homepage_text" rows="3" placeholder="Présentez votre mission, vos valeurs..." class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo"></textarea>
        </div>
        <div class="pt-4 border-t border-csBorder">
            <p class="text-xs font-semibold text-white mb-3 flex items-center gap-2"><i data-lucide="user-round" class="w-3.5 h-3.5 text-csIndigo"></i> Votre compte PDG</p>
            <div class="grid grid-cols-2 gap-3">
                <div>
                    <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Votre pseudo</label>
                    <input type="text" name="username" required minlength="3" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
                </div>
                <div>
                    <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Mot de passe</label>
                    <input type="password" name="password" required minlength="6" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
                </div>
            </div>
        </div>
        <button type="submit" class="w-full bg-csIndigo hover:bg-csIndigoHover text-white font-semibold py-3 rounded-lg text-sm transition shadow-lg shadow-indigo-500/20">Créer l'espace</button>
        <p class="text-[11px] text-csMuted text-center">Une succursale correspondante sera automatiquement créée dans Mini GitHub.</p>
    </form>
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

MY_COMPANIES_TEMPLATE = APP_HEADER + """
<h1 class="text-2xl font-bold text-white mb-6">Mes espaces</h1>
<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
    {% for c in companies %}
    <a href="{{ url_for('company_dashboard', company_id=c.id) }}" class="__CARD__ p-5 hover:border-csIndigo/50 transition">
        <div class="flex items-center gap-3 mb-3">
            <div class="w-11 h-11 rounded-xl bg-csCard2 border border-csBorder flex items-center justify-center text-xl">{{ c.logo }}</div>
            <div>
                <h3 class="font-bold text-white">{{ c.name }}</h3>
                <span class="text-[10px] font-semibold px-2 py-0.5 rounded-full border" style="border-color: {{ c.poste_color }}55; color: {{ c.poste_color }};">{{ c.poste_name }}{% if c.is_pdg %} 👑{% endif %}</span>
            </div>
        </div>
        <p class="text-xs text-csMuted line-clamp-2">{{ c.description or "Aucune description." }}</p>
    </a>
    {% else %}
    <div class="col-span-full __CARD__ p-10 text-center text-csMuted">
        <p>Vous n'êtes membre d'aucun espace.</p>
        <a href="{{ url_for('found_company') }}" class="text-csIndigo hover:underline text-sm font-semibold mt-2 inline-block">Créer mon premier espace →</a>
    </div>
    {% endfor %}
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

# ---------------------------------------------------------------------------
# Tableau de bord
# ---------------------------------------------------------------------------

DASHBOARD_TEMPLATE = APP_HEADER + """
<div class="flex items-center justify-between mb-8">
    <div class="flex items-center gap-4">
        <div class="w-14 h-14 rounded-2xl bg-csCard2 border border-csBorder flex items-center justify-center text-2xl">{{ company.logo }}</div>
        <div>
            <h1 class="text-2xl font-bold text-white flex items-center gap-2 flex-wrap">{{ company.name }}
                <span class="text-[10px] font-semibold px-2 py-0.5 rounded-full border align-middle" style="border-color: {{ membership.poste_color }}55; color: {{ membership.poste_color }};">{{ membership.poste_name }}{% if membership.is_pdg %} 👑{% endif %}</span>
                <span class="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-csCard2 border border-csBorder text-csMuted align-middle">{{ space_types.get(company.space_type, space_types['entreprise']).icon }} {{ space_types.get(company.space_type, space_types['entreprise']).label }}</span>
            </h1>
            <p class="text-sm text-csMuted mt-0.5">{{ company.description or "Aucune description." }}</p>
        </div>
    </div>
    {% if perms.edit_company %}
    <a href="{{ url_for('parametres', company_id=company.id) }}" class="text-xs font-semibold border border-csBorder hover:bg-csBorder/40 px-3 py-2 rounded-lg flex items-center gap-1.5 text-csMuted"><i data-lucide="settings" class="w-3.5 h-3.5"></i> Paramètres</a>
    {% endif %}
</div>

<div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
    <div class="__CARD__ p-5"><p class="text-xs text-csMuted">Membres</p><p class="text-2xl font-bold text-white mt-1">{{ stats.members }}</p></div>
    <div class="__CARD__ p-5"><p class="text-xs text-csMuted">Postes</p><p class="text-2xl font-bold text-white mt-1">{{ stats.postes }}</p></div>
    <div class="__CARD__ p-5"><p class="text-xs text-csMuted">Projets</p><p class="text-2xl font-bold text-white mt-1">{{ stats.projects }}</p></div>
    <div class="__CARD__ p-5"><p class="text-xs text-csMuted">Messages</p><p class="text-2xl font-bold text-white mt-1">{{ stats.messages }}</p></div>
</div>

<div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
    <div class="__CARD__ p-6">
        <div class="flex items-center justify-between mb-4">
            <h3 class="font-bold text-white text-sm flex items-center gap-2"><i data-lucide="folder-git-2" class="w-4 h-4 text-csIndigo"></i> Projets récents</h3>
            <a href="{{ url_for('projets', company_id=company.id) }}" class="text-xs text-csIndigo hover:underline">Tout voir →</a>
        </div>
        <div class="space-y-2">
            {% for p in recent_projects %}
            <div class="flex items-center justify-between bg-csCard2 p-3 rounded-lg border border-csBorder text-sm">
                <span class="font-medium text-white">{{ p.repo_name }}</span>
                <span class="text-[10px] text-csMuted">{{ p.owner_name }}</span>
            </div>
            {% else %}
            <p class="text-xs text-csMuted">Aucun projet pour le moment.</p>
            {% endfor %}
        </div>
    </div>
    <div class="__CARD__ p-6">
        <div class="flex items-center justify-between mb-4">
            <h3 class="font-bold text-white text-sm flex items-center gap-2"><i data-lucide="message-square" class="w-4 h-4 text-csIndigo"></i> Derniers messages</h3>
            <a href="{{ url_for('messagerie', company_id=company.id) }}" class="text-xs text-csIndigo hover:underline">Ouvrir →</a>
        </div>
        <div class="space-y-2">
            {% for m in recent_messages %}
            <div class="flex items-start gap-2 text-sm">
                <div class="w-6 h-6 rounded-full flex items-center justify-center font-bold text-white text-[10px] uppercase shrink-0" style="background-color: {{ m.avatar_color }}">{{ m.username[0] }}</div>
                <p class="text-csText"><span class="font-semibold text-white">{{ m.username }}</span> <span class="text-csMuted">{{ m.content[:60] }}{% if m.content|length > 60 %}…{% endif %}</span></p>
            </div>
            {% else %}
            <p class="text-xs text-csMuted">Aucun message pour le moment.</p>
            {% endfor %}
        </div>
    </div>
</div>

<div class="grid grid-cols-1 sm:grid-cols-3 gap-4 mt-6">
    {% if perms.create_account %}
    <a href="{{ url_for('equipe', company_id=company.id) }}" class="__CARD__ p-5 hover:border-csIndigo/50 transition flex items-center gap-3">
        <i data-lucide="user-plus" class="w-5 h-5 text-csIndigo"></i>
        <span class="text-sm font-semibold text-white">Créer un compte employé</span>
    </a>
    {% endif %}
    {% if perms.create_project %}
    <a href="{{ url_for('projets', company_id=company.id) }}" class="__CARD__ p-5 hover:border-csIndigo/50 transition flex items-center gap-3">
        <i data-lucide="folder-plus" class="w-5 h-5 text-csIndigo"></i>
        <span class="text-sm font-semibold text-white">Lancer un projet</span>
    </a>
    {% endif %}
    {% if perms.create_poste %}
    <a href="{{ url_for('postes', company_id=company.id) }}" class="__CARD__ p-5 hover:border-csIndigo/50 transition flex items-center gap-3">
        <i data-lucide="briefcase" class="w-5 h-5 text-csIndigo"></i>
        <span class="text-sm font-semibold text-white">Créer un poste</span>
    </a>
    {% endif %}
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

# ---------------------------------------------------------------------------
# Messagerie
# ---------------------------------------------------------------------------

MESSAGERIE_TEMPLATE = APP_HEADER + """
<h1 class="text-xl font-bold text-white mb-4 flex items-center gap-2"><i data-lucide="message-square" class="w-5 h-5 text-csIndigo"></i> Messagerie — {{ company.name }}</h1>
<div class="__CARD__ flex flex-col h-[65vh]">
    <div class="flex-1 overflow-y-auto custom-scrollbar p-5 space-y-4">
        {% for m in messages %}
        <div class="flex items-start gap-3">
            <div class="w-8 h-8 rounded-full flex items-center justify-center font-bold text-white text-xs uppercase shrink-0" style="background-color: {{ m.avatar_color }}">{{ m.username[0] }}</div>
            <div class="flex-1">
                <div class="flex items-center gap-2">
                    <span class="font-semibold text-white text-sm">{{ m.username }}</span>
                    <span class="text-[10px] text-csMuted">{{ m.created_at }}</span>
                </div>
                <p class="text-sm text-csText mt-0.5">{{ m.content }}</p>
            </div>
            {% if perms.manage_messaging %}
            <form method="POST" action="{{ url_for('delete_message', company_id=company.id, message_id=m.id) }}">
                <button class="text-csMuted hover:text-red-400 p-1" title="Supprimer"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></button>
            </form>
            {% endif %}
        </div>
        {% else %}
        <p class="text-sm text-csMuted text-center mt-10">Aucun message. Lancez la discussion !</p>
        {% endfor %}
    </div>
    <form method="POST" class="border-t border-csBorder p-4 flex gap-3">
        <input type="text" name="content" required placeholder="Écrire un message à l'équipe..." class="flex-1 bg-csCard2 border border-csBorder rounded-lg px-3.5 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
        <button type="submit" class="bg-csIndigo hover:bg-csIndigoHover text-white font-semibold px-4 py-2.5 rounded-lg text-sm transition flex items-center gap-1.5"><i data-lucide="send" class="w-4 h-4"></i></button>
    </form>
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

# ---------------------------------------------------------------------------
# Équipe & comptes
# ---------------------------------------------------------------------------

EQUIPE_TEMPLATE = APP_HEADER + """
<div class="flex items-center justify-between mb-6">
    <h1 class="text-xl font-bold text-white flex items-center gap-2"><i data-lucide="users" class="w-5 h-5 text-csIndigo"></i> Équipe — {{ company.name }}</h1>
</div>

{% if perms.create_account %}
<div class="__CARD__ p-6 mb-6">
    <h3 class="font-bold text-white text-sm mb-4 flex items-center gap-2"><i data-lucide="user-plus" class="w-4 h-4 text-csIndigo"></i> Créer un compte employé</h3>
    {% if postes %}
    <form method="POST" action="{{ url_for('create_account', company_id=company.id) }}" class="flex flex-col sm:flex-row gap-3">
        <select name="poste_id" required class="flex-1 bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
            {% for p in postes %}<option value="{{ p.id }}">{{ p.name }}</option>{% endfor %}
        </select>
        <button type="submit" class="bg-csIndigo hover:bg-csIndigoHover text-white font-semibold px-5 py-2.5 rounded-lg text-sm transition whitespace-nowrap">Générer les identifiants</button>
    </form>
    <p class="text-[11px] text-csMuted mt-2">Un identifiant et un mot de passe temporaires seront générés. Transmettez-les à la personne concernée : à sa première connexion, elle choisira son pseudo et son mot de passe définitifs.</p>
    {% else %}
    <p class="text-sm text-csMuted">Créez d'abord un <a href="{{ url_for('postes', company_id=company.id) }}" class="text-csIndigo hover:underline">poste</a> avant de pouvoir créer des comptes.</p>
    {% endif %}
</div>
{% endif %}

<div class="__CARD__ overflow-x-auto">
    <table class="w-full text-sm min-w-[560px]">
        <thead class="bg-csCard2 text-csMuted text-xs uppercase">
            <tr><th class="text-left px-5 py-3">Employé</th><th class="text-left px-5 py-3">Poste</th><th class="text-left px-5 py-3">Depuis</th><th class="text-right px-5 py-3">Actions</th></tr>
        </thead>
        <tbody class="divide-y divide-csBorder">
            {% for e in employees %}
            <tr>
                <td class="px-5 py-3 flex items-center gap-2">
                    <a href="{{ url_for('profil', username=e.username) }}" class="w-7 h-7 rounded-full flex items-center justify-center font-bold text-white text-xs uppercase shrink-0" style="background-color: {{ e.avatar_color }}">{{ e.username[0] }}</a>
                    <a href="{{ url_for('profil', username=e.username) }}" class="font-medium text-white hover:text-csIndigo transition">{{ e.display_name or e.username }}</a>
                    {% if e.must_setup_account %}<span class="text-[9px] text-amber-400 border border-amber-500/40 bg-amber-950/30 px-1.5 py-0.5 rounded-full">en attente d'activation</span>{% endif %}
                </td>
                <td class="px-5 py-3">
                    {% if perms.assign_poste and not e.is_pdg %}
                    <form method="POST" action="{{ url_for('assign_poste', company_id=company.id, user_id=e.user_id) }}">
                        <select name="poste_id" onchange="this.form.submit()" class="bg-csCard2 border border-csBorder rounded-lg px-2 py-1 text-xs text-white">
                            {% for p in postes %}<option value="{{ p.id }}" {% if p.id == e.poste_id %}selected{% endif %}>{{ p.name }}</option>{% endfor %}
                        </select>
                    </form>
                    {% else %}
                    <span class="text-xs font-semibold px-2 py-1 rounded-full border" style="border-color: {{ e.poste_color }}55; color: {{ e.poste_color }};">{{ e.poste_name }}{% if e.is_pdg %} 👑{% endif %}</span>
                    {% endif %}
                </td>
                <td class="px-5 py-3 text-csMuted text-xs">{{ e.joined_at.split(' ')[0] }}</td>
                <td class="px-5 py-3 text-right">
                    {% if e.user_id in fireable %}
                    <form method="POST" action="{{ url_for('fire_employee', company_id=company.id, user_id=e.user_id) }}" onsubmit="return confirm('Licencier {{ e.username }} ? Cette personne perdra immédiatement l\'accès à l\'entreprise.');" class="inline">
                        <button class="text-csMuted hover:text-orange-400 p-1.5" title="Licencier"><i data-lucide="user-minus" class="w-4 h-4"></i></button>
                    </form>
                    {% endif %}
                    {% if perms.delete_account and not e.is_pdg and e.user_id != session.get('user_id') %}
                    <form method="POST" action="{{ url_for('delete_account', company_id=company.id, user_id=e.user_id) }}" onsubmit="return confirm('Supprimer ce compte ?');" class="inline">
                        <button class="text-csMuted hover:text-red-400 p-1.5" title="Supprimer le compte"><i data-lucide="user-x" class="w-4 h-4"></i></button>
                    </form>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

PROFIL_TEMPLATE = APP_HEADER + """
<div class="max-w-3xl mx-auto">
    <div class="__CARD__ p-8 mb-6">
        <div class="flex items-start justify-between gap-4 flex-wrap">
            <div class="flex items-center gap-4">
                <div class="w-16 h-16 rounded-2xl flex items-center justify-center font-bold text-white text-2xl uppercase shrink-0" style="background-color: {{ profile_user.avatar_color }}">{{ profile_user.username[0] }}</div>
                <div>
                    <h1 class="text-xl font-bold text-white">{{ profile_user.display_name or profile_user.username }}</h1>
                    <p class="text-xs text-csMuted">@{{ profile_user.username }}</p>
                    {% if profile_user.available_for_projects %}
                    <span class="inline-flex items-center gap-1 text-[10px] font-semibold px-2 py-0.5 rounded-full bg-green-500/15 text-green-400 border border-green-500/30 mt-2"><i data-lucide="check-circle-2" class="w-3 h-3"></i> Disponible pour rejoindre un projet</span>
                    {% endif %}
                </div>
            </div>
            {% if is_self %}
            <a href="{{ url_for('edit_profil') }}" class="text-xs font-semibold border border-csBorder hover:bg-csBorder/40 px-3 py-2 rounded-lg flex items-center gap-1.5 text-csMuted"><i data-lucide="pencil" class="w-3.5 h-3.5"></i> Modifier mon profil</a>
            {% endif %}
        </div>
        <p class="text-sm text-csText mt-5">{{ profile_user.bio or "Cette personne n'a pas encore écrit de bio." }}</p>

        {% if skills_list %}
        <div class="flex flex-wrap gap-2 mt-5">
            {% for s in skills_list %}
            <span class="text-xs font-semibold px-3 py-1.5 rounded-full bg-csCard2 border border-csBorder text-csText">{{ s }}</span>
            {% endfor %}
        </div>
        {% endif %}

        {% if interests_list %}
        <div class="mt-4">
            <p class="text-[10px] uppercase font-semibold text-csMuted mb-2">Centres d'intérêt</p>
            <div class="flex flex-wrap gap-2">
                {% for s in interests_list %}
                <span class="text-xs px-3 py-1 rounded-full bg-csIndigo/10 border border-csIndigo/30 text-csIndigo">{{ s }}</span>
                {% endfor %}
            </div>
        </div>
        {% endif %}
    </div>

    <div class="grid grid-cols-1 sm:grid-cols-2 gap-6">
        <div class="__CARD__ p-6">
            <h3 class="font-bold text-white text-sm mb-4 flex items-center gap-2"><i data-lucide="rocket" class="w-4 h-4 text-csIndigo"></i> Projets</h3>
            <div class="space-y-2">
                {% for p in profile_projects %}
                <div class="flex items-center gap-2 text-sm bg-csCard2 border border-csBorder rounded-lg p-2.5">
                    <span>{{ p.image or '🚀' }}</span>
                    <span class="text-csText">{{ p.title or p.repo_name }}</span>
                </div>
                {% else %}
                <p class="text-xs text-csMuted">Aucun projet pour le moment.</p>
                {% endfor %}
            </div>
        </div>
        <div class="__CARD__ p-6">
            <h3 class="font-bold text-white text-sm mb-4 flex items-center gap-2"><i data-lucide="building" class="w-4 h-4 text-csIndigo"></i> Espaces</h3>
            <div class="space-y-2">
                {% for c in profile_companies %}
                <a href="{{ url_for('company_public', company_id=c.id) }}" class="flex items-center gap-2 text-sm bg-csCard2 border border-csBorder rounded-lg p-2.5 hover:border-csIndigo/50 transition">
                    <span>{{ c.logo }}</span>
                    <span class="text-csText">{{ c.name }}</span>
                    <span class="ml-auto text-[10px] font-semibold px-2 py-0.5 rounded-full border" style="border-color: {{ c.poste_color }}55; color: {{ c.poste_color }};">{{ c.poste_name }}{% if c.is_pdg %} 👑{% endif %}</span>
                </a>
                {% else %}
                <p class="text-xs text-csMuted">Aucun espace pour le moment.</p>
                {% endfor %}
            </div>
        </div>
    </div>
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

EDIT_PROFIL_TEMPLATE = APP_HEADER + """
<div class="max-w-xl mx-auto __CARD__ p-8">
    <h1 class="text-xl font-bold text-white mb-6 flex items-center gap-2"><i data-lucide="user-round" class="w-5 h-5 text-csIndigo"></i> Mon profil de créateur</h1>
    <form method="POST" class="space-y-4">
        <div>
            <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Nom affiché</label>
            <input type="text" name="display_name" value="{{ u.display_name or '' }}" placeholder="{{ u.username }}" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3.5 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
        </div>
        <div>
            <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Bio</label>
            <textarea name="bio" rows="3" placeholder="Je construis des jeux, des sites et des trucs avec mes amis 🚀" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3.5 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">{{ u.bio or '' }}</textarea>
        </div>
        <div>
            <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Compétences (séparées par des virgules)</label>
            <input type="text" name="skills" value="{{ u.skills or '' }}" placeholder="Python, Flask, JavaScript, Roblox, IA" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3.5 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
        </div>
        <div>
            <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Centres d'intérêt (séparés par des virgules)</label>
            <input type="text" name="interests" value="{{ u.interests or '' }}" placeholder="Jeux vidéo, musique, science" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3.5 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
        </div>
        <label class="flex items-center gap-2 text-sm text-csText">
            <input type="checkbox" name="available_for_projects" {% if u.available_for_projects %}checked{% endif %} class="w-4 h-4 accent-indigo-500"> Disponible pour rejoindre de nouveaux projets
        </label>
        <button type="submit" class="w-full bg-csIndigo hover:bg-csIndigoHover text-white font-semibold py-2.5 rounded-lg text-sm transition">Enregistrer</button>
    </form>
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

CREDENTIALS_TEMPLATE = APP_HEADER + """
<div class="max-w-md mx-auto __CARD__ p-8 shadow-2xl text-center">
    <div class="inline-flex p-3 rounded-2xl bg-emerald-500/15 border border-emerald-500/30 mb-4">
        <i data-lucide="check-circle-2" class="w-8 h-8 text-emerald-400"></i>
    </div>
    <h1 class="text-xl font-bold text-white mb-1">Compte créé avec succès</h1>
    <p class="text-xs text-csMuted mb-6">Transmettez ces identifiants temporaires à l'employé. Ils ne seront affichés qu'une seule fois.</p>
    <div class="space-y-3 text-left">
        <div class="bg-csCard2 border border-csBorder rounded-lg p-3.5">
            <p class="text-[10px] uppercase text-csMuted font-semibold mb-1">Identifiant temporaire</p>
            <p class="font-mono text-csIndigo font-bold text-lg">{{ creds.username }}</p>
        </div>
        <div class="bg-csCard2 border border-csBorder rounded-lg p-3.5">
            <p class="text-[10px] uppercase text-csMuted font-semibold mb-1">Mot de passe temporaire</p>
            <p class="font-mono text-csIndigo font-bold text-lg break-all">{{ creds.password }}</p>
        </div>
    </div>
    <a href="{{ url_for('equipe', company_id=company.id) }}" class="mt-6 inline-block w-full bg-csIndigo hover:bg-csIndigoHover text-white font-semibold py-2.5 rounded-lg text-sm transition">Retour à l'équipe</a>
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

# ---------------------------------------------------------------------------
# Postes & permissions
# ---------------------------------------------------------------------------

POSTES_TEMPLATE = APP_HEADER + """
<div class="flex items-center justify-between mb-6">
    <h1 class="text-xl font-bold text-white flex items-center gap-2"><i data-lucide="briefcase" class="w-5 h-5 text-csIndigo"></i> Postes — {{ company.name }}</h1>
</div>

{% if perms.create_poste %}
<div class="__CARD__ p-6 mb-6">
    <h3 class="font-bold text-white text-sm mb-4">Créer un nouveau poste</h3>
    <form method="POST" class="flex flex-col sm:flex-row gap-3">
        <input type="text" name="name" required placeholder="ex: Réviseur, Développeur, Community Manager..." class="flex-1 bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
        <input type="color" name="color" value="#6366f1" class="w-14 h-11 bg-csCard2 border border-csBorder rounded-lg cursor-pointer">
        <button type="submit" class="bg-csIndigo hover:bg-csIndigoHover text-white font-semibold px-5 py-2.5 rounded-lg text-sm transition whitespace-nowrap">Créer le poste</button>
    </form>
</div>
{% endif %}

<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
    {% for p in postes %}
    <a href="{{ url_for('poste_permissions', company_id=company.id, poste_id=p.id) }}" class="__CARD__ p-5 hover:border-csIndigo/50 transition">
        <div class="flex items-center justify-between mb-2">
            <span class="font-bold text-white flex items-center gap-1.5">{{ p.name }} {% if p.is_pdg %}👑{% endif %}</span>
            <span class="w-3 h-3 rounded-full" style="background-color: {{ p.color }}"></span>
        </div>
        <p class="text-xs text-csMuted">{{ p.member_count }} membre(s)</p>
        <p class="text-[11px] text-csIndigo mt-3 flex items-center gap-1">{% if p.is_pdg %}Toutes les permissions{% else %}Voir les permissions{% endif %} <i data-lucide="arrow-right" class="w-3 h-3"></i></p>
    </a>
    {% else %}
    <div class="col-span-full __CARD__ p-10 text-center text-csMuted">Aucun poste créé pour le moment.</div>
    {% endfor %}
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

POSTE_PERMISSIONS_TEMPLATE = APP_HEADER + """
<div class="max-w-2xl mx-auto __CARD__ p-8">
    <div class="flex items-center gap-3 mb-6">
        <span class="w-4 h-4 rounded-full" style="background-color: {{ poste.color }}"></span>
        <h1 class="text-xl font-bold text-white">{{ poste.name }}{% if poste.is_pdg %} 👑{% endif %}</h1>
    </div>

    {% if poste.is_pdg %}
    <div class="bg-csCard2 border border-csBorder rounded-xl p-5 text-sm text-csMuted flex items-center gap-3">
        <i data-lucide="shield-check" class="w-5 h-5 text-csIndigo shrink-0"></i>
        Le poste de PDG dispose automatiquement de toutes les permissions et ne peut pas être modifié.
    </div>
    {% else %}
    <form method="POST" class="space-y-3">
        {% for key, meta in permissions.items() %}
        <label class="flex items-center gap-3 bg-csCard2 border border-csBorder rounded-lg px-4 py-3 cursor-pointer hover:border-csIndigo/50 transition {% if not can_edit %}opacity-60 pointer-events-none{% endif %}">
            <input type="checkbox" name="perms" value="{{ key }}" {% if key in current_perms %}checked{% endif %} class="w-4 h-4 accent-indigo-500">
            <i data-lucide="{{ meta.icon }}" class="w-4 h-4 text-csIndigo"></i>
            <span class="text-sm text-csText">{{ meta.label }}</span>
        </label>
        {% endfor %}

        <div class="mt-2 pt-4 border-t border-csBorder">
            <p class="text-xs font-semibold uppercase text-csMuted mb-1">Peut licencier les postes suivants</p>
            <p class="text-[11px] text-csMuted mb-3">Actif uniquement si la compétence « Licencier des employés » ci-dessus est cochée.</p>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
                {% for op in other_postes %}
                <label class="flex items-center gap-2 bg-csCard2 border border-csBorder rounded-lg px-3 py-2 text-xs cursor-pointer {% if not can_edit %}opacity-60 pointer-events-none{% endif %}">
                    <input type="checkbox" name="fire_targets" value="{{ op.id }}" {% if op.id in current_fire_targets %}checked{% endif %} class="w-3.5 h-3.5 accent-orange-500">
                    <span class="text-csText">{{ op.name }}</span>
                </label>
                {% else %}
                <p class="text-xs text-csMuted col-span-2">Aucun autre poste dans cet espace pour le moment.</p>
                {% endfor %}
            </div>
        </div>

        {% if can_edit %}
        <button type="submit" class="w-full bg-csIndigo hover:bg-csIndigoHover text-white font-semibold py-2.5 rounded-lg text-sm transition mt-4">Enregistrer les permissions</button>
        {% endif %}
    </form>
    {% endif %}

    <a href="{{ url_for('postes', company_id=company.id) }}" class="text-xs text-csMuted hover:text-white mt-6 inline-flex items-center gap-1"><i data-lucide="arrow-left" class="w-3.5 h-3.5"></i> Retour aux postes</a>
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

# ---------------------------------------------------------------------------
# Projets (liés à Mini GitHub)
# ---------------------------------------------------------------------------

PROJETS_TEMPLATE = APP_HEADER + """
<div class="flex items-center justify-between mb-6">
    <h1 class="text-xl font-bold text-white flex items-center gap-2"><i data-lucide="folder-git-2" class="w-5 h-5 text-csIndigo"></i> Projets — {{ company.name }}</h1>
</div>

{% if perms.create_project %}
<div class="__CARD__ p-6 mb-6">
    <h3 class="font-bold text-white text-sm mb-4">Lancer un nouveau projet</h3>
    <form method="POST" class="space-y-3"> 
        <div class="grid grid-cols-1 sm:grid-cols-[80px_1fr] gap-3">
            <input type="text" name="image" maxlength="4" value="{{ prefill and '💡' or '🚀' }}" class="bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-center text-lg focus:outline-none focus:border-csIndigo">
            <input type="text" name="title" value="{{ prefill }}" required placeholder="Nom du projet (ex: Jeu de survie sur une île)" class="bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
        </div>
        <textarea name="description" rows="2" placeholder="De quoi s'agit-il ?" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">{{ prefill_desc }}</textarea>
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <select name="category" class="bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
                {% for c in categories %}<option value="{{ c }}">{{ c }}</option>{% endfor %}
            </select>
            <select name="visibility" class="bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
                <option value="prive">🔒 Privé (membres de l'espace)</option>
                <option value="invitation">✉️ Sur invitation</option>
                <option value="public">🌎 Public</option>
            </select>
            <input type="text" name="name" required placeholder="Identifiant technique (ex: site-vitrine)" class="bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
        </div>
        <input type="text" name="links" placeholder="Liens utiles (optionnel, séparés par des virgules)" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
        <label class="flex items-center gap-2 text-xs text-csMuted">
            <input type="checkbox" name="is_private" checked class="w-4 h-4 accent-indigo-500"> Dépôt de code privé
        </label>
        <button type="submit" class="bg-csIndigo hover:bg-csIndigoHover text-white font-semibold px-5 py-2.5 rounded-lg text-sm transition">Créer le projet</button>
    </form>
    <p class="text-[11px] text-csMuted mt-2 flex items-center gap-1.5"><i data-lucide="info" class="w-3 h-3"></i> Le code n'est qu'une fonctionnalité parmi d'autres : jeu, art, école, musique, IA... tout projet a sa place.</p>
</div>
{% endif %}

<div class="grid grid-cols-1 md:grid-cols-2 gap-4">
    {% for p in projects %}
    <div class="__CARD__ p-5">
        <div class="flex items-center justify-between mb-2">
            <span class="font-bold text-white flex items-center gap-2 text-lg">
                <span>{{ p.image or '🚀' }}</span>
                <span class="text-sm">{{ p.title or p.repo_name }}</span>
            </span>
            {% if p.is_private %}<span class="text-[10px] text-csMuted border border-csBorder px-2 py-0.5 rounded-full">Privé</span>{% endif %}
        </div>
        <div class="flex items-center gap-2 mb-3">
            <span class="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-csCard2 border border-csBorder text-csMuted">{{ p.category or 'Autre' }}</span>
            <span class="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-csCard2 border border-csBorder text-csMuted">{{ statuses.get(p.status, '💡 Idée') }}</span>
        </div>
        <p class="text-xs text-csMuted line-clamp-2 mb-4">{{ p.description or "Aucune description." }}</p>
        <div class="flex items-center justify-between text-xs">
            <span class="text-csMuted">Par {{ p.owner_name }} · {{ p.created_at.split(' ')[0] }}</span>
            <div class="flex items-center gap-3">
                <a href="{{ url_for('project_tasks', company_id=company.id, project_id=p.id) }}" class="text-csIndigo hover:underline flex items-center gap-1"><i data-lucide="list-checks" class="w-3.5 h-3.5"></i> Tâches</a>
                <a href="{{ minigithub_url }}/{{ p.owner_name }}/{{ p.repo_name }}" target="_blank" class="text-csIndigo hover:underline flex items-center gap-1">Code <i data-lucide="external-link" class="w-3 h-3"></i></a>
                {% if perms.delete_project %}
                <form method="POST" action="{{ url_for('delete_project', company_id=company.id, project_id=p.id) }}" onsubmit="return confirm('Supprimer ce projet ?');">
                    <button class="text-csMuted hover:text-red-400"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></button>
                </form>
                {% endif %}
            </div>
        </div>
    </div>
    {% else %}
    <div class="col-span-full __CARD__ p-10 text-center text-csMuted">Aucun projet pour le moment.</div>
    {% endfor %}
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

# ---------------------------------------------------------------------------
# Priorité 2 — Idées
# ---------------------------------------------------------------------------

IDEES_TEMPLATE = APP_HEADER + """
<div class="flex items-center justify-between mb-6">
    <h1 class="text-xl font-bold text-white flex items-center gap-2"><i data-lucide="lightbulb" class="w-5 h-5 text-csIndigo"></i> Idées — {{ company.name }}</h1>
</div>

<div class="__CARD__ p-6 mb-6">
    <h3 class="font-bold text-white text-sm mb-4">💡 Nouvelle idée</h3>
    <form method="POST" class="space-y-3">
        <input type="text" name="title" required placeholder="Ex: Faire un jeu de survie sur une île" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
        <textarea name="description" rows="2" placeholder="Précisez votre idée (optionnel)" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo"></textarea>
        <button type="submit" class="bg-csIndigo hover:bg-csIndigoHover text-white font-semibold px-5 py-2.5 rounded-lg text-sm transition">Proposer l'idée</button>
    </form>
</div>

<div class="space-y-4">
    {% for i in ideas %}
    <div class="__CARD__ p-5 {% if i.converted_project_id %}opacity-60{% endif %}">
        <div class="flex items-start justify-between gap-3">
            <div>
                <h3 class="font-bold text-white flex items-center gap-2">💡 {{ i.title }}
                    {% if i.converted_project_id %}<span class="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-green-500/15 text-green-400 border border-green-500/30">Devenu un projet</span>{% endif %}
                </h3>
                <p class="text-xs text-csMuted mt-1">{{ i.description or '' }}</p>
                <p class="text-[11px] text-csMuted mt-2">Par {{ i.author_name }} · {{ i.created_at.split(' ')[0] }}</p>
            </div>
            {% if (i.author_id == session.get('user_id') or perms.delete_project) and not i.converted_project_id %}
            <form method="POST" action="{{ url_for('delete_idea', company_id=company.id, idea_id=i.id) }}" onsubmit="return confirm('Supprimer cette idée ?');">
                <button class="text-csMuted hover:text-red-400"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></button>
            </form>
            {% endif %}
        </div>
        <div class="flex items-center gap-4 mt-4 text-xs">
            <form method="POST" action="{{ url_for('like_idea', company_id=company.id, idea_id=i.id) }}">
                <button class="flex items-center gap-1 {% if i.liked_by_me %}text-red-400{% else %}text-csMuted hover:text-red-400{% endif %}"><i data-lucide="heart" class="w-3.5 h-3.5"></i> {{ i.like_count }}</button>
            </form>
            <form method="POST" action="{{ url_for('save_idea', company_id=company.id, idea_id=i.id) }}">
                <button class="flex items-center gap-1 {% if i.saved_by_me %}text-yellow-400{% else %}text-csMuted hover:text-yellow-400{% endif %}"><i data-lucide="star" class="w-3.5 h-3.5"></i> {{ i.save_count }}</button>
            </form>
            <span class="flex items-center gap-1 text-csMuted"><i data-lucide="message-circle" class="w-3.5 h-3.5"></i> {{ i.comments|length }}</span>
            {% if perms.create_project and not i.converted_project_id %}
            <form method="POST" action="{{ url_for('convert_idea', company_id=company.id, idea_id=i.id) }}" class="ml-auto">
                <button class="bg-csIndigo hover:bg-csIndigoHover text-white font-semibold px-3 py-1.5 rounded-lg flex items-center gap-1.5"><i data-lucide="rocket" class="w-3.5 h-3.5"></i> Transformer en projet</button>
            </form>
            {% endif %}
        </div>
        {% if i.comments %}
        <div class="mt-3 pt-3 border-t border-csBorder space-y-2">
            {% for c in i.comments %}
            <p class="text-xs text-csText"><span class="font-semibold text-white">{{ c.username }}</span> <span class="text-csMuted">{{ c.content }}</span></p>
            {% endfor %}
        </div>
        {% endif %}
        <form method="POST" action="{{ url_for('comment_idea', company_id=company.id, idea_id=i.id) }}" class="mt-3 flex gap-2">
            <input type="text" name="content" required placeholder="Commenter..." class="flex-1 bg-csCard2 border border-csBorder rounded-lg px-3 py-1.5 text-white text-xs focus:outline-none focus:border-csIndigo">
            <button class="text-csIndigo text-xs font-semibold px-2">Envoyer</button>
        </form>
    </div>
    {% else %}
    <div class="__CARD__ p-10 text-center text-csMuted">Aucune idée pour le moment. Proposez la première !</div>
    {% endfor %}
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

FEED_TEMPLATE = APP_HEADER + """
<div class="flex items-center justify-between mb-6">
    <h1 class="text-xl font-bold text-white flex items-center gap-2"><i data-lucide="newspaper" class="w-5 h-5 text-csIndigo"></i> Fil d'activité — {{ company.name }}</h1>
</div>

<div class="space-y-3">
    {% for a in activities %}
    <div class="__CARD__ p-4 flex items-start gap-3">
        <div class="w-8 h-8 rounded-full flex items-center justify-center font-bold text-white text-xs uppercase shrink-0" style="background-color: {{ a.avatar_color }}">{{ a.username[0] }}</div>
        <div class="flex-1">
            <p class="text-sm text-csText"><span class="font-semibold text-white">{{ a.display_name or a.username }}</span> {{ a.text }}</p>
            <p class="text-[11px] text-csMuted mt-0.5">{{ a.created_at.split('.')[0] }}</p>
        </div>
    </div>
    {% else %}
    <div class="__CARD__ p-10 text-center text-csMuted">
        <i data-lucide="newspaper" class="w-10 h-10 mx-auto mb-3 opacity-40"></i>
        <p>Rien à afficher pour le moment. Créez un projet, proposez une idée...</p>
    </div>
    {% endfor %}
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

# ---------------------------------------------------------------------------
# Priorité 3 — Tâches
# ---------------------------------------------------------------------------

TASKS_TEMPLATE = APP_HEADER + """
<div class="flex items-center justify-between mb-6">
    <h1 class="text-xl font-bold text-white flex items-center gap-2"><i data-lucide="list-checks" class="w-5 h-5 text-csIndigo"></i> Tâches — {{ project.title or project.repo_name }}</h1>
    <a href="{{ url_for('projets', company_id=company.id) }}" class="text-xs text-csIndigo hover:underline">← Retour aux projets</a>
</div>

<div class="__CARD__ p-6 mb-6">
    <h3 class="font-bold text-white text-sm mb-4">Nouvelle tâche</h3>
    <form method="POST" class="flex flex-col sm:flex-row gap-3">
        <input type="text" name="title" required placeholder="Ex: Corriger la connexion" class="flex-1 bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
        <select name="priority" class="bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
            <option value="basse">Priorité basse</option>
            <option value="normale" selected>Priorité normale</option>
            <option value="haute">Priorité haute</option>
        </select>
        <button type="submit" class="bg-csIndigo hover:bg-csIndigoHover text-white font-semibold px-5 py-2.5 rounded-lg text-sm transition">Ajouter</button>
    </form>
</div>

<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
    {% for status_key, status_label in statuses %}
    <div class="__CARD__ p-4">
        <h3 class="font-bold text-white text-xs uppercase tracking-wide mb-3 flex items-center justify-between">
            {{ status_label }} <span class="text-csMuted font-normal">{{ tasks_by_status.get(status_key, [])|length }}</span>
        </h3>
        <div class="space-y-2">
            {% for t in tasks_by_status.get(status_key, []) %}
            <div class="bg-csCard2 border border-csBorder rounded-lg p-3 text-xs">
                <p class="text-white font-medium">{{ t.title }}</p>
                {% if t.priority == 'haute' %}<span class="text-[10px] text-red-400">🔴 Priorité haute</span>{% endif %}
                <div class="flex items-center justify-between mt-2">
                    <form method="POST" action="{{ url_for('update_task_status', company_id=company.id, project_id=project.id, task_id=t.id) }}" class="flex-1">
                        <select name="status" onchange="this.form.submit()" class="w-full bg-csCard border border-csBorder rounded px-1.5 py-1 text-[10px] text-csMuted focus:outline-none">
                            {% for sk, sl in statuses %}
                            <option value="{{ sk }}" {% if sk == t.status %}selected{% endif %}>{{ sl }}</option>
                            {% endfor %}
                        </select>
                    </form>
                    <form method="POST" action="{{ url_for('delete_task', company_id=company.id, project_id=project.id, task_id=t.id) }}" onsubmit="return confirm('Supprimer cette tâche ?');">
                        <button class="text-csMuted hover:text-red-400 ml-2"><i data-lucide="trash-2" class="w-3 h-3"></i></button>
                    </form>
                </div>
            </div>
            {% else %}
            <p class="text-[11px] text-csMuted">Rien ici.</p>
            {% endfor %}
        </div>
    </div>
    {% endfor %}
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

PARAMETRES_TEMPLATE = APP_HEADER + """
<div class="max-w-xl mx-auto space-y-6">
    <div class="__CARD__ p-8">
        <h1 class="text-xl font-bold text-white mb-6 flex items-center gap-2"><i data-lucide="settings" class="w-5 h-5 text-csIndigo"></i> Paramètres de l'espace</h1>
        {% if can_edit %}
        <form method="POST" class="space-y-4">
            <div class="grid grid-cols-3 gap-3">
                <div>
                    <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Logo</label>
                    <input type="text" name="logo" maxlength="4" value="{{ company.logo }}" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-center text-lg">
                </div>
                <div class="col-span-2">
                    <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Nom</label>
                    <input type="text" name="name" required value="{{ company.name }}" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm">
                </div>
            </div>
            <div>
                <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Description</label>
                <input type="text" name="description" value="{{ company.description or '' }}" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm">
            </div>
            <div>
                <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Page d'accueil</label>
                <textarea name="homepage_text" rows="4" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm">{{ company.homepage_text or '' }}</textarea>
            </div>
            <button type="submit" class="w-full bg-csIndigo hover:bg-csIndigoHover text-white font-semibold py-2.5 rounded-lg text-sm transition">Enregistrer</button>
        </form>
        {% else %}
        <p class="text-sm text-csMuted">Vous n'avez pas la permission de modifier la fiche de cet espace.</p>
        {% endif %}
    </div>

    <div class="__CARD__ p-8">
        <h2 class="text-sm font-bold text-white mb-1 flex items-center gap-2"><i data-lucide="git-branch" class="w-4 h-4 text-csIndigo"></i> Statut de l'espace</h2>
        <p class="text-[11px] text-csMuted mb-4">Filiale de qui ? Société mère de qui ?</p>

        {% if parent %}
        <p class="text-sm text-csText mb-3">Filiale de <a href="{{ url_for('company_public', company_id=parent.id) }}" class="text-csIndigo hover:underline font-semibold">{{ parent.name }}</a></p>
        {% else %}
        <p class="text-sm text-csMuted mb-3">Espace indépendant (aucun espace parent).</p>
        {% endif %}

        {% if children %}
        <p class="text-xs text-csMuted mb-4">Société mère de : {% for ch in children %}<a href="{{ url_for('company_public', company_id=ch.id) }}" class="text-csIndigo hover:underline">{{ ch.name }}</a>{% if not loop.last %}, {% endif %}{% endfor %}</p>
        {% endif %}

        {% if can_edit %}
        <form method="POST" class="flex flex-col sm:flex-row gap-2">
            <input type="hidden" name="form" value="hierarchy">
            <select name="parent_company_id" class="flex-1 bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
                <option value="">— Indépendante (aucune société mère) —</option>
                {% for oc in other_companies %}
                <option value="{{ oc.id }}" {% if parent and oc.id == parent.id %}selected{% endif %}>{{ oc.name }}</option>
                {% endfor %}
            </select>
            <button type="submit" class="bg-csIndigo hover:bg-csIndigoHover text-white font-semibold px-5 py-2.5 rounded-lg text-sm transition whitespace-nowrap">Mettre à jour</button>
        </form>
        {% endif %}
    </div>

    {% if can_delete %}
    <div class="__CARD__ p-8 border-red-900/50">
        <h2 class="text-sm font-bold text-red-400 mb-2 flex items-center gap-2"><i data-lucide="flame" class="w-4 h-4"></i> Zone dangereuse</h2>
        <p class="text-xs text-csMuted mb-4">Supprimer l'espace efface définitivement ses postes, ses comptes membres, ses projets et les dépôts de code associés. Les éventuels espaces enfants redeviennent indépendants. Cette action est irréversible.</p>
        <form method="POST" action="{{ url_for('delete_company', company_id=company.id) }}" onsubmit="return confirm('Cette action est irréversible. Confirmer la suppression définitive de {{ company.name }} ?');" class="flex flex-col sm:flex-row gap-2">
            <input type="text" name="confirm_name" required placeholder="Tapez « {{ company.name }} » pour confirmer" class="flex-1 bg-csCard2 border border-red-900/50 rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-red-500">
            <button type="submit" class="bg-red-600 hover:bg-red-500 text-white font-semibold px-5 py-2.5 rounded-lg text-sm transition whitespace-nowrap">Supprimer l'espace</button>
        </form>
    </div>
    {% endif %}
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

# ---------------------------------------------------------------------------
# Routes — Landing / directory
# ---------------------------------------------------------------------------

@app.route('/')
def landing():
    if session.get('user_id'):
        return redirect(url_for('my_companies'))
    companies = get_db().execute("""
        SELECT c.*,
            (SELECT COUNT(*) FROM employees e WHERE e.company_id = c.id) AS member_count,
            (SELECT COUNT(*) FROM projects p WHERE p.company_id = c.id) AS project_count
        FROM companies c ORDER BY c.created_at DESC
    """).fetchall()
    return render_template_string(LANDING_TEMPLATE, companies=companies, my_companies=[], space_types=SPACE_TYPES)

@app.route('/entreprise/<int:company_id>')
def company_public(company_id):
    db = get_db()
    c = get_company(company_id)
    if not c:
        flash('Espace introuvable.', 'error')
        return redirect(url_for('landing'))
    member_count = db.execute("SELECT COUNT(*) c FROM employees WHERE company_id=?", (company_id,)).fetchone()['c']
    project_count = db.execute("SELECT COUNT(*) c FROM projects WHERE company_id=?", (company_id,)).fetchone()['c']
    postes = db.execute("SELECT * FROM postes WHERE company_id=? ORDER BY is_pdg DESC, name", (company_id,)).fetchall()
    parent = get_company(c['parent_company_id']) if c['parent_company_id'] else None
    children = get_company_children(company_id)
    return render_template_string(COMPANY_PUBLIC_TEMPLATE, c=c, member_count=member_count,
                                   project_count=project_count, postes=postes, parent=parent, children=children,
                                   my_companies=get_my_companies(), space_types=SPACE_TYPES)

# ---------------------------------------------------------------------------
# Routes — Authentification
# ---------------------------------------------------------------------------

@app.route('/connexion', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and user['password_hash'] == hash_password(password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['avatar_color'] = user['avatar_color']
            flash(f'Bienvenue, {user["username"]} !', 'success')
            return redirect(url_for('my_companies'))
        flash('Identifiant ou mot de passe incorrect.', 'error')
    return render_template_string(LOGIN_TEMPLATE, my_companies=[])

@app.route('/deconnexion')
def logout():
    session.clear()
    flash('Vous avez été déconnecté.', 'success')
    return redirect(url_for('landing'))

@app.route('/configurer-compte', methods=['GET', 'POST'])
@login_required
def setup_account():
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (session['user_id'],)).fetchone()
    if not user or not user['must_setup_account']:
        return redirect(url_for('my_companies'))

    emp = db.execute("""SELECT c.name FROM employees e JOIN companies c ON e.company_id=c.id
                         WHERE e.user_id=? LIMIT 1""", (user['id'],)).fetchone()
    company_name = emp['name'] if emp else 'CorpSuite'

    if request.method == 'POST':
        new_username = request.form['new_username'].strip()
        new_password = request.form['new_password']
        confirm_password = request.form['confirm_password']

        if new_password != confirm_password:
            flash('Les mots de passe ne correspondent pas.', 'error')
        elif db.execute("SELECT 1 FROM users WHERE username = ? AND id != ?", (new_username, user['id'])).fetchone():
            flash('Ce pseudo est déjà pris.', 'error')
        else:
            db.execute("UPDATE users SET username = ?, password_hash = ?, must_setup_account = 0 WHERE id = ?",
                       (new_username, hash_password(new_password), user['id']))
            db.commit()
            session['username'] = new_username
            flash('Votre compte est activé. Bienvenue !', 'success')
            return redirect(url_for('my_companies'))

    return render_template_string(SETUP_ACCOUNT_TEMPLATE, company_name=company_name, my_companies=[])

@app.route('/fonder', methods=['GET', 'POST'])
def found_company():
    if request.method == 'POST':
        db = get_db()
        cur = db.cursor()
        company_name = request.form['company_name'].strip()
        description = request.form.get('description', '').strip()
        logo = request.form.get('logo', '🏢').strip() or '🏢'
        homepage_text = request.form.get('homepage_text', '').strip()
        space_type = request.form.get('space_type', 'entreprise')
        if space_type not in SPACE_TYPES:
            space_type = 'entreprise'
        username = request.form['username'].strip()
        password = request.form['password']

        if cur.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            flash('Ce pseudo est déjà utilisé, choisissez-en un autre.', 'error')
            return render_template_string(FOUND_COMPANY_TEMPLATE, my_companies=[], space_types=SPACE_TYPES)

        # 1) Compte du PDG (fondateur, choisit lui-même ses identifiants)
        cur.execute("INSERT INTO users (username, password_hash, avatar_color) VALUES (?, ?, ?)",
                    (username, hash_password(password), '#6366f1'))
        user_id = cur.lastrowid

        # 2) Succursale correspondante dans Mini GitHub
        cur.execute("INSERT INTO branch_offices (name, location, description, admin_id) VALUES (?, ?, ?, ?)",
                    (company_name, 'Siège social', description, user_id))
        branch_office_id = cur.lastrowid
        cur.execute("UPDATE users SET branch_office_id = ? WHERE id = ?", (branch_office_id, user_id))

        # 3) Espace
        cur.execute("""INSERT INTO companies (name, description, logo, homepage_text, branch_office_id, pdg_user_id, space_type)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (company_name, description, logo, homepage_text, branch_office_id, user_id, space_type))
        company_id = cur.lastrowid

        # 4) Poste PDG (toutes les permissions, non modifiable)
        cur.execute("INSERT INTO postes (company_id, name, color, is_pdg) VALUES (?, 'PDG', '#f59e0b', 1)", (company_id,))
        poste_id = cur.lastrowid

        # 5) Rattachement
        cur.execute("INSERT INTO employees (company_id, user_id, poste_id) VALUES (?, ?, ?)",
                    (company_id, user_id, poste_id))

        db.commit()

        session['user_id'] = user_id
        session['username'] = username
        session['avatar_color'] = '#6366f1'
        flash(f"Espace « {company_name} » créé ! Une succursale a été créée dans Mini GitHub.", 'success')
        return redirect(url_for('company_dashboard', company_id=company_id))

    return render_template_string(FOUND_COMPANY_TEMPLATE, my_companies=[], space_types=SPACE_TYPES)

@app.route('/mes-entreprises')
@login_required
def my_companies():
    companies = get_my_companies()
    if len(companies) == 1:
        return redirect(url_for('company_dashboard', company_id=companies[0]['id']))
    return render_template_string(MY_COMPANIES_TEMPLATE, companies=companies, my_companies=companies)

# ---------------------------------------------------------------------------
# Routes — Tableau de bord
# ---------------------------------------------------------------------------

@app.route('/app/<int:company_id>')
@member_required
def company_dashboard(company_id):
    db = get_db()
    company = get_company(company_id)
    membership = get_membership(company_id)
    stats = {
        'members': db.execute("SELECT COUNT(*) c FROM employees WHERE company_id=?", (company_id,)).fetchone()['c'],
        'projects': db.execute("SELECT COUNT(*) c FROM projects WHERE company_id=?", (company_id,)).fetchone()['c'],
        'postes': db.execute("SELECT COUNT(*) c FROM postes WHERE company_id=?", (company_id,)).fetchone()['c'],
        'messages': db.execute("SELECT COUNT(*) c FROM company_messages WHERE company_id=?", (company_id,)).fetchone()['c'],
    }
    recent_projects = db.execute("""
        SELECT pr.*, r.name AS repo_name, u.username AS owner_name FROM projects pr
        JOIN repositories r ON pr.repo_id = r.id JOIN users u ON r.owner_id = u.id
        WHERE pr.company_id=? ORDER BY pr.created_at DESC LIMIT 5
    """, (company_id,)).fetchall()
    recent_messages = db.execute("""
        SELECT m.*, u.username, u.avatar_color FROM company_messages m JOIN users u ON m.user_id = u.id
        WHERE m.company_id=? ORDER BY m.created_at DESC LIMIT 5
    """, (company_id,)).fetchall()
    perms = {k: has_perm(company_id, k) for k in PERMISSIONS}
    return render_template_string(DASHBOARD_TEMPLATE, company=company, membership=membership, stats=stats,
                                   recent_projects=recent_projects, recent_messages=recent_messages,
                                   perms=perms, my_companies=get_my_companies(), space_types=SPACE_TYPES)

# ---------------------------------------------------------------------------
# Routes — Messagerie
# ---------------------------------------------------------------------------

@app.route('/app/<int:company_id>/messagerie', methods=['GET', 'POST'])
@member_required
def messagerie(company_id):
    db = get_db()
    company = get_company(company_id)
    if request.method == 'POST':
        content = request.form['content'].strip()
        if content:
            db.execute("INSERT INTO company_messages (company_id, user_id, content) VALUES (?, ?, ?)",
                       (company_id, session['user_id'], content))
            db.commit()
        return redirect(url_for('messagerie', company_id=company_id))

    messages = db.execute("""
        SELECT m.*, u.username, u.avatar_color FROM company_messages m JOIN users u ON m.user_id = u.id
        WHERE m.company_id=? ORDER BY m.created_at ASC
    """, (company_id,)).fetchall()
    perms = {k: has_perm(company_id, k) for k in PERMISSIONS}
    return render_template_string(MESSAGERIE_TEMPLATE, company=company, messages=messages,
                                   perms=perms, my_companies=get_my_companies())

@app.route('/app/<int:company_id>/messagerie/<int:message_id>/supprimer', methods=['POST'])
@permission_required('manage_messaging')
def delete_message(company_id, message_id):
    db = get_db()
    db.execute("DELETE FROM company_messages WHERE id=? AND company_id=?", (message_id, company_id))
    db.commit()
    flash('Message supprimé.', 'success')
    return redirect(url_for('messagerie', company_id=company_id))

# ---------------------------------------------------------------------------
# Routes — Équipe & comptes
# ---------------------------------------------------------------------------

@app.route('/app/<int:company_id>/equipe')
@member_required
def equipe(company_id):
    db = get_db()
    company = get_company(company_id)
    employees = db.execute("""
        SELECT e.*, u.username, u.avatar_color, u.must_setup_account, u.display_name, p.name AS poste_name, p.color AS poste_color, p.is_pdg
        FROM employees e JOIN users u ON e.user_id = u.id JOIN postes p ON e.poste_id = p.id
        WHERE e.company_id=? ORDER BY p.is_pdg DESC, u.username
    """, (company_id,)).fetchall()
    postes = db.execute("SELECT * FROM postes WHERE company_id=? AND is_pdg=0 ORDER BY name", (company_id,)).fetchall()
    all_postes = db.execute("SELECT * FROM postes WHERE company_id=? ORDER BY name", (company_id,)).fetchall()
    perms = {k: has_perm(company_id, k) for k in PERMISSIONS}
    fireable = {e['user_id'] for e in employees if can_fire(company_id, e['user_id'])}
    return render_template_string(EQUIPE_TEMPLATE, company=company, employees=employees, postes=all_postes,
                                   perms=perms, fireable=fireable, my_companies=get_my_companies())

@app.route('/app/<int:company_id>/comptes/nouveau', methods=['POST'])
@permission_required('create_account')
def create_account(company_id):
    db = get_db()
    cur = db.cursor()
    poste_id = request.form['poste_id']
    poste = cur.execute("SELECT * FROM postes WHERE id=? AND company_id=?", (poste_id, company_id)).fetchone()
    company = get_company(company_id)
    if not poste:
        flash('Poste introuvable.', 'error')
        return redirect(url_for('equipe', company_id=company_id))

    username = random_username()
    while cur.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        username = random_username()
    password = random_password()

    cur.execute("INSERT INTO users (username, password_hash, avatar_color, branch_office_id, must_setup_account) VALUES (?, ?, ?, ?, 1)",
                (username, hash_password(password), '#6366f1', company['branch_office_id']))
    new_user_id = cur.lastrowid
    cur.execute("INSERT INTO employees (company_id, user_id, poste_id) VALUES (?, ?, ?)",
                (company_id, new_user_id, poste_id))
    db.commit()
    log_activity(company_id, session['user_id'], 'member', f"a ajouté {username} comme {poste['name']} 👥")

    return render_template_string(CREDENTIALS_TEMPLATE, company=company,
                                   creds={'username': username, 'password': password},
                                   my_companies=get_my_companies())

@app.route('/app/<int:company_id>/equipe/<int:user_id>/poste', methods=['POST'])
@permission_required('assign_poste')
def assign_poste(company_id, user_id):
    db = get_db()
    poste_id = request.form['poste_id']
    target = db.execute("SELECT p.is_pdg FROM employees e JOIN postes p ON e.poste_id=p.id WHERE e.company_id=? AND e.user_id=?",
                         (company_id, user_id)).fetchone()
    if target and target['is_pdg']:
        flash('Impossible de modifier le poste du PDG.', 'error')
    else:
        db.execute("UPDATE employees SET poste_id=? WHERE company_id=? AND user_id=?", (poste_id, company_id, user_id))
        db.commit()
        flash('Poste mis à jour.', 'success')
    return redirect(url_for('equipe', company_id=company_id))

@app.route('/app/<int:company_id>/equipe/<int:user_id>/supprimer', methods=['POST'])
@permission_required('delete_account')
def delete_account(company_id, user_id):
    db = get_db()
    target = db.execute("SELECT p.is_pdg FROM employees e JOIN postes p ON e.poste_id=p.id WHERE e.company_id=? AND e.user_id=?",
                         (company_id, user_id)).fetchone()
    if target and target['is_pdg']:
        flash('Impossible de supprimer le compte du PDG.', 'error')
    elif user_id == session.get('user_id'):
        flash('Vous ne pouvez pas supprimer votre propre compte.', 'error')
    else:
        db.execute("DELETE FROM employees WHERE company_id=? AND user_id=?", (company_id, user_id))
        db.commit()
        flash('Compte retiré de l\'espace.', 'success')
    return redirect(url_for('equipe', company_id=company_id))

@app.route('/app/<int:company_id>/equipe/<int:user_id>/licencier', methods=['POST'])
@member_required
def fire_employee(company_id, user_id):
    if not can_fire(company_id, user_id):
        flash("Vous n'avez pas la compétence pour licencier cette personne.", 'error')
        return redirect(url_for('equipe', company_id=company_id))

    db = get_db()
    target = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    db.execute("DELETE FROM employees WHERE company_id=? AND user_id=?", (company_id, user_id))
    if target:
        db.execute(
            "INSERT INTO company_messages (company_id, user_id, content) VALUES (?, ?, ?)",
            (company_id, session['user_id'],
             f"{target['username']} a été licencié(e) par {session['username']}.")
        )
    db.commit()
    flash(f"{target['username'] if target else 'La personne'} a été licencié(e).", 'success')
    return redirect(url_for('equipe', company_id=company_id))

# ---------------------------------------------------------------------------
# Routes — Postes & permissions
# ---------------------------------------------------------------------------

@app.route('/app/<int:company_id>/postes', methods=['GET', 'POST'])
@member_required
def postes(company_id):
    db = get_db()
    company = get_company(company_id)
    if request.method == 'POST':
        if not has_perm(company_id, 'create_poste'):
            flash("Permission refusée.", 'error')
        else:
            name = request.form['name'].strip()
            color = request.form.get('color', '#6366f1')
            db.execute("INSERT INTO postes (company_id, name, color, is_pdg) VALUES (?, ?, ?, 0)", (company_id, name, color))
            db.commit()
            flash(f'Poste « {name} » créé.', 'success')
        return redirect(url_for('postes', company_id=company_id))

    postes_list = db.execute("""
        SELECT p.*, (SELECT COUNT(*) FROM employees e WHERE e.poste_id = p.id) AS member_count
        FROM postes p WHERE p.company_id=? ORDER BY p.is_pdg DESC, p.name
    """, (company_id,)).fetchall()
    perms = {k: has_perm(company_id, k) for k in PERMISSIONS}
    return render_template_string(POSTES_TEMPLATE, company=company, postes=postes_list,
                                   perms=perms, my_companies=get_my_companies())

@app.route('/app/<int:company_id>/postes/<int:poste_id>', methods=['GET', 'POST'])
@member_required
def poste_permissions(company_id, poste_id):
    db = get_db()
    company = get_company(company_id)
    poste = db.execute("SELECT * FROM postes WHERE id=? AND company_id=?", (poste_id, company_id)).fetchone()
    if not poste:
        flash('Poste introuvable.', 'error')
        return redirect(url_for('postes', company_id=company_id))

    can_edit = has_perm(company_id, 'manage_permissions') and not poste['is_pdg']

    if request.method == 'POST':
        if not can_edit:
            flash('Permission refusée.', 'error')
        else:
            selected = request.form.getlist('perms')
            db.execute("DELETE FROM poste_permissions WHERE poste_id=?", (poste_id,))
            for key in selected:
                if key in PERMISSIONS:
                    db.execute("INSERT INTO poste_permissions (poste_id, permission_key) VALUES (?, ?)", (poste_id, key))

            # Personnalisation "qui peut licencier qui" : seulement pertinent
            # si la compétence 'fire_employee' est cochée pour ce poste.
            db.execute("DELETE FROM poste_fire_targets WHERE poste_id=?", (poste_id,))
            if 'fire_employee' in selected:
                for raw_tid in request.form.getlist('fire_targets'):
                    if raw_tid.isdigit() and int(raw_tid) != poste_id:
                        target_ok = db.execute(
                            "SELECT 1 FROM postes WHERE id=? AND company_id=? AND is_pdg=0",
                            (int(raw_tid), company_id)
                        ).fetchone()
                        if target_ok:
                            db.execute(
                                "INSERT INTO poste_fire_targets (poste_id, target_poste_id) VALUES (?, ?)",
                                (poste_id, int(raw_tid))
                            )

            db.commit()
            flash('Permissions mises à jour.', 'success')
        return redirect(url_for('poste_permissions', company_id=company_id, poste_id=poste_id))

    current_perms = {r['permission_key'] for r in db.execute(
        "SELECT permission_key FROM poste_permissions WHERE poste_id=?", (poste_id,)).fetchall()}
    current_fire_targets = {r['target_poste_id'] for r in db.execute(
        "SELECT target_poste_id FROM poste_fire_targets WHERE poste_id=?", (poste_id,)).fetchall()}
    other_postes = db.execute(
        "SELECT * FROM postes WHERE company_id=? AND is_pdg=0 AND id != ? ORDER BY name",
        (company_id, poste_id)
    ).fetchall()
    return render_template_string(POSTE_PERMISSIONS_TEMPLATE, company=company, poste=poste,
                                   permissions=PERMISSIONS, current_perms=current_perms, can_edit=can_edit,
                                   other_postes=other_postes, current_fire_targets=current_fire_targets,
                                   my_companies=get_my_companies())

# ---------------------------------------------------------------------------
# Routes — Projets (génèrent un dépôt Mini GitHub)
# ---------------------------------------------------------------------------

@app.route('/app/<int:company_id>/projets', methods=['GET', 'POST'])
@member_required
def projets(company_id):
    db = get_db()
    cur = db.cursor()
    company = get_company(company_id)

    if request.method == 'POST':
        if not has_perm(company_id, 'create_project'):
            flash('Permission refusée.', 'error')
            return redirect(url_for('projets', company_id=company_id))

        raw_title = request.form.get('title', '').strip()
        name = request.form['name'].strip().replace(' ', '-')
        description = request.form.get('description', '').strip()
        is_private = 1 if request.form.get('is_private') else 0
        category = request.form.get('category', 'Autre').strip() or 'Autre'
        image = request.form.get('image', '🚀').strip() or '🚀'
        links = request.form.get('links', '').strip()
        visibility = request.form.get('visibility', 'prive')
        if visibility not in ('prive', 'invitation', 'public'):
            visibility = 'prive'
        title = raw_title or name
        username = session['username']

        if cur.execute("SELECT r.id FROM repositories r WHERE r.owner_id=? AND r.name=?",
                       (session['user_id'], name)).fetchone():
            flash('Vous avez déjà un dépôt portant ce nom.', 'error')
            return redirect(url_for('projets', company_id=company_id))

        # 1) Dépôt Mini GitHub — le code n'est qu'un module du projet parmi
        # d'autres, mais reste créé automatiquement pour que l'onglet
        # « Code » soit toujours disponible si le projet en a besoin plus tard.
        cur.execute("""INSERT INTO repositories (name, description, owner_id, branch_office_id, is_private)
                        VALUES (?, ?, ?, ?, ?)""",
                    (name, description, session['user_id'], company['branch_office_id'], is_private))
        repo_id = cur.lastrowid

        # 2) Branche principale
        cur.execute("INSERT INTO branches (repo_id, name) VALUES (?, 'main')", (repo_id,))

        # 3) README initial
        readme = f"# {title}\n\n{description}\n\nProjet initié via CorpSuite par {username} pour {company['name']}."
        cur.execute("""INSERT INTO files (repo_id, branch_name, file_path, content, language)
                        VALUES (?, 'main', 'README.md', ?, 'Markdown')""", (repo_id, readme))

        # 4) Lien Projet <-> Entreprise, avec les métadonnées "universelles"
        cur.execute("""INSERT INTO projects (company_id, repo_id, created_by, title, category, status, image, links, visibility)
                        VALUES (?, ?, ?, ?, ?, 'idee', ?, ?, ?)""",
                    (company_id, repo_id, session['user_id'], title, category, image, links, visibility))
        db.commit()
        log_activity(company_id, session['user_id'], 'project', f"a lancé le projet {image} « {title} »")
        flash(f'Projet « {title} » créé !', 'success')
        return redirect(url_for('projets', company_id=company_id))

    projects_list = db.execute("""
        SELECT pr.id, pr.title, pr.category, pr.status, pr.image, pr.links, pr.visibility,
               r.name AS repo_name, r.description, r.is_private, r.created_at, u.username AS owner_name
        FROM projects pr JOIN repositories r ON pr.repo_id = r.id JOIN users u ON r.owner_id = u.id
        WHERE pr.company_id=? ORDER BY pr.created_at DESC
    """, (company_id,)).fetchall()
    perms = {k: has_perm(company_id, k) for k in PERMISSIONS}
    prefill = request.args.get('titre', '')
    prefill_desc = request.args.get('description', '')
    return render_template_string(PROJETS_TEMPLATE, company=company, projects=projects_list, perms=perms,
                                   minigithub_url=MINIGITHUB_URL, my_companies=get_my_companies(),
                                   categories=PROJECT_CATEGORIES, statuses=dict(PROJECT_STATUSES),
                                   prefill=prefill, prefill_desc=prefill_desc)

@app.route('/app/<int:company_id>/projets/<int:project_id>/supprimer', methods=['POST'])
@permission_required('delete_project')
def delete_project(company_id, project_id):
    db = get_db()
    proj = db.execute("SELECT * FROM projects WHERE id=? AND company_id=?", (project_id, company_id)).fetchone()
    if proj:
        db.execute("DELETE FROM files WHERE repo_id=?", (proj['repo_id'],))
        db.execute("DELETE FROM branches WHERE repo_id=?", (proj['repo_id'],))
        db.execute("DELETE FROM pull_requests WHERE repo_id=?", (proj['repo_id'],))
        db.execute("DELETE FROM repository_collaborators WHERE repo_id=?", (proj['repo_id'],))
        db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        db.execute("DELETE FROM repositories WHERE id=?", (proj['repo_id'],))
        db.commit()
        flash('Projet et dépôt associé supprimés.', 'success')
    return redirect(url_for('projets', company_id=company_id))

# ---------------------------------------------------------------------------
# Routes — Idées (Priorité 2)
# ---------------------------------------------------------------------------

def _load_ideas(company_id):
    db = get_db()
    ideas = db.execute("""
        SELECT i.*, u.username AS author_name FROM ideas i JOIN users u ON i.author_id = u.id
        WHERE i.company_id=? ORDER BY i.created_at DESC
    """, (company_id,)).fetchall()
    result = []
    uid = session.get('user_id')
    for i in ideas:
        i = dict(i)
        i['like_count'] = db.execute("SELECT COUNT(*) c FROM idea_likes WHERE idea_id=?", (i['id'],)).fetchone()['c']
        i['save_count'] = db.execute("SELECT COUNT(*) c FROM idea_saves WHERE idea_id=?", (i['id'],)).fetchone()['c']
        i['liked_by_me'] = bool(db.execute("SELECT 1 FROM idea_likes WHERE idea_id=? AND user_id=?", (i['id'], uid)).fetchone())
        i['saved_by_me'] = bool(db.execute("SELECT 1 FROM idea_saves WHERE idea_id=? AND user_id=?", (i['id'], uid)).fetchone())
        i['comments'] = db.execute("""
            SELECT ic.*, u.username FROM idea_comments ic JOIN users u ON ic.user_id = u.id
            WHERE ic.idea_id=? ORDER BY ic.created_at ASC
        """, (i['id'],)).fetchall()
        result.append(i)
    return result

@app.route('/app/<int:company_id>/idees', methods=['GET', 'POST'])
@member_required
def idees(company_id):
    db = get_db()
    company = get_company(company_id)

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        if not title:
            flash('Le titre de l\'idée est requis.', 'error')
        else:
            db.execute("INSERT INTO ideas (company_id, author_id, title, description) VALUES (?, ?, ?, ?)",
                       (company_id, session['user_id'], title, description))
            db.commit()
            log_activity(company_id, session['user_id'], 'idea', f"a proposé l'idée 💡 « {title} »")
            flash('Idée proposée !', 'success')
        return redirect(url_for('idees', company_id=company_id))

    perms = {k: has_perm(company_id, k) for k in PERMISSIONS}
    return render_template_string(IDEES_TEMPLATE, company=company, ideas=_load_ideas(company_id), perms=perms,
                                   my_companies=get_my_companies())

@app.route('/app/<int:company_id>/idees/<int:idea_id>/aimer', methods=['POST'])
@member_required
def like_idea(company_id, idea_id):
    db = get_db()
    uid = session['user_id']
    existing = db.execute("SELECT id FROM idea_likes WHERE idea_id=? AND user_id=?", (idea_id, uid)).fetchone()
    if existing:
        db.execute("DELETE FROM idea_likes WHERE id=?", (existing['id'],))
    else:
        db.execute("INSERT INTO idea_likes (idea_id, user_id) VALUES (?, ?)", (idea_id, uid))
    db.commit()
    return redirect(url_for('idees', company_id=company_id))

@app.route('/app/<int:company_id>/idees/<int:idea_id>/sauvegarder', methods=['POST'])
@member_required
def save_idea(company_id, idea_id):
    db = get_db()
    uid = session['user_id']
    existing = db.execute("SELECT id FROM idea_saves WHERE idea_id=? AND user_id=?", (idea_id, uid)).fetchone()
    if existing:
        db.execute("DELETE FROM idea_saves WHERE id=?", (existing['id'],))
    else:
        db.execute("INSERT INTO idea_saves (idea_id, user_id) VALUES (?, ?)", (idea_id, uid))
    db.commit()
    return redirect(url_for('idees', company_id=company_id))

@app.route('/app/<int:company_id>/idees/<int:idea_id>/commenter', methods=['POST'])
@member_required
def comment_idea(company_id, idea_id):
    content = request.form.get('content', '').strip()
    if content:
        db = get_db()
        db.execute("INSERT INTO idea_comments (idea_id, user_id, content) VALUES (?, ?, ?)",
                   (idea_id, session['user_id'], content))
        db.commit()
    return redirect(url_for('idees', company_id=company_id))

@app.route('/app/<int:company_id>/idees/<int:idea_id>/supprimer', methods=['POST'])
@member_required
def delete_idea(company_id, idea_id):
    db = get_db()
    idea = db.execute("SELECT * FROM ideas WHERE id=? AND company_id=?", (idea_id, company_id)).fetchone()
    if idea and (idea['author_id'] == session['user_id'] or has_perm(company_id, 'delete_project')):
        db.execute("DELETE FROM idea_likes WHERE idea_id=?", (idea_id,))
        db.execute("DELETE FROM idea_saves WHERE idea_id=?", (idea_id,))
        db.execute("DELETE FROM idea_comments WHERE idea_id=?", (idea_id,))
        db.execute("DELETE FROM ideas WHERE id=?", (idea_id,))
        db.commit()
        flash('Idée supprimée.', 'success')
    return redirect(url_for('idees', company_id=company_id))

@app.route('/app/<int:company_id>/idees/<int:idea_id>/transformer', methods=['POST'])
@permission_required('create_project')
def convert_idea(company_id, idea_id):
    db = get_db()
    idea = db.execute("SELECT * FROM ideas WHERE id=? AND company_id=?", (idea_id, company_id)).fetchone()
    if not idea:
        return redirect(url_for('idees', company_id=company_id))
    # Redirige vers la création de projet pré-remplie avec le contenu de l'idée ;
    # l'idée est marquée comme convertie une fois le projet réellement créé
    # (on ne peut pas connaître l'id du projet avant que le formulaire soit soumis,
    # donc on marque la conversion ici avec un lien symbolique vers la démarche).
    db.execute("UPDATE ideas SET converted_project_id = -1 WHERE id=?", (idea_id,))
    db.commit()
    log_activity(company_id, session['user_id'], 'convert', f"a transformé l'idée « {idea['title']} » en projet 🚀")
    return redirect(url_for('projets', company_id=company_id, titre=idea['title'], description=idea['description'] or ''))

# ---------------------------------------------------------------------------
# Routes — Tâches (Priorité 3)
# ---------------------------------------------------------------------------

@app.route('/app/<int:company_id>/projets/<int:project_id>/taches', methods=['GET', 'POST'])
@member_required
def project_tasks(company_id, project_id):
    db = get_db()
    company = get_company(company_id)
    project = db.execute("""
        SELECT pr.*, r.name AS repo_name FROM projects pr JOIN repositories r ON pr.repo_id = r.id
        WHERE pr.id=? AND pr.company_id=?
    """, (project_id, company_id)).fetchone()
    if not project:
        flash('Projet introuvable.', 'error')
        return redirect(url_for('projets', company_id=company_id))

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        priority = request.form.get('priority', 'normale')
        if title:
            db.execute("""INSERT INTO tasks (project_id, company_id, title, priority, created_by)
                           VALUES (?, ?, ?, ?, ?)""", (project_id, company_id, title, priority, session['user_id']))
            db.commit()
            flash('Tâche ajoutée.', 'success')
        return redirect(url_for('project_tasks', company_id=company_id, project_id=project_id))

    tasks = db.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()
    tasks_by_status = {}
    for t in tasks:
        tasks_by_status.setdefault(t['status'] or 'a_faire', []).append(t)
    return render_template_string(TASKS_TEMPLATE, company=company, project=project, statuses=TASK_STATUSES,
                                   tasks_by_status=tasks_by_status, my_companies=get_my_companies())

@app.route('/app/<int:company_id>/projets/<int:project_id>/taches/<int:task_id>/statut', methods=['POST'])
@member_required
def update_task_status(company_id, project_id, task_id):
    new_status = request.form.get('status', 'a_faire')
    if new_status not in dict(TASK_STATUSES):
        new_status = 'a_faire'
    db = get_db()
    db.execute("UPDATE tasks SET status=? WHERE id=? AND project_id=? AND company_id=?",
               (new_status, task_id, project_id, company_id))
    db.commit()
    return redirect(url_for('project_tasks', company_id=company_id, project_id=project_id))

@app.route('/app/<int:company_id>/projets/<int:project_id>/taches/<int:task_id>/supprimer', methods=['POST'])
@member_required
def delete_task(company_id, project_id, task_id):
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id=? AND project_id=? AND company_id=?", (task_id, project_id, company_id))
    db.commit()
    return redirect(url_for('project_tasks', company_id=company_id, project_id=project_id))

# ---------------------------------------------------------------------------
# Routes — Fil d'activité (Priorité 5)
# ---------------------------------------------------------------------------

@app.route('/app/<int:company_id>/fil')
@member_required
def activity_feed(company_id):
    db = get_db()
    company = get_company(company_id)
    activities = db.execute("""
        SELECT a.*, u.username, u.avatar_color, u.display_name FROM activities a JOIN users u ON a.actor_id = u.id
        WHERE a.company_id=? ORDER BY a.created_at DESC LIMIT 50
    """, (company_id,)).fetchall()
    return render_template_string(FEED_TEMPLATE, company=company, activities=activities, my_companies=get_my_companies())

# ---------------------------------------------------------------------------
# Routes — Profils de créateurs (Priorité 4)
# ---------------------------------------------------------------------------

def _split_tags(raw):
    if not raw:
        return []
    return [t.strip() for t in raw.split(',') if t.strip()]

@app.route('/profil/<username>')
@login_required
def profil(username):
    db = get_db()
    profile_user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not profile_user:
        flash('Ce profil n\'existe pas.', 'error')
        return redirect(url_for('my_companies'))

    profile_projects = db.execute("""
        SELECT pr.title, pr.image, r.name AS repo_name FROM projects pr
        JOIN repositories r ON pr.repo_id = r.id
        WHERE r.owner_id = ? ORDER BY pr.created_at DESC
    """, (profile_user['id'],)).fetchall()

    profile_companies = db.execute("""
        SELECT c.*, p.name AS poste_name, p.is_pdg, p.color AS poste_color
        FROM employees e JOIN companies c ON e.company_id = c.id JOIN postes p ON e.poste_id = p.id
        WHERE e.user_id = ? ORDER BY c.name
    """, (profile_user['id'],)).fetchall()

    return render_template_string(
        PROFIL_TEMPLATE, profile_user=profile_user, is_self=(session.get('user_id') == profile_user['id']),
        skills_list=_split_tags(profile_user['skills']), interests_list=_split_tags(profile_user['interests']),
        profile_projects=profile_projects, profile_companies=profile_companies, my_companies=get_my_companies()
    )

@app.route('/mon-profil', methods=['GET', 'POST'])
@login_required
def edit_profil():
    db = get_db()
    if request.method == 'POST':
        db.execute("""UPDATE users SET display_name=?, bio=?, skills=?, interests=?, available_for_projects=?
                       WHERE id=?""", (
            request.form.get('display_name', '').strip(),
            request.form.get('bio', '').strip(),
            request.form.get('skills', '').strip(),
            request.form.get('interests', '').strip(),
            1 if request.form.get('available_for_projects') else 0,
            session['user_id']
        ))
        db.commit()
        flash('Profil mis à jour !', 'success')
        return redirect(url_for('profil', username=session['username']))

    u = db.execute("SELECT * FROM users WHERE id=?", (session['user_id'],)).fetchone()
    return render_template_string(EDIT_PROFIL_TEMPLATE, u=u, my_companies=get_my_companies())

# ---------------------------------------------------------------------------
# Routes — Paramètres
# ---------------------------------------------------------------------------

@app.route('/app/<int:company_id>/parametres', methods=['GET', 'POST'])
@member_required
def parametres(company_id):
    db = get_db()
    company = get_company(company_id)
    can_edit = has_perm(company_id, 'edit_company')
    can_delete = has_perm(company_id, 'delete_company')

    if request.method == 'POST':
        if not can_edit:
            flash('Permission refusée.', 'error')
            return redirect(url_for('parametres', company_id=company_id))

        if request.form.get('form') == 'hierarchy':
            raw_parent = request.form.get('parent_company_id', '').strip()
            if not raw_parent:
                db.execute("UPDATE companies SET parent_company_id = NULL WHERE id=?", (company_id,))
                db.commit()
                flash("L'espace est désormais indépendant.", 'success')
            else:
                new_parent_id = int(raw_parent)
                parent = get_company(new_parent_id)
                if not parent:
                    flash('Société mère introuvable.', 'error')
                elif new_parent_id == company_id or is_descendant(new_parent_id, company_id):
                    flash("Impossible : cela créerait une boucle de filiation (une filiale ne peut pas être sa propre société mère).", 'error')
                else:
                    db.execute("UPDATE companies SET parent_company_id = ? WHERE id=?", (new_parent_id, company_id))
                    db.commit()
                    flash(f"« {company['name']} » est désormais une filiale de « {parent['name']} ».", 'success')
            return redirect(url_for('parametres', company_id=company_id))

        db.execute("UPDATE companies SET name=?, description=?, logo=?, homepage_text=? WHERE id=?",
                   (request.form['name'].strip(), request.form.get('description', '').strip(),
                    request.form.get('logo', '🏢').strip() or '🏢',
                    request.form.get('homepage_text', '').strip(), company_id))
        db.commit()
        flash("Fiche de l'espace mise à jour.", 'success')
        return redirect(url_for('company_dashboard', company_id=company_id))

    other_companies = db.execute("SELECT * FROM companies WHERE id != ? ORDER BY name", (company_id,)).fetchall()
    parent = get_company(company['parent_company_id']) if company['parent_company_id'] else None
    children = get_company_children(company_id)
    return render_template_string(PARAMETRES_TEMPLATE, company=company, my_companies=get_my_companies(),
                                   can_edit=can_edit, can_delete=can_delete,
                                   other_companies=other_companies, parent=parent, children=children)

@app.route('/app/<int:company_id>/supprimer', methods=['POST'])
@permission_required('delete_company')
def delete_company(company_id):
    db = get_db()
    company = get_company(company_id)
    if not company:
        flash('Espace introuvable.', 'error')
        return redirect(url_for('my_companies'))

    if request.form.get('confirm_name', '').strip() != company['name']:
        flash('Le nom saisi ne correspond pas : suppression annulée.', 'error')
        return redirect(url_for('parametres', company_id=company_id))

    # Les filiales éventuelles redeviennent des entreprises indépendantes
    # plutôt que d'être supprimées en cascade.
    db.execute("UPDATE companies SET parent_company_id = NULL WHERE parent_company_id = ?", (company_id,))

    # Projets (et dépôts Mini GitHub associés)
    for proj in db.execute("SELECT repo_id FROM projects WHERE company_id=?", (company_id,)).fetchall():
        db.execute("DELETE FROM files WHERE repo_id=?", (proj['repo_id'],))
        db.execute("DELETE FROM branches WHERE repo_id=?", (proj['repo_id'],))
        db.execute("DELETE FROM pull_requests WHERE repo_id=?", (proj['repo_id'],))
        db.execute("DELETE FROM repository_collaborators WHERE repo_id=?", (proj['repo_id'],))
        db.execute("DELETE FROM repositories WHERE id=?", (proj['repo_id'],))
    db.execute("DELETE FROM projects WHERE company_id=?", (company_id,))

    # Comptes employés (doit précéder la suppression des postes, référencés
    # par employees.poste_id)
    db.execute("DELETE FROM employees WHERE company_id=?", (company_id,))

    # Postes, permissions, cibles de licenciement
    postes_ids = [r['id'] for r in db.execute("SELECT id FROM postes WHERE company_id=?", (company_id,)).fetchall()]
    for pid in postes_ids:
        db.execute("DELETE FROM poste_permissions WHERE poste_id=?", (pid,))
        db.execute("DELETE FROM poste_fire_targets WHERE poste_id=? OR target_poste_id=?", (pid, pid))
    db.execute("DELETE FROM postes WHERE company_id=?", (company_id,))

    db.execute("DELETE FROM company_messages WHERE company_id=?", (company_id,))
    db.execute("DELETE FROM companies WHERE id=?", (company_id,))
    db.commit()

    flash(f"L'espace « {company['name']} » a été définitivement supprimé.", 'success')
    return redirect(url_for('my_companies'))

# ---------------------------------------------------------------------------

if __name__ == '__main__':
    init_db()
    print("=================================================================")
    print("🏢 CorpSuite lancé sur http://127.0.0.1:5001")
    print("🔗 Base de données partagée avec Mini GitHub Pro : minigithub.db")
    print("   Lancez aussi `python github.py` (port 5000) pour ouvrir le code")
    print("   des projets créés depuis CorpSuite.")
    print("💡 Aucun espace n'existe encore : rendez-vous sur /fonder")
    print("   pour créer la première et devenir son PDG.")
    print("=================================================================")
    app.run(debug=True, port=5001)
