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
| `POST /api/utilisateurs/upload-kyc/` puis `POST /api/utilisateurs/demande-vendeur/` | `apps/utilisateurs/urls.py` | Dépôt de la demande |

Le module vendeurs **ne duplique aucun statut de validation** et ne modifie pas le module utilisateurs.

## 2. Cycle de vie

```
client (statut_kyc = non_soumis)
   │  upload KYC + POST /api/utilisateurs/demande-vendeur/   (module utilisateurs)
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

- Champs : `nom` (unique), `slug` (auto, unique), `description`, `logo`, `banniere`, `telephone_contact`, `email_contact`, `adresse`, `ville`, `est_active`, dates.
- `est_active` : fermeture temporaire (par le vendeur) ou suspension (par l'administration).
- `boutique.est_publiable` → `est_active` **et** compte actif **et** vendeur validé. **C'est le seul test à utiliser par les autres modules.**
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
| PATCH / PUT | `ma-boutique/` | Mise à jour. `proprietaire` et `slug` non modifiables |

Un compte non validé reçoit **403** sur ces routes, y compris en lecture.

### Administration (`IsAuthenticated` + `EstAdministrateur` : rôle `admin`/`super_admin` ou `is_staff`)
| Méthode | URL | Description |
|---|---|---|
| GET | `administration/demandes/` | Demandes en attente + dossier KYC joint |
| POST | `administration/demandes/<id>/valider/` | Corps : `{"commentaire": "…"}` (optionnel) |
| POST | `administration/demandes/<id>/refuser/` | Corps : `{"commentaire": "…"}` — **obligatoire** |
| GET | `administration/boutiques/` | Toutes les boutiques, fermées comprises |
| GET / PATCH | `administration/boutiques/<id>/` | Suspendre / réactiver (`est_active`) |

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
4. **`est_active` partagé** entre fermeture par le vendeur et suspension par l'administration. Si une suspension non contournable par le vendeur est requise, il faudra deux champs distincts.
5. **Un livreur ou un administrateur ne peut pas devenir vendeur** : le modèle utilisateur ne porte qu'un rôle unique, donc la validation refuse d'écraser ces rôles. À arbitrer si le cas se présente.
6. **Aucune rétrogradation** d'un vendeur déjà validé n'est prévue (pas de flux « retirer le statut vendeur » dans le projet). L'administration peut seulement suspendre la boutique.

## 7. Tests

`backend-django/apps/vendeurs/tests.py` — 35 tests : modèle et visibilité, services de décision, boutique publique, espace vendeur, back-office.

```bash
cd backend-django
python manage.py test apps.vendeurs
```
