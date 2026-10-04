# ANITCHE — Guide de structure du projet

> Document vivant, à faire évoluer avec le projet.

Ce document existe pour une seule raison : que chaque personne de l'équipe — Frontend, Backend, Design — ouvre le repo et comprenne en cinq minutes où est chaque chose, et pourquoi elle est là. Pas de flou, pas de "je demande sur WhatsApp".

## 1. Vue d'ensemble

ANITCHE repose sur trois briques qui travaillent ensemble :

```
React (Frontend)
  │
  ├──▶ Django   → coeur métier : utilisateurs, catalogue, panier,
  │               commandes, paiements, retours, fidélité, admin
  │
  └──▶ FastAPI  → vitesse : recherche, IA, QR code, temps réel
        │
        PostgreSQL + Redis
```

**Pourquoi deux backends ?**
Django gère le classique : un ORM puissant, un admin prêt à l'emploi, idéal pour la logique métier stable. FastAPI prend ce qui doit être rapide et asynchrone : recherche photo/texte, recommandations IA, scan QR, suivi de livraison en direct.

## 2. Prérequis

| Outil | Usage |
|---|---|
| Node.js (v18+) | Frontend React |
| Python (3.11+) | Django et FastAPI |
| PostgreSQL | Base de données |
| Redis | Cache + tâches en arrière-plan (Celery) |
| Git | Gestion du code |
| Docker (conseillé) | Lancer tout le projet d'un coup |

## 3. Structure complète

```
anitche/
├── frontend/                    # Application React
│   ├── src/
│   │   ├── composants/
│   │   │   ├── ui/              # boutons, cartes, champs réutilisables
│   │   │   └── mise-en-page/    # header, footer, structure des pages
│   │   ├── fonctionnalites/     # un dossier par epic du backlog
│   │   ├── hooks/
│   │   ├── services/            # appels API (axios)
│   │   ├── store/                # état global (Zustand)
│   │   ├── routes/
│   │   └── App.jsx
│   └── package.json
│
├── backend-django/               # Coeur métier
│   ├── config/settings/{base,dev,prod,test,ci}.py
│   ├── apps/                     # un dossier par epic du backlog
│   │   └── demo/                 # seed_demo : données de démo (dev et tests seulement)
│   ├── schema.yaml               # contrat OpenAPI versionné (lu par le frontend, vérifié en CI)
│   └── manage.py
│
├── backend-fastapi/               # Services rapides / IA
│   └── app/
│       ├── main.py
│       ├── routeurs/
│       ├── services/
│       └── modeles/               # schémas Pydantic
│
├── infra/                          # Déploiement
│   ├── docker-compose.yml
│   ├── docker-compose.prod.yml
│   ├── nginx/nginx.conf
│   └── scripts/{deploy.sh, backup_db.sh}
│
├── .github/workflows/               # Tests automatiques (CI/CD)
│
└── docs/                              # Backlog, specs, ce guide
```

**Règle de nommage à retenir** : les dossiers métier sont en français, pour que toute l'équipe s'y retrouve — y compris design. Les fichiers de code (`models.py`, `views.py`, `ProductCard.jsx`) restent en anglais : c'est la convention universelle attendue par Django et React.

## 4. Démarrer après le clone

### Avec Docker (recommandé)

```bash
docker compose -f infra/docker-compose.yml up
```

- Frontend  → http://localhost:5173
- Django    → http://localhost:8000
- Swagger   → http://localhost:8000/api/docs/ (documentation de l'API Django, dev uniquement)
- FastAPI   → http://localhost:8001/docs
- Mailpit   → http://localhost:8025 (emails envoyés en dev)

Puis, pour avoir des boutiques, des produits, des commandes et un compte par rôle :

```bash
docker exec anitche-backend python manage.py seed_demo
```

Comptes, contenu et conventions de l'API (JWT, erreurs, pagination) : [`GUIDE_FRONTEND.md`](./GUIDE_FRONTEND.md).

### Sans Docker

| Équipe | Commandes |
|---|---|
| Frontend | `cd frontend && npm install && npm run dev` |
| Backend Django | `cd backend-django` → venv → `pip install -r requirements.txt` → `migrate` → `runserver` |
| Backend FastAPI | `cd backend-fastapi` → venv → `pip install -r requirements.txt` → `uvicorn app.main:app --reload` |

⚠️ **Piège courant** : après `startapp`, vérifie que `name` dans chaque `apps.py` pointe vers `apps.nom_de_lapp` (et non juste `nom_de_lapp`) — sinon Django ne retrouve pas l'app une fois déplacée dans `apps/`.

### Emails de dev (Mailpit)

En dev, aucun email ne sort vers l'extérieur : Django les envoie au service **Mailpit** du `docker-compose.yml` de dev, qui les garde pour consultation. Il n'existe pas dans `docker-compose.prod.yml`.

- **Voir les emails** : ouvrir http://localhost:8025. Chaque email envoyé par le backend (codes OTP d'inscription, de mot de passe oublié, de changement de contact, notifications) y apparaît, avec son destinataire et son contenu.
- **Envoi asynchrone** : les codes OTP et les alertes de connexion partent par le worker Celery (les notifications, elles, sont envoyées directement par le backend). Si un code n'arrive pas, vérifier que `celery-worker` et `mailpit` tournent (`docker compose -f infra/docker-compose.yml ps`).
- **Sans Docker** : `config/settings/dev.py` envoie en SMTP vers `localhost:1025`. Lancer Mailpit localement sur ce port, ou afficher les emails dans le terminal avec `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend`.
- **Collections Postman** : les requêtes « Lire le code … (Mailpit) » récupèrent les codes OTP via l'API de Mailpit (`GET http://localhost:8025/api/v1/search`). Aucune saisie manuelle n'est nécessaire, sauf pour la connexion Google et le mot de passe admin, à renseigner soi-même.

### Limites de débit relevées en dev uniquement

Pour que les Runs Postman répétés (connexions, codes OTP, inscriptions, paiements, retours et tickets à chaque exécution) ne soient pas bloqués en 429, `config/settings/dev.py` relève ces limites à 1000/hour :

| Scope | Production (`base.py`) |
|---|---|
| `login` | 10/hour |
| `otp_envoi` | 5/hour |
| `otp_verification` | 10/hour |
| `inscription` | 10/hour |
| `paiements` | 20/hour |
| `retour_creation`, `retour_photo` | 10/hour, 30/hour |
| `support_ticket`, `support_message`, `support_piece_jointe` | 10/hour, 60/hour, 20/hour |

- Ces valeurs ne concernent **que le dev** : `prod.py` et `ci.py` ne chargent jamais `dev.py` et ne redéfinissent aucune limite, donc la production applique celles de `base.py`. Des tests le vérifient (`TauxDeLimiteParEnvironnementTests` dans `apps/core/tests.py`, `LimitesDeProductionTests` dans `apps/utilisateurs/tests.py`).
- Toutes les autres limites (`kyc`, `anon`, `user`, `boutique_creation`…) restent identiques en dev et en production.
- Ne jamais copier ces valeurs dans `base.py` ou `prod.py` : ce sont elles qui protègent la connexion et les codes OTP contre le bourrage d'identifiants et la force brute.

## 5. Déploiement en production

```bash
./infra/scripts/deploy.sh
```

Toujours passer par `deploy.sh`, jamais par un `docker compose … up -d --build` direct : seul le script contrôle `infra/.env` et recrée le rôle PostgreSQL de FastAPI après les migrations. Étapes, arrêt à la première erreur :

1. `git pull origin main` ;
2. `infra/scripts/check_prod_env.sh` : refuse tout ce que refuseraient au démarrage `config/settings/prod.py` (Django et Celery) et les réglages de production de FastAPI (origines CORS, `FRONTEND_BASE_URL`, `PUBLIC_BASE_URL`, `MEDIA_BASE_URL`), plus un `FASTAPI_DB_PASSWORD` de moins de 12 caractères ou pas seulement alphanumérique, et un `DB_PASSWORD` d'exemple ou de moins de 12 caractères. Il affiche toutes les erreurs (noms des variables, jamais leurs valeurs) puis sort en 1, et avertit si `infra/.env` n'a pas les droits 600 ;
3. construction des images (`docker compose build`) ;
4. démarrage de `db` et `redis` seuls, jusqu'à ce qu'ils soient prêts (`up -d --wait db redis`) ;
5. migrations dans un conteneur ponctuel (`docker compose run --rm backend-django python manage.py migrate`) ;
6. `infra/scripts/create_fastapi_readonly_role.sh` (avec `COMPOSE_FILE=infra/docker-compose.prod.yml`) : crée ou met à jour le rôle `anitche_fastapi_ro`, son mot de passe (`FASTAPI_DB_PASSWORD`) et ses droits. À chaque déploiement, car ses `GRANT` portent sur des tables et des vues créées par les migrations : une migration qui recrée une vue lui retire ses droits ;
7. `up -d` de toute la pile, puis statut. `backend-django` relance `migrate` à son démarrage, sans effet après l'étape 5.

### Emails en production

Variables de `infra/.env` (modèle commenté : `infra/.env.example`), transmises à `backend-django`, `celery-worker` et `celery-beat` :

| Variable | Valeur | Refusé au démarrage |
|---|---|---|
| `EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` | tout autre backend (console, locmem, dummy, filebased) |
| `EMAIL_HOST` | serveur SMTP du fournisseur | vide ou absent (aucune valeur par défaut en production) |
| `EMAIL_PORT` | 587 (par défaut) ou 465 | |
| `EMAIL_USE_TLS`, `EMAIL_USE_SSL` | `True`, `False` (par défaut : STARTTLS sur 587) ; `False`, `True` pour 465 | les deux à `True`, ou les deux à `False` (identifiants SMTP en clair) |
| `EMAIL_TIMEOUT` | 10 secondes par défaut | hors de 1 à 60 |
| `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | identifiant et clé SMTP du compte (secrets) | vides ou absents |
| `DEFAULT_FROM_EMAIL` | `"ANITCHE <no-reply@anitche.com>"`, entre guillemets dans `infra/.env` | adresse hors `@anitche.com` |

- `prod.py` applique ces refus au démarrage de Django et de Celery ; `check_prod_env.sh` les applique avant le déploiement. `SERVER_EMAIL` (emails d'erreur de Django) reprend `DEFAULT_FROM_EMAIL`.
- Avant le premier envoi, publier pour `anitche.com` les enregistrements SPF, DKIM et DMARC demandés par le fournisseur : sans eux, les codes OTP arrivent en spam ou sont refusés.
- Codes OTP : 3 essais au plus sur erreur SMTP ou réseau, sans que le code apparaisse dans les journaux ([`MODULE_UTILISATEURS.md`](./MODULE_UTILISATEURS.md) § 5).
- Preuve attendue avant l'ouverture : un code OTP réellement reçu sur une vraie boîte.

### Composants

- **Nginx** en reverse proxy devant tout, sur un seul domaine (`https://anitche.com`) : `/` vers le build React statique, `/api/` et `/admin/` vers Django avec le chemin complet (sauf la route interne `/api/utilisateurs/jeton/verification`, 404), `/fast/` vers FastAPI sans le préfixe `/fast` (WebSocket compris). Il sert lui-même `/static/` (volume `anitche_static`, rempli par `collectstatic`) et les seuls médias publics sous `/media/` (images des produits et catégories, logos et bannières ; volume `anitche_media` en lecture seule) ; tout autre chemin sous `/media/` répond 404. Détail et liste des dossiers : en-tête de `infra/nginx/nginx.conf`.
- **Gunicorn** pour Django, **Uvicorn** pour FastAPI, chacun dans son conteneur.
- **PostgreSQL managé** si possible (Neon, Supabase, Railway) plutôt que self-hosté au début.
- **Redis** pour Celery + cache.
- `infra/scripts/deploy.sh` enchaîne contrôle, construction, migrations, rôle FastAPI et redémarrage (étapes ci-dessus).
- `infra/scripts/backup_db.sh` sauvegarde la base (à brancher sur un cron).

## 6. Aide-mémoire — où je mets quoi

| Je veux ajouter... | Je vais dans... |
|---|---|
| Un composant réutilisable | `frontend/src/composants/ui/` |
| Une fonctionnalité liée à un epic | `frontend/src/fonctionnalites/<nom-epic>/` |
| Un appel API | `frontend/src/services/` |
| Une table liée aux commandes | `backend-django/apps/commandes/models.py` |
| Une route de recherche rapide | `backend-fastapi/app/routeurs/recherche.py` |
| Un script de déploiement | `infra/scripts/` |
| Une doc / spec | `docs/` |

Une structure claire, c'est une équipe qui avance vite.
