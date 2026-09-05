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
    'manage_messaging':   {'label': "Modérer la messagerie d'entreprise",'icon': 'message-square'},
    'edit_company':       {'label': "Modifier la fiche entreprise",      'icon': 'edit-3'},
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

        # Colonne ajoutée à `users` si elle vient de github.py et ne l'a pas encore
        try:
            cur.execute("ALTER TABLE users ADD COLUMN must_setup_account INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        # --- Tables propres à CorpSuite ---
        cur.execute('''CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            logo TEXT DEFAULT '🏢',
            homepage_text TEXT,
            branch_office_id INTEGER NOT NULL,
            pdg_user_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (branch_office_id) REFERENCES branch_offices (id),
            FOREIGN KEY (pdg_user_id) REFERENCES users (id)
        )''')
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
        db.commit()

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
    <title>CorpSuite — Plateforme d'entreprise</title>
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
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
            <div class="flex items-center gap-6">
                <a href="{{ url_for('landing') }}" class="flex items-center gap-2.5 text-white font-extrabold text-lg">
                    <span class="brand-gradient w-8 h-8 rounded-lg flex items-center justify-center shadow-lg shadow-indigo-500/20">
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
                </div>
                {% endif %}
            </div>
            <div class="flex items-center gap-3">
                {% if session.get('user_id') %}
                    {% if my_companies and my_companies|length > 1 %}
                    <div class="relative group hidden sm:block">
                        <button class="text-xs font-semibold px-3 py-2 rounded-lg border border-csBorder hover:bg-csBorder/40 flex items-center gap-1.5 text-csMuted">
                            <i data-lucide="building" class="w-3.5 h-3.5"></i> Changer d'entreprise
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
                        <div class="w-7 h-7 rounded-full flex items-center justify-center font-bold text-white text-xs uppercase" style="background-color: {{ session.get('avatar_color', '#6366f1') }}">
                            {{ session.get('username')[0] }}
                        </div>
                        <span class="font-medium text-white hidden sm:inline">{{ session.get('username') }}</span>
                    </div>
                    <a href="{{ url_for('logout') }}" class="text-csMuted hover:text-red-400 p-2 rounded-lg hover:bg-csBorder/40 transition" title="Déconnexion">
                        <i data-lucide="log-out" class="w-4 h-4"></i>
                    </a>
                {% else %}
                    <a href="{{ url_for('login') }}" class="text-sm font-semibold text-csText hover:text-white px-3 py-2">Connexion</a>
                    <a href="{{ url_for('found_company') }}" class="bg-csIndigo hover:bg-csIndigoHover text-white text-sm font-semibold px-4 py-2 rounded-lg transition shadow-lg shadow-indigo-500/20">Fonder mon entreprise</a>
                {% endif %}
            </div>
        </div>
    </header>
    <main class="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-8">
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
            <div>CorpSuite &copy; 2026 — Comptes, postes, permissions &amp; messagerie d'entreprise</div>
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
    <h1 class="text-4xl sm:text-5xl font-extrabold text-white tracking-tight leading-tight">La suite complète pour<br> piloter votre mini-entreprise</h1>
    <p class="text-csMuted mt-5 text-base leading-relaxed">Créez votre entreprise, définissez des postes avec des permissions précises, distribuez des comptes à votre équipe et lancez des projets qui génèrent automatiquement leur dépôt de code.</p>
    <div class="flex items-center justify-center gap-3 mt-8">
        <a href="{{ url_for('found_company') }}" class="bg-csIndigo hover:bg-csIndigoHover text-white font-semibold px-6 py-3 rounded-xl transition shadow-lg shadow-indigo-500/25 flex items-center gap-2">
            <i data-lucide="rocket" class="w-4 h-4"></i> Fonder mon entreprise
        </a>
        <a href="{{ url_for('login') }}" class="border border-csBorder hover:bg-csBorder/40 text-csText font-semibold px-6 py-3 rounded-xl transition">J'ai déjà un compte</a>
    </div>
</div>

<h2 class="text-lg font-bold text-white mb-4 flex items-center gap-2"><i data-lucide="compass" class="w-5 h-5 text-csIndigo"></i> Entreprises sur la plateforme</h2>
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
        <p class="text-xs text-csMuted line-clamp-2">{{ c.description or "Aucune description." }}</p>
    </a>
    {% else %}
    <div class="col-span-full __CARD__ p-10 text-center text-csMuted">
        <i data-lucide="building" class="w-10 h-10 mx-auto mb-3 opacity-40"></i>
        <p>Aucune entreprise pour le moment. Soyez le premier à en fonder une !</p>
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
                <h1 class="text-2xl font-bold text-white">{{ c.name }}</h1>
                <p class="text-xs text-csMuted mt-1">{{ member_count }} membre(s) · {{ project_count }} projet(s) actif(s) · Fondée le {{ c.created_at.split(' ')[0] }}</p>
            </div>
        </div>
        {% if c.description %}<p class="text-sm text-csText mt-6">{{ c.description }}</p>{% endif %}
        {% if c.homepage_text %}
        <div class="mt-6 pt-6 border-t border-csBorder text-sm text-csText leading-relaxed whitespace-pre-line">{{ c.homepage_text }}</div>
        {% endif %}
    </div>

    <div class="__CARD__ p-6">
        <h3 class="font-bold text-white text-sm mb-4 flex items-center gap-2"><i data-lucide="briefcase" class="w-4 h-4 text-csIndigo"></i> Postes de l'entreprise</h3>
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
        <p class="text-sm text-csMuted mt-1">Utilisez les identifiants fournis par votre PDG, ou les vôtres si vous avez fondé une entreprise.</p>
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
        Pas encore d'entreprise ? <a href="{{ url_for('found_company') }}" class="text-csIndigo hover:underline font-semibold">Fondez la vôtre</a>
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
            <h1 class="text-xl font-bold text-white">Fonder une nouvelle entreprise</h1>
            <p class="text-xs text-csMuted">Vous en deviendrez automatiquement le PDG, avec tous les droits.</p>
        </div>
    </div>
    <form method="POST" class="space-y-5">
        <div class="grid grid-cols-3 gap-3">
            <div class="col-span-1">
                <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Logo (emoji)</label>
                <input type="text" name="logo" maxlength="4" placeholder="🏢" value="🏢" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-center text-lg focus:outline-none focus:border-csIndigo">
            </div>
            <div class="col-span-2">
                <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Nom de l'entreprise</label>
                <input type="text" name="company_name" required placeholder="ex: Nova Studio" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
            </div>
        </div>
        <div>
            <label class="block text-xs font-semibold uppercase text-csMuted mb-1">Description courte</label>
            <input type="text" name="description" placeholder="Ce que fait votre entreprise, en une phrase" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
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
        <button type="submit" class="w-full bg-csIndigo hover:bg-csIndigoHover text-white font-semibold py-3 rounded-lg text-sm transition shadow-lg shadow-indigo-500/20">Fonder l'entreprise</button>
        <p class="text-[11px] text-csMuted text-center">Une succursale correspondante sera automatiquement créée dans Mini GitHub.</p>
    </form>
</div>
""".replace("__CARD__", CARD) + APP_FOOTER

MY_COMPANIES_TEMPLATE = APP_HEADER + """
<h1 class="text-2xl font-bold text-white mb-6">Mes entreprises</h1>
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
        <p>Vous n'êtes membre d'aucune entreprise.</p>
        <a href="{{ url_for('found_company') }}" class="text-csIndigo hover:underline text-sm font-semibold mt-2 inline-block">Fonder ma première entreprise →</a>
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
            <h1 class="text-2xl font-bold text-white flex items-center gap-2">{{ company.name }}
                <span class="text-[10px] font-semibold px-2 py-0.5 rounded-full border align-middle" style="border-color: {{ membership.poste_color }}55; color: {{ membership.poste_color }};">{{ membership.poste_name }}{% if membership.is_pdg %} 👑{% endif %}</span>
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

<div class="__CARD__ overflow-hidden">
    <table class="w-full text-sm">
        <thead class="bg-csCard2 text-csMuted text-xs uppercase">
            <tr><th class="text-left px-5 py-3">Employé</th><th class="text-left px-5 py-3">Poste</th><th class="text-left px-5 py-3">Depuis</th><th class="text-right px-5 py-3">Actions</th></tr>
        </thead>
        <tbody class="divide-y divide-csBorder">
            {% for e in employees %}
            <tr>
                <td class="px-5 py-3 flex items-center gap-2">
                    <div class="w-7 h-7 rounded-full flex items-center justify-center font-bold text-white text-xs uppercase" style="background-color: {{ e.avatar_color }}">{{ e.username[0] }}</div>
                    <span class="font-medium text-white">{{ e.username }}</span>
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
                    {% if perms.delete_account and not e.is_pdg and e.user_id != session.get('user_id') %}
                    <form method="POST" action="{{ url_for('delete_account', company_id=company.id, user_id=e.user_id) }}" onsubmit="return confirm('Supprimer ce compte ?');">
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
        <div class="flex flex-col sm:flex-row gap-3">
            <input type="text" name="name" required placeholder="Nom du projet (ex: site-vitrine)" class="flex-1 bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo">
            <label class="flex items-center gap-2 text-xs text-csMuted px-3">
                <input type="checkbox" name="is_private" class="w-4 h-4 accent-indigo-500"> Dépôt privé
            </label>
        </div>
        <textarea name="description" rows="2" placeholder="Description du projet" class="w-full bg-csCard2 border border-csBorder rounded-lg px-3 py-2.5 text-white text-sm focus:outline-none focus:border-csIndigo"></textarea>
        <button type="submit" class="bg-csIndigo hover:bg-csIndigoHover text-white font-semibold px-5 py-2.5 rounded-lg text-sm transition">Créer le projet + dépôt de code</button>
    </form>
    <p class="text-[11px] text-csMuted mt-2 flex items-center gap-1.5"><i data-lucide="link" class="w-3 h-3"></i> Un dépôt Mini GitHub (branche <code>main</code> + README) sera créé automatiquement.</p>
</div>
{% endif %}

<div class="grid grid-cols-1 md:grid-cols-2 gap-4">
    {% for p in projects %}
    <div class="__CARD__ p-5">
        <div class="flex items-center justify-between mb-2">
            <span class="font-bold text-white flex items-center gap-2"><i data-lucide="folder-git-2" class="w-4 h-4 text-csIndigo"></i> {{ p.repo_name }}</span>
            {% if p.is_private %}<span class="text-[10px] text-csMuted border border-csBorder px-2 py-0.5 rounded-full">Privé</span>{% endif %}
        </div>
        <p class="text-xs text-csMuted line-clamp-2 mb-4">{{ p.description or "Aucune description." }}</p>
        <div class="flex items-center justify-between text-xs">
            <span class="text-csMuted">Par {{ p.owner_name }} · {{ p.created_at.split(' ')[0] }}</span>
            <div class="flex items-center gap-3">
                <a href="{{ minigithub_url }}/{{ p.owner_name }}/{{ p.repo_name }}" target="_blank" class="text-csIndigo hover:underline flex items-center gap-1">Ouvrir le code <i data-lucide="external-link" class="w-3 h-3"></i></a>
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

PARAMETRES_TEMPLATE = APP_HEADER + """
<div class="max-w-xl mx-auto __CARD__ p-8">
    <h1 class="text-xl font-bold text-white mb-6 flex items-center gap-2"><i data-lucide="settings" class="w-5 h-5 text-csIndigo"></i> Paramètres de l'entreprise</h1>
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
    return render_template_string(LANDING_TEMPLATE, companies=companies, my_companies=[])

@app.route('/entreprise/<int:company_id>')
def company_public(company_id):
    db = get_db()
    c = get_company(company_id)
    if not c:
        flash('Entreprise introuvable.', 'error')
        return redirect(url_for('landing'))
    member_count = db.execute("SELECT COUNT(*) c FROM employees WHERE company_id=?", (company_id,)).fetchone()['c']
    project_count = db.execute("SELECT COUNT(*) c FROM projects WHERE company_id=?", (company_id,)).fetchone()['c']
    postes = db.execute("SELECT * FROM postes WHERE company_id=? ORDER BY is_pdg DESC, name", (company_id,)).fetchall()
    return render_template_string(COMPANY_PUBLIC_TEMPLATE, c=c, member_count=member_count,
                                   project_count=project_count, postes=postes, my_companies=get_my_companies())

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
        username = request.form['username'].strip()
        password = request.form['password']

        if cur.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            flash('Ce pseudo est déjà utilisé, choisissez-en un autre.', 'error')
            return render_template_string(FOUND_COMPANY_TEMPLATE, my_companies=[])

        # 1) Compte du PDG (fondateur, choisit lui-même ses identifiants)
        cur.execute("INSERT INTO users (username, password_hash, avatar_color) VALUES (?, ?, ?)",
                    (username, hash_password(password), '#6366f1'))
        user_id = cur.lastrowid

        # 2) Succursale correspondante dans Mini GitHub
        cur.execute("INSERT INTO branch_offices (name, location, description, admin_id) VALUES (?, ?, ?, ?)",
                    (company_name, 'Siège social', description, user_id))
        branch_office_id = cur.lastrowid
        cur.execute("UPDATE users SET branch_office_id = ? WHERE id = ?", (branch_office_id, user_id))

        # 3) Entreprise
        cur.execute("""INSERT INTO companies (name, description, logo, homepage_text, branch_office_id, pdg_user_id)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                    (company_name, description, logo, homepage_text, branch_office_id, user_id))
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
        flash(f"Entreprise « {company_name} » fondée ! Une succursale a été créée dans Mini GitHub.", 'success')
        return redirect(url_for('company_dashboard', company_id=company_id))

    return render_template_string(FOUND_COMPANY_TEMPLATE, my_companies=[])

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
                                   perms=perms, my_companies=get_my_companies())

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
        SELECT e.*, u.username, u.avatar_color, u.must_setup_account, p.name AS poste_name, p.color AS poste_color, p.is_pdg
        FROM employees e JOIN users u ON e.user_id = u.id JOIN postes p ON e.poste_id = p.id
        WHERE e.company_id=? ORDER BY p.is_pdg DESC, u.username
    """, (company_id,)).fetchall()
    postes = db.execute("SELECT * FROM postes WHERE company_id=? AND is_pdg=0 ORDER BY name", (company_id,)).fetchall()
    all_postes = db.execute("SELECT * FROM postes WHERE company_id=? ORDER BY name", (company_id,)).fetchall()
    perms = {k: has_perm(company_id, k) for k in PERMISSIONS}
    return render_template_string(EQUIPE_TEMPLATE, company=company, employees=employees, postes=all_postes,
                                   perms=perms, my_companies=get_my_companies())

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
        flash('Compte retiré de l\'entreprise.', 'success')
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
            db.commit()
            flash('Permissions mises à jour.', 'success')
        return redirect(url_for('poste_permissions', company_id=company_id, poste_id=poste_id))

    current_perms = {r['permission_key'] for r in db.execute(
        "SELECT permission_key FROM poste_permissions WHERE poste_id=?", (poste_id,)).fetchall()}
    return render_template_string(POSTE_PERMISSIONS_TEMPLATE, company=company, poste=poste,
                                   permissions=PERMISSIONS, current_perms=current_perms, can_edit=can_edit,
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

        name = request.form['name'].strip().replace(' ', '-')
        description = request.form.get('description', '').strip()
        is_private = 1 if request.form.get('is_private') else 0
        username = session['username']

        if cur.execute("SELECT r.id FROM repositories r WHERE r.owner_id=? AND r.name=?",
                       (session['user_id'], name)).fetchone():
            flash('Vous avez déjà un dépôt portant ce nom.', 'error')
            return redirect(url_for('projets', company_id=company_id))

        # 1) Dépôt Mini GitHub
        cur.execute("""INSERT INTO repositories (name, description, owner_id, branch_office_id, is_private)
                        VALUES (?, ?, ?, ?, ?)""",
                    (name, description, session['user_id'], company['branch_office_id'], is_private))
        repo_id = cur.lastrowid

        # 2) Branche principale
        cur.execute("INSERT INTO branches (repo_id, name) VALUES (?, 'main')", (repo_id,))

        # 3) README initial
        readme = f"# {name}\n\n{description}\n\nProjet initié via CorpSuite par {username} pour {company['name']}."
        cur.execute("""INSERT INTO files (repo_id, branch_name, file_path, content, language)
                        VALUES (?, 'main', 'README.md', ?, 'Markdown')""", (repo_id, readme))

        # 4) Lien Projet <-> Entreprise
        cur.execute("INSERT INTO projects (company_id, repo_id, created_by) VALUES (?, ?, ?)",
                    (company_id, repo_id, session['user_id']))
        db.commit()
        flash(f'Projet « {name} » créé avec son dépôt de code dans Mini GitHub !', 'success')
        return redirect(url_for('projets', company_id=company_id))

    projects_list = db.execute("""
        SELECT pr.id, r.name AS repo_name, r.description, r.is_private, r.created_at, u.username AS owner_name
        FROM projects pr JOIN repositories r ON pr.repo_id = r.id JOIN users u ON r.owner_id = u.id
        WHERE pr.company_id=? ORDER BY pr.created_at DESC
    """, (company_id,)).fetchall()
    perms = {k: has_perm(company_id, k) for k in PERMISSIONS}
    return render_template_string(PROJETS_TEMPLATE, company=company, projects=projects_list, perms=perms,
                                   minigithub_url=MINIGITHUB_URL, my_companies=get_my_companies())

@app.route('/app/<int:company_id>/projets/<int:project_id>/supprimer', methods=['POST'])
@permission_required('delete_project')
def delete_project(company_id, project_id):
    db = get_db()
    proj = db.execute("SELECT * FROM projects WHERE id=? AND company_id=?", (project_id, company_id)).fetchone()
    if proj:
        db.execute("DELETE FROM files WHERE repo_id=?", (proj['repo_id'],))
        db.execute("DELETE FROM branches WHERE repo_id=?", (proj['repo_id'],))
        db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        db.execute("DELETE FROM repositories WHERE id=?", (proj['repo_id'],))
        db.commit()
        flash('Projet et dépôt associé supprimés.', 'success')
    return redirect(url_for('projets', company_id=company_id))

# ---------------------------------------------------------------------------
# Routes — Paramètres
# ---------------------------------------------------------------------------

@app.route('/app/<int:company_id>/parametres', methods=['GET', 'POST'])
@permission_required('edit_company')
def parametres(company_id):
    db = get_db()
    company = get_company(company_id)
    if request.method == 'POST':
        db.execute("UPDATE companies SET name=?, description=?, logo=?, homepage_text=? WHERE id=?",
                   (request.form['name'].strip(), request.form.get('description', '').strip(),
                    request.form.get('logo', '🏢').strip() or '🏢',
                    request.form.get('homepage_text', '').strip(), company_id))
        db.commit()
        flash('Fiche entreprise mise à jour.', 'success')
        return redirect(url_for('company_dashboard', company_id=company_id))
    return render_template_string(PARAMETRES_TEMPLATE, company=company, my_companies=get_my_companies())

# ---------------------------------------------------------------------------

if __name__ == '__main__':
    init_db()
    print("=================================================================")
    print("🏢 CorpSuite lancé sur http://127.0.0.1:5001")
    print("🔗 Base de données partagée avec Mini GitHub Pro : minigithub.db")
    print("   Lancez aussi `python github.py` (port 5000) pour ouvrir le code")
    print("   des projets créés depuis CorpSuite.")
    print("💡 Aucune entreprise n'existe encore : rendez-vous sur /fonder")
    print("   pour créer la première et devenir son PDG.")
    print("=================================================================")
    app.run(debug=True, port=5001)