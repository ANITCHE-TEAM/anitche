# Module vendeurs — contrat et règles

> Périmètre : Jordan. Backend Django, `backend-django/apps/vendeurs/`.
> Document vivant : à corriger dès qu'une règle est arbitrée en réunion.

## 1. Ce sur quoi le module s'appuie (module utilisateurs — inchangé)

| Élément | Où | Rôle |
|---|---|---|
| `statut_kyc` (`non_soumis`, `en_attente`, `valide`, `refuse`) | `apps/utilisateurs/models.py` | **Source de vérité** de l'état vendeur |
| `role` (`client` par défaut, `vendeur`, …) | idem | Rôle unique du compte |
| `Utilisateur.soumettre_demande_vendeur()` | idem | Fait passer le compte à `en_attente` |
| `DocumentKYC` (`dossier_kyc`) + `date_traitement`, `commentaire_admin` | idem | Dossier instruit par l'administration |
| `POST /api/utilisateurs/upload-kyc/` | `apps/utilisateurs/urls.py` | Dépôt de la demande (l'upload passe directement le compte à `en_attente`) |

Le module vendeurs **ne duplique aucun statut de validation** et ne modifie pas le module utilisateurs.

## 2. Cycle de vie

```
client (statut_kyc = non_soumis)
   │  POST /api/utilisateurs/upload-kyc/   (module utilisateurs)
   ▼
en_attente ──── refus admin ────▶ refuse ──(nouvelle demande possible)──┐
   │                                                                     │
   │ validation admin                                                    │
   ▼                                                                     │
valide + role = vendeur ──▶ peut créer sa boutique ──▶ peut publier      │
                                                                         │
                              ◀──────────────────────────────────────────┘
```

- **Validation** : `statut_kyc = valide` **et** `role = vendeur`, dans la même transaction. Les deux champs ne peuvent jamais diverger.
- **Refus** : `statut_kyc = refuse`, le rôle n'est pas touché (le compte reste client).
- Toute décision est tracée dans `DocumentKYC.date_traitement` / `commentaire_admin`.
- Les transitions ne partent que de l'état `en_attente` : hors de cet état, le service lève `TransitionVendeurImpossible` (→ HTTP 400).

Point d'entrée unique des transitions : `apps/vendeurs/services.py`. L'API et le django-admin l'appellent tous les deux — aucune écriture manuelle du couple rôle/statut ailleurs.

## 3. Modèle

`Boutique` — une par compte (`OneToOneField` vers `AUTH_USER_MODEL`, `related_name='boutique'`).

- Champs : `nom` (unique), `nom_normalise` (unique, calculé), `slug` (auto, unique), `description`, `logo`, `banniere`, `telephone_contact`, `email_contact`, `adresse`, `ville`, `est_active`, `est_suspendue`, dates.
- `est_active` : fermeture volontaire, décidée par le vendeur (modifiable via `ma-boutique/`).
- `est_suspendue` : suspension décidée par l'administration. Le vendeur la voit mais ne peut pas la lever, et sa boutique est **gelée** tant qu'elle dure (voir § 4).
- `nom_normalise` : forme de comparaison du nom (casse, accents, espaces et ponctuation ignorés : « Chez Awa », « chez-awa », « CHÉZ AWA ! » → `chezawa`). Recalculée à chaque `save()`, jamais saisie. Sert à refuser les noms quasi identiques (usurpation d'une boutique existante). Règles, communes à l'API et au django-admin (`verifier_nom_boutique_disponible`) :
  - nom sans aucune lettre ni chiffre → refusé ;
  - nom **identique** à un nom existant, à la **casse** et aux **espaces répétés ou en bordure** près → « Ce nom de boutique est déjà utilisé. » (ex. « Chez Awa » / « chez  awa » / «  CHEZ AWA  ») ;
  - sinon, même `nom_normalise` (différence d'**accents**, de **ponctuation** ou d'espacement entre les mots) → **trop proche**, message distinct invitant à choisir un nom plus éloigné (ex. « Chez Awa » / « Chéz Awa! » / « Chez-Awa » / « ChezAwa »).
  Une boutique peut changer la casse ou la ponctuation de son propre nom. La contrainte unique en base reste le filet en cas de course (→ 400, jamais 500).
- `boutique.est_publiable` → `est_active` **et** non `est_suspendue` **et** compte actif **et** vendeur validé. **C'est le seul test à utiliser par les autres modules.** Seule exception assumée : `Produit.objects.publies()` (catalogue) en duplique la traduction SQL et doit suivre toute évolution de cette règle.
- `Boutique.objects.publiques()` : queryset des boutiques visibles côté client.
- `DemandeVendeur` : modèle **proxy** de `Utilisateur` filtré sur `statut_kyc = en_attente`. Aucune table, aucune donnée dupliquée — juste une file de traitement.

## 4. Endpoints — `/api/vendeurs/`

### Public (`AllowAny`)
| Méthode | URL | Description |
|---|---|---|
| GET | `boutiques/` | Boutiques publiables. Filtres : `?recherche=` (nom), `?ville=` |
| GET | `boutiques/<slug>/` | Fiche publique (404 si non publiable) |

### Vendeur authentifié (`IsAuthenticated` + `EstVendeurValide` + propriétaire)
| Méthode | URL | Description |
|---|---|---|
| POST | `ma-boutique/` | Crée la boutique (une seule par compte → 400 sinon) |
| GET | `ma-boutique/` | Sa boutique (404 si aucune) |
| PATCH / PUT | `ma-boutique/` | Mise à jour, y compris fermeture / réouverture (`est_active`). `proprietaire`, `slug` et `est_suspendue` non modifiables (ignorés). **403** si la boutique est suspendue |

- Un compte dont le KYC n'est pas (ou plus) validé reçoit **403** sur toutes ces routes, lecture comprise (`EstVendeurValide`) — y compris s'il possède déjà une boutique.
- **Boutique suspendue = gelée côté vendeur** (`BoutiqueNonSuspendue`) : `GET` reste possible (le vendeur voit `est_suspendue: true`), toute écriture renvoie **403** avec un message explicite — contenu comme fermeture/réouverture (`est_active`). L'écriture redevient possible dès la levée de la suspension.
- Images : le logo est limité à **2 Mo** par l'API (règle du serializer, volontairement absente du modèle pour ne pas contraindre le django-admin). La bannière suit la règle du modèle (`validateur_image_standard` : 5 Mo, JPEG/PNG/WebP, contenu vérifié).

### Administration (`IsAuthenticated` + `EstAdministrateur` : rôle `admin` ou `super_admin` uniquement — `is_staff` seul ne donne pas accès)
| Méthode | URL | Description |
|---|---|---|
| GET | `administration/demandes/` | Demandes en attente + dossier KYC joint |
| POST | `administration/demandes/<id>/valider/` | Corps : `{"commentaire": "…"}` (optionnel) |
| POST | `administration/demandes/<id>/refuser/` | Corps : `{"commentaire": "…"}` — **obligatoire** |
| GET | `administration/boutiques/` | Toutes les boutiques, fermées et suspendues comprises |
| GET / PATCH | `administration/boutiques/<id>/` | Suspendre / lever la suspension : corps `{"est_suspendue": true/false}`. Tout autre champ → **400** « Seul le champ est_suspendue est modifiable ici » |

Un compte hors file d'attente renvoie **404** sur les deux routes de décision.

## 5. Dépendances avec les autres modules

- **catalogue** (à venir) : rattacher le produit à la boutique via `models.ForeignKey('vendeurs.Boutique', related_name='produits')`, et protéger les écritures avec `apps.vendeurs.permissions.EstVendeurValide`. Côté publication, tester `boutique.est_publiable` — ne pas retester `role`/`statut_kyc` à la main.
- **commandes / paiements / livraison** : le vendeur d'une ligne de commande se retrouve par `produit.boutique.proprietaire`.
- **notifications** : aucun envoi n'est branché sur les décisions vendeur pour l'instant. Le point d'accroche naturel est `apps/vendeurs/services.py` (fin de `valider_demande_vendeur` / `refuser_demande_vendeur`), comme le fait déjà le module utilisateurs avec ses `TODO` d'envoi OTP.
- **frontend** : la validation vendeur n'existait nulle part avant ce module — c'est bien `/api/vendeurs/administration/demandes/…` qui fait passer un compte de `en_attente` à `valide`.

## 6. Choix par défaut, à confirmer avec l'équipe

Aucune spec détaillée vendeur n'est présente dans `docs/` (le backlog des 18 epics n'est pas versionné). Les points suivants ont donc été tranchés par défaut, en restant au plus près du modèle existant :

1. **Une seule boutique par compte** (`OneToOne`). Si le produit veut plusieurs boutiques par vendeur, il faut passer en `ForeignKey`.
2. **Création de boutique réservée aux vendeurs déjà validés.** Alternative possible : laisser préparer une boutique en brouillon pendant l'instruction du KYC.
3. **Motif obligatoire au refus**, optionnel à la validation (traçabilité côté `commentaire_admin`).
4. **Fermeture vendeur et suspension admin sont deux champs distincts** (`est_active` / `est_suspendue`). Pendant une suspension, la boutique est entièrement gelée côté vendeur (lecture seule, 403 sur toute écriture, `est_active` compris). L'administration, elle, ne modifie jamais le contenu de la boutique.
   - ⚠️ **Dette connue** : pendant une suspension, **personne ne peut corriger le contenu** via l'API — ni le vendeur (gelé), ni l'administration (`administration/boutiques/<id>/` n'accepte que `est_suspendue`). Seule issue aujourd'hui : lever la suspension pour laisser le vendeur corriger. (Le django-admin `/admin/` permet techniquement d'éditer tous les champs à un compte staff disposant des permissions Django : c'est un accès technique, pas un flux métier.) À traiter si un besoin de modération de contenu apparaît.
   - ~~Les écritures du **catalogue** ne testaient pas `est_suspendue`.~~ **Résolu** (refonte du catalogue, septembre 2026) : `BoutiqueDuVendeurNonSuspendue` (`apps/vendeurs/permissions.py`, partagée avec `passeport_qr`) refuse toute écriture (403) d'un vendeur dont la boutique est suspendue ; la lecture reste possible. Voir `MODULE_CATALOGUE.md`.
5. **Un livreur ou un administrateur ne peut pas devenir vendeur** : le modèle utilisateur ne porte qu'un rôle unique, donc la validation refuse d'écraser ces rôles. À arbitrer si le cas se présente.
6. **Aucune rétrogradation** d'un vendeur déjà validé n'est prévue (pas de flux « retirer le statut vendeur » dans le projet). L'administration peut seulement suspendre la boutique.

## 7. Tests

`backend-django/apps/vendeurs/tests.py` — 69 tests : modèle et visibilité, unicité normalisée du nom, services de décision, boutique publique (dont filtres `ville` et `recherche`), espace vendeur (dont gel pendant suspension et perte du KYC), limites d'images, throttling, back-office, concurrence.

⚠️ **Toujours lancer les tests sur PostgreSQL.** Sans `DJANGO_SETTINGS_MODULE`, `manage.py test` bascule sur `config.settings.test` (SQLite) : `test_deux_creations_simultanees_une_seule_acceptee` y est alors **sauté silencieusement** (`select_for_update()` est un no-op sous SQLite). Il faut forcer `config.settings.ci` :

Depuis la machine hôte (Postgres de `infra/docker-compose.yml` démarré, port 5432 exposé) :

```bash
cd backend-django
DJANGO_SETTINGS_MODULE=config.settings.ci DB_NAME=anitche_test DB_USER=postgres DB_PASSWORD=postgres DB_HOST=localhost   python manage.py test apps.vendeurs -v 2
```

Ou dans le conteneur backend :

```bash
docker compose -f infra/docker-compose.yml exec -e DJANGO_SETTINGS_MODULE=config.settings.ci backend-django python manage.py test apps.vendeurs -v 2
```

Retirer `apps.vendeurs` pour lancer toute la suite. Vérifier dans la sortie `-v 2` que le test de concurrence affiche `ok` et non `skipped`.
