# Passation — backend Django

> État au 27 septembre 2026. Pour la personne qui reprend le backend Django (ou qui le rejoint).
> Remplace `PASSATION_VENDEURS.md` (juillet 2026), qui ne couvrait que les deux premiers modules.
> Document vivant : corrige-le au fur et à mesure plutôt que d'en créer un nouveau.

## 1. Où en est le backend

Tous les modules métier sont implémentés, refactorisés un par un (compréhension → correction → tests → sécurité), et documentés :

| App | Rôle | Document |
|---|---|---|
| `utilisateurs` | comptes, JWT (+ Google), OTP par email, mot de passe oublié, KYC chiffré ; route interne de vérification du jeton pour FastAPI | [`MODULE_UTILISATEURS.md`](./MODULE_UTILISATEURS.md) |
| `vendeurs` | demandes vendeur, boutiques, suspension | [`MODULE_VENDEURS.md`](./MODULE_VENDEURS.md) |
| `catalogue` | catégories, produits, variantes, stock, images, modération | [`MODULE_CATALOGUE.md`](./MODULE_CATALOGUE.md) |
| `panier` | panier connecté ou anonyme | [`MODULE_PANIER.md`](./MODULE_PANIER.md) |
| `commandes` | checkout (une commande par boutique), machine à états, expiration | [`MODULE_COMMANDES.md`](./MODULE_COMMANDES.md) |
| `paiements` | fournisseur interchangeable (CinetPay, simulé), webhooks signés, remboursements, reversements | [`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md) |
| `livraison` | tarifs par commune, livreurs, code de livraison, contestation | [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) |
| `retours` | retours sous 7 jours après livraison, remboursement | [`MODULE_RETOURS.md`](./MODULE_RETOURS.md) |
| `fidelite` | points après le délai de rétractation, coupons | [`MODULE_FIDELITE.md`](./MODULE_FIDELITE.md) |
| `notifications` | notifications en base et emails asynchrones | [`MODULE_NOTIFICATIONS.md`](./MODULE_NOTIFICATIONS.md) |
| `support` | tickets, messages, pièces jointes | [`MODULE_SUPPORT.md`](./MODULE_SUPPORT.md) |
| `passeport_qr` | passeports d'authenticité, vérification publique | [`MODULE_PASSEPORT_QR.md`](./MODULE_PASSEPORT_QR.md) |
| `core` | chiffrement de champs, validateurs d'upload, IP client, `ErreurMetier` | — |
| `demo` | commande `seed_demo` (dev et tests uniquement) | [`GUIDE_FRONTEND.md`](./GUIDE_FRONTEND.md) § 2 |

Chaque `MODULE_*.md` suit le même plan : responsabilités, contrat des endpoints, règles métier, sécurité, impact frontend, dette connue. **C'est le document à lire avant de toucher au module.**

## 2. Conventions transverses

- **Format d'erreur unique** `{success, status_code, detail, errors}` (`errors` toujours un objet) : `config/exceptions.py`. Une vue ne construit jamais une réponse d'erreur à la main ; elle lève une exception DRF ou `apps.core.exceptions.ErreurMetier(message, code_http)`. Seule exception : les webhooks des fournisseurs de paiement. Un test transversal le vérifie (`config/tests.py`).
- **Schéma OpenAPI** (drf-spectacular) : `backend-django/schema.yaml` est versionné, lu par le frontend. Après tout changement de contrat : `python manage.py spectacular --file schema.yaml`, sinon la CI échoue (schéma sans avertissement et à jour). Documentation interactive `/api/docs/` en dev, jamais en production (`DOCUMENTATION_API_ACTIVE` figé à `False` dans `prod.py`).
- **Logique métier dans `services.py`** quand elle est multi-étapes ou partagée (API, admin, tâches) ; les vues orchestrent. Pas de couche supplémentaire sans problème concret à résoudre.
- **Concurrence** : stock, argent, points et transitions d'état passent par `select_for_update()` ou des `UPDATE` conditionnels. Les tests de concurrence ne tournent que sur PostgreSQL.
- **Montants** : FCFA entiers (décimaux sans partie décimale).
- **Nommage** : identifiants et documents en français, comme le reste du backend ; messages de commit en anglais.

## 3. Démarrer

```bash
docker compose -f infra/docker-compose.yml up -d
docker exec anitche-backend python manage.py seed_demo   # données et comptes de démo
```

Détails (services, Mailpit, Swagger, comptes de démo) : [`GUIDE_FRONTEND.md`](./GUIDE_FRONTEND.md) et [`GUIDE_STRUCTURE_ANITCHE.md`](./GUIDE_STRUCTURE_ANITCHE.md).

Le fichier `backend-django/.env` local ne correspond pas forcément au Postgres de Docker : la stack de dev utilise `postgres` / `postgres` (base `anitche`).

## 4. Tests

Toujours sur **PostgreSQL** (le Postgres 16 de Docker), jamais sur SQLite : sous SQLite, les tests de verrouillage et certaines contraintes sont ignorés sans bruit.

```bash
cd backend-django
DJANGO_SETTINGS_MODULE=config.settings.ci DB_NAME=anitche_test DB_USER=postgres \
DB_PASSWORD=postgres DB_HOST=localhost python manage.py test --noinput -v 2
```

Vérifier en `-v 2` que chaque test affiche `ok` et non `skipped`. La suite complète compte environ 800 tests.

**CI** (`.github/workflows/ci-django.yml`, PostgreSQL 16 + Redis) : `makemigrations --check`, schéma OpenAPI validé et à jour, puis la suite complète.

**Collections Postman** (racine du dépôt local, lancées avec Newman contre la stack de dev) : une par module, plus `postman_0_setup_vendeur.json` pour préparer un vendeur. Le mot de passe administrateur n'est jamais enregistré dans les collections : le fournir à l'exécution (`--env-var admin_password=…`).

## 5. Règles à ne pas casser

1. **`statut_kyc` est la seule source de vérité de l'état vendeur**, et rôle + statut bougent ensemble, uniquement dans `apps/vendeurs/services.py`.
2. **Visibilité publique** : `Boutique.est_publiable` et `Produit.objects.publies()` portent la règle ; ne jamais re-tester `role` ou `statut_kyc` ailleurs.
3. **Paiement** : le montant est toujours calculé par le serveur ; une notification de fournisseur n'est crue qu'après signature et vérification de la transaction. Le fournisseur simulé est refusé au démarrage en production.
4. **Données sensibles** : pièces KYC, photos de retour et pièces jointes ne sont servies que par leurs vues authentifiées, jamais par une URL média directe. Numéros mobile money chiffrés au repos (rotation : `rechiffrer_donnees_sensibles`).
5. **Production** : `prod.py` refuse de démarrer sans vraie `SECRET_KEY`, clés de chiffrement, `ALLOWED_HOSTS`/`CORS` explicites et fournisseur de paiement réel. Ne jamais contourner ces garde-fous.
6. **Authentification de FastAPI** : FastAPI ne valide pas les JWT lui-même, il appelle la route interne `GET /api/utilisateurs/jeton/verification/` (`{id, role}`, limite `service_fastapi` qui ne consomme pas la limite `user`, hors schéma OpenAPI). Il l'appelle directement sur le réseau Docker, en HTTP : c'est la seule route exemptée de la redirection HTTPS (`SECURE_REDIRECT_EXEMPT` dans `prod.py`), et `backend-django` doit figurer dans `ALLOWED_HOSTS`. Elle n'est pas destinée au frontend et doit être bloquée publiquement par nginx. Détails : [`MODULE_UTILISATEURS.md`](./MODULE_UTILISATEURS.md) § 3 bis.

## 6. Pièges rencontrés

- **Multipart et booléens** : par défaut, DRF lit un booléen absent d'un formulaire comme `false` (une boutique créée avec son logo naissait fermée, un `PUT` multipart la fermait). C'est corrigé globalement dans `apps/core/apps.py` (`BooleanField.default_empty_html`) : absent = non fourni, comme en JSON. Ne pas retirer ce réglage ; `config/tests.py::BooleensMultipartTests` le vérifie pour tous les serializers.
- **404 génériques** : `get_object_or_404` produit « No Commande matches the given query. » ; `config/exceptions.py` le remplace par « Ressource introuvable. » (sans nom de modèle). Un `Http404("message")` écrit par une vue est conservé.
- **Montants dans le schéma** : `config/schema.py` retire le signe moins du motif des décimaux (sauf `DECIMAUX_SIGNES`) et fixe des exemples FCFA réalistes. Un nouveau montant pouvant être négatif doit être ajouté à `DECIMAUX_SIGNES`.
- **`apps.py`** : `name = 'apps.<nom>'`, sinon Django ne retrouve pas l'app.
- **Tests et `DEBUG`** : le lanceur de tests force `DEBUG=False` ; un test qui en dépend doit utiliser `override_settings(DEBUG=True)`.
- **`transaction.on_commit`** : les emails et notifications partent après le commit ; dans un `TestCase`, utiliser `captureOnCommitCallbacks` pour les vérifier.

## 7. Suite et dette

- **Dette par module** : section « Dette connue » de chaque `MODULE_*.md`.
- **Transverse** : pas de `.env.example` dans `backend-django/` ; pas de linter Python en CI.
- **Points ouverts pour l'étape hébergement** : bloquer publiquement `/api/utilisateurs/jeton/verification/` dans nginx (`location = … { return 404; }`) ; `infra/scripts/check_prod_env.sh` exige désormais `FASTAPI_DB_PASSWORD` (rôle PostgreSQL en lecture seule de FastAPI, pas encore créé) : le déploiement échoue tant qu'elle n'est pas définie dans `infra/.env`.
- **Capacité** : aucune mesure de charge n'a encore été faite. La cible (environ 1 000 utilisateurs simultanés) devra être démontrée par des tests de charge (scénarios, métriques, goulots, corrections, nouvelle mesure), jamais supposée à partir du code.
- **Collections Postman** : `vendeurs.postman_collection.json` suppose une base vierge (inscription et KYC du vendeur de test) et ne se rejoue pas telle quelle sur une base de dev déjà utilisée ; `postman_0_setup_vendeur.json` attend `base_url` en variable d'environnement.
