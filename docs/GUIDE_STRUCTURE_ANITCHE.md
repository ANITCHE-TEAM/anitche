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
│   ├── config/settings/{base,dev,prod}.py
│   ├── apps/                     # un dossier par epic du backlog
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
- FastAPI   → http://localhost:8001/docs
- Mailpit   → http://localhost:8025 (emails envoyés en dev)

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

Pour que les Runs Postman répétés (connexions, codes OTP, inscriptions à chaque exécution) ne soient pas bloqués en 429, `config/settings/dev.py` relève quatre limites :

| Scope | Dev | Production (`base.py`) |
|---|---|---|
| `login` | 1000/hour | 10/hour |
| `otp_envoi` | 1000/hour | 5/hour |
| `otp_verification` | 1000/hour | 10/hour |
| `inscription` | 1000/hour | 10/hour |

- Ces valeurs ne concernent **que le dev** : `prod.py` et `ci.py` ne chargent jamais `dev.py` et ne redéfinissent aucune limite, donc la production applique celles de `base.py`. Des tests le vérifient (`TauxDeLimiteParEnvironnementTests` dans `apps/core/tests.py`, `LimitesDeProductionTests` dans `apps/utilisateurs/tests.py`).
- Toutes les autres limites (`kyc`, `anon`, `user`, `boutique_creation`…) restent identiques en dev et en production.
- Ne jamais copier ces valeurs dans `base.py` ou `prod.py` : ce sont elles qui protègent la connexion et les codes OTP contre le bourrage d'identifiants et la force brute.

## 5. Déploiement en production

```bash
docker compose -f infra/docker-compose.prod.yml --env-file infra/.env up -d --build
```

- **Nginx** en reverse proxy devant tout : sert le build React statique (`/`), route `/api/` vers Django et `/fast/` vers FastAPI.
- **Gunicorn** pour Django, **Uvicorn** pour FastAPI, chacun dans son conteneur.
- **PostgreSQL managé** si possible (Neon, Supabase, Railway) plutôt que self-hosté au début.
- **Redis** pour Celery + cache.
- `infra/scripts/deploy.sh` automatise pull + rebuild + redémarrage.
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
