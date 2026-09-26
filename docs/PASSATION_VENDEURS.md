# Passation — backend Django, périmètre vendeurs

> État au 25 juillet 2026. Rédigé pour la personne qui reprend le backend Django après le module vendeurs.
> Document vivant : corrige-le au fur et à mesure plutôt que d'en créer un nouveau.

## 1. Où en est le backend Django

```
apps/utilisateurs   ✅ terminé   comptes, KYC, OTP, JWT, mot de passe oublié
apps/vendeurs       ✅ terminé   boutiques, validation vendeur, back-office
apps/catalogue      ⬜ vide      prochaine brique — dépend de vendeurs
apps/panier         ⬜ vide
apps/commandes      ⬜ vide
apps/paiements      ⬜ vide
apps/livraison      ⬜ vide
apps/retours        ⬜ vide
apps/fidelite       ⬜ vide
apps/notifications  ⬜ vide      des points d'accroche l'attendent déjà (§5)
apps/support        ⬜ vide
apps/passeport_qr   ⬜ vide
```

Les apps vides sont des squelettes `startapp`, déjà déclarés dans `INSTALLED_APPS`.

## 2. Ce qui a été livré côté vendeurs

Commit `2af337c` — `feat(vendeurs): boutique, validation vendeur et back-office — sprint 1`.

| Fichier | Ce qu'il contient |
|---|---|
| `models.py` | `Boutique` (une par compte, slug auto, `est_publiable`) et `DemandeVendeur` (proxy de `Utilisateur` filtré sur `en_attente`) |
| `services.py` | `valider_demande_vendeur()` / `refuser_demande_vendeur()` — **le seul endroit** où le couple rôle/statut est écrit |
| `permissions.py` | `EstVendeurValide`, `EstAdministrateur`, `EstProprietaireDeLaBoutique` |
| `serializers.py` | vue publique / vue propriétaire / vue back-office + lecture du dossier KYC |
| `views.py`, `urls.py` | 11 endpoints sous `/api/vendeurs/`, séparés par niveau d'accès |
| `admin.py` | boutiques + file de demandes en lecture seule, avec actions valider / refuser |
| `tests.py` | 35 tests |

Le détail du contrat d'API est dans [`MODULE_VENDEURS.md`](./MODULE_VENDEURS.md) — c'est le document à lire avant de brancher quoi que ce soit dessus.

**Le trou qui a été comblé :** avant ce module, rien ne faisait passer un compte de `en_attente` à `valide`, et `role` n'était jamais mis à `vendeur`. La validation vendeur passe désormais par `POST /api/vendeurs/administration/demandes/<id>/valider/`.

## 3. Les deux règles à ne pas casser

1. **`statut_kyc` est la seule source de vérité de l'état vendeur.** Aucun autre modèle ne stocke un statut de validation. `Boutique.est_publiable` est une propriété dérivée, pas un champ.
2. **Rôle et statut bougent ensemble.** Une validation écrit `statut_kyc = valide` **et** `role = vendeur` dans la même transaction, dans `services.py`. Ne jamais écrire ces deux champs à la main ailleurs — ni dans une vue, ni dans l'admin, ni dans un script.

## 4. Démarrer après le clone

```bash
cd backend-django
python -m venv venv && venv\Scripts\activate     # Windows
pip install -r requirements.txt
# créer un .env à partir des variables lues dans config/settings/base.py
python manage.py migrate
python manage.py test
python manage.py runserver
```

Il n'y a **pas de `.env.example`** dans `backend-django/` : les variables attendues sont `SECRET_KEY`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `REDIS_URL` (voir `config/settings/base.py`). En créer un serait un bon premier geste.

La CI (`.github/workflows/ci-django.yml`) lance `makemigrations --check --dry-run` puis `manage.py test` sur PostgreSQL 16. Une migration oubliée fait échouer le build.

## 5. La suite, par ordre logique

### a. `apps/catalogue` — la prochaine brique, celle qui dépend de vendeurs

C'est la seule app dont le point d'accroche est déjà prêt :

```python
# apps/catalogue/models.py
boutique = models.ForeignKey('vendeurs.Boutique', related_name='produits',
                             on_delete=models.CASCADE)

# apps/catalogue/views.py
from apps.vendeurs.permissions import EstVendeurValide
permission_classes = [IsAuthenticated, EstVendeurValide]

# publication d'un produit
if not produit.boutique.est_publiable:
    ...  # refuser
```

⚠️ **Ne pas re-tester `role` ou `statut_kyc` à la main** dans le catalogue. Si la règle de publication change un jour, elle doit changer à un seul endroit : `Boutique.est_publiable`.

Restent à définir côté catalogue (rien n'est spécifié dans `docs/`) : catégories, variantes, gestion du stock, modération des annonces.

### b. `apps/notifications` — les points d'accroche existent déjà

Trois endroits attendent un envoi réel, tous marqués dans le code :

| Où | Quoi |
|---|---|
| `apps/utilisateurs/views.py` (2 `TODO`) | envoi du code OTP par email / SMS |
| `apps/vendeurs/services.py`, fin de `valider_demande_vendeur()` | prévenir le vendeur que sa demande est acceptée |
| `apps/vendeurs/services.py`, fin de `refuser_demande_vendeur()` | lui transmettre le motif du refus (`commentaire_admin`) |

Celery et Redis sont déjà configurés (`config/celery.py`, `CELERY_BROKER_URL`), et `apps/utilisateurs/tasks.py` donne le modèle d'une tâche (`nettoyer_otp_expires`).

### c. Décisions produit à faire arbitrer

Six choix ont été tranchés par défaut faute de spec écrite — ils sont listés en section 6 de [`MODULE_VENDEURS.md`](./MODULE_VENDEURS.md). Les deux qui coûteraient une migration si l'équipe tranche autrement :

- **une seule boutique par compte** (`OneToOne` → `ForeignKey` sinon) ;
- **boutique créable seulement après validation** (au lieu d'un brouillon préparé pendant l'instruction du KYC).

À poser en réunion avant que le catalogue ne s'appuie dessus.

### d. Dette et manques identifiés

- Le **backlog des 18 epics n'est pas versionné** : `docs/` ne contient que le guide de structure et les deux documents vendeurs. Chaque nouveau module repart donc de zéro sur les règles métier. Y remédier ferait gagner du temps à tout le monde.
- Pas de `.env.example` côté `backend-django` (voir §4).
- Pas de linter Python en CI (le frontend a ESLint, pas le backend).
- Aucune pagination sur les listes DRF : `/api/vendeurs/boutiques/` renverra tout le catalogue de boutiques le jour où il y en aura mille. À traiter globalement via `REST_FRAMEWORK['DEFAULT_PAGINATION_CLASS']` plutôt que vue par vue — **attention, ça changera la forme des réponses déjà consommées par le frontend**.

## 6. Pièges rencontrés, pour ne pas les revivre

- **`apps.py`** : `name` doit valoir `apps.<nom_app>` et non `<nom_app>`, sinon Django ne retrouve pas l'app déplacée dans `apps/` (déjà signalé dans le guide de structure).
- **Modèle proxy** : `DemandeVendeur` ne crée aucune table. Sa migration ne fait que déclarer le proxy — c'est normal qu'elle paraisse vide.
- **`makemigrations` fonctionne sans base de données** (un avertissement s'affiche mais la migration est générée), contrairement à `test` et `migrate`.
- **`clean()` de `Boutique`** ne s'applique qu'à la création. C'était volontaire : sinon une boutique dont le vendeur est suspendu deviendrait impossible à corriger depuis l'admin.

## 7. Contacts / responsabilités

| Périmètre | Qui |
|---|---|
| `apps/utilisateurs` | auteur du sprint 1 utilisateurs |
| `apps/vendeurs` | Jordan |
| suite du backend Django | à attribuer |

Pour toute question sur le module vendeurs, l'ordre de lecture conseillé est :
`MODULE_VENDEURS.md` → `services.py` → `models.py` → `tests.py`.
