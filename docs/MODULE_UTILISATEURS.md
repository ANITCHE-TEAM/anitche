# Module utilisateurs — contrat et règles

> Périmètre : backend Django, `backend-django/apps/utilisateurs/`.
> Document vivant : à corriger dès qu'une règle est arbitrée en réunion.
>
> Ce module **n'a pas encore été repris** selon la méthode module par module (diagnostic confirmé par tests jetables, puis refonte) : ce document décrit l'existant tel qu'il est codé. Les points du § 9 viennent de la lecture du code ; ils n'ont pas encore été confirmés par des tests.

## 1. Rôle du module et dépendances

Comptes, authentification (JWT, Google), codes OTP et dossier KYC. C'est le socle des autres modules :

| Élément | Utilisé par | Pour |
|---|---|---|
| `Role` (`client`, `vendeur`, `livreur`, `moderateur`, `support`, `admin`, `super_admin`) | tous | Les permissions métier s'appuient sur le rôle. `ROLES_ADMINISTRATION = (admin, super_admin)` est défini dans `apps/vendeurs/permissions.py` |
| `StatutKYC` (`non_soumis`, `en_attente`, `valide`, `refuse`) | vendeurs, catalogue, passeport_qr, panier | **Source de vérité** de l'état vendeur (`EstVendeurValide`, `Boutique.est_publiable`) |
| `is_active` | vendeurs, catalogue | Un compte désactivé ne publie rien (`est_publiable`) et ne reçoit pas de jeton |
| `DocumentKYC` | vendeurs | Instruction des demandes vendeur (validation / refus : `MODULE_VENDEURS.md`) |

Le module dépend de `apps/core` (validateurs de fichiers, `CheminUploadUUID`) et de `rest_framework_simplejwt` (+ `token_blacklist`), `google-auth`, Celery (envoi des emails, tâches planifiées).

`is_staff` ne sert qu'au Django admin : aucun module ne l'utilise comme pouvoir métier.

## 2. Modèle

- **`Utilisateur`** (`AbstractBaseUser` + `PermissionsMixin`) : identifiant de connexion = `email` (unique). `telephone` unique (facultatif), `nom`, `prenom`, `role` (défaut `client`), `statut_kyc` (défaut `non_soumis`), `email_verifie`, `telephone_verifie`, `google_id` (unique, rempli si le compte est lié à Google), `is_active`, `is_staff`.
  - `soumettre_demande_vendeur()` : passe `statut_kyc` à `en_attente` ; lève `StatutsKYCImpossibles` si une demande est déjà en attente ou validée.
  - `UtilisateurManager.create_superuser()` : `is_staff`, `is_superuser` et rôle `super_admin` par défaut.
- **`DocumentKYC`** (un par utilisateur, `OneToOne`) : `type_piece`, `piece_identite_recto`, `piece_identite_verso` (facultatif en base), `selfie`, `numero_mobile_money`, `adresse`, `compte_bancaire` (facultatif), `date_soumission`, `date_traitement`, `commentaire_admin`.
  - Fichiers stockés sous un nom UUID (`CheminUploadUUID`) : le nom d'origine n'est jamais conservé.
  - Pièce d'identité : `validateur_document_kyc` (JPEG, PNG, PDF, **10 Mo**, signature binaire vérifiée). Selfie : `validateur_image_standard` (JPEG, PNG, WebP, **5 Mo**).
- **`CodeOTP`** : `code_hash` (le code n'est **jamais** stocké en clair), `type_usage` (`inscription`, `mdp_oublie`, `changement_email`, `changement_telephone`), `nouvelle_valeur` (changement de contact), `date_expiration`, `nombre_tentatives`, `utilise`.

## 3. Endpoints — `/api/utilisateurs/`

| Méthode | URL | Accès | Limite de débit | Description |
|---|---|---|---|---|
| POST | `inscription/` | AllowAny | `anon` (global) | Corps : `email`, `password`, `nom`, `prenom`, `telephone?`. Crée le compte (`email_verifie = false`) et envoie un OTP `inscription` à cette adresse. **201** |
| POST | `connexion/` | AllowAny | `login` | `email`, `password` → `{access, refresh}`. Notification email au titulaire à chaque connexion **réussie** (§ 5) |
| POST | `connexion/rafraichir/` | AllowAny | `anon` (global) | `TokenRefreshView` de simplejwt : `refresh` → nouveaux `access` et `refresh` (rotation, § 4) |
| POST | `connexion-google/` | AllowAny | `login` | `id_token` Google Identity Services → `{access, refresh}` (§ 6) |
| POST | `deconnexion/` | AllowAny, **sans authentification** | `logout` | `refresh` → mis en liste noire. **205**. Posséder le refresh token vaut preuve ; un access token expiré n'empêche pas de se déconnecter |
| GET / PUT / PATCH | `profil/` | IsAuthenticated | `user` (global) | Son propre profil : `id`, `email`, `nom`, `prenom`, `telephone`, `email_verifie`, `telephone_verifie`, `role`, `statut_kyc`, `date_creation`. **Seuls `nom` et `prenom` sont modifiables** (email et téléphone passent par l'OTP) |
| POST | `changement-contact/` | IsAuthenticated | `otp` | `nouvel_email` **ou** `nouveau_telephone` (un seul). Refus si la valeur appartient déjà à un autre compte. Envoie un OTP (§ 5) |
| POST | `verification-otp/` | IsAuthenticated | `otp` | `code`, `type_usage`. Vérifie le dernier OTP non utilisé de ce type et applique l'effet (§ 5) |
| POST | `mot-de-passe-oublie/` | AllowAny | `otp` | `email`. Réponse **identique** que le compte existe ou non : « Si ce compte existe, un code a été envoyé. » |
| POST | `mot-de-passe-oublie/confirmer/` | AllowAny | `otp` | `email`, `code`, `nouveau_password`. Toute erreur renvoie le même message « Code invalide ou expiré. » (pas d'énumération). Succès : mot de passe changé et **tous les refresh tokens révoqués** |
| POST | `upload-kyc/` | IsAuthenticated | `kyc` | Multipart : `type_piece`, `piece_identite_recto`, `piece_identite_verso` (selon le type), `selfie`, `numero_mobile_money`, `adresse`, `compte_bancaire?`. Soumet la demande vendeur (§ 7). Les fichiers sont en écriture seule : la réponse ne contient jamais leur URL |
| GET | `kyc/<utilisateur_id>/<champ>/` | IsAuthenticated : propriétaire, `admin` ou `super_admin` | `user` (global) | `champ` ∈ `piece_identite_recto`, `piece_identite_verso`, `selfie`. **Seul** point d'accès aux documents KYC (jamais l'URL `MEDIA_URL`). Non autorisé → **403 avant toute recherche** (on ne révèle pas qui a déposé un dossier) ; champ inconnu ou non fourni → 404 |

Erreurs : format commun `config/exceptions.py` (`success`, `status_code`, `detail`, `errors`) pour les erreurs levées par DRF ; les vues OTP et mot de passe oublié renvoient directement `{"message": …}`.

## 4. JWT

Réglages `SIMPLE_JWT` (`config/settings/base.py`) :

| Réglage | Valeur | Effet |
|---|---|---|
| `ACCESS_TOKEN_LIFETIME` | 15 minutes | |
| `REFRESH_TOKEN_LIFETIME` | 7 jours | |
| `ROTATE_REFRESH_TOKENS` + `BLACKLIST_AFTER_ROTATION` | oui | Chaque rafraîchissement émet un nouveau refresh token et met l'ancien en liste noire |
| `CHECK_REVOKE_TOKEN` | oui | Tout jeton émis avant un changement de mot de passe devient invalide (access compris) |

- Déconnexion : met le refresh token fourni en liste noire (`token_blacklist`).
- Mot de passe oublié confirmé : `services.revoquer_tokens_actifs()` met en liste noire **tous** les refresh tokens de l'utilisateur.
- Connexion Google : le jeton est émis par `RefreshToken.for_user()`, qui ne vérifie pas `is_active` ; le service le vérifie explicitement avant (§ 6).
- Purge : tâche `purger_tokens_expires` (tous les jours à 3 h 15) supprime les `OutstandingToken` expirés (et leurs entrées de liste noire).

## 5. OTP

- **Génération** : 6 chiffres tirés avec `secrets` (générateur cryptographique), stockés hachés (`make_password`). Validité : **10 minutes** (`CodeOTP.DUREE_VALIDITE_MINUTES`).
- **Vérification** (`CodeOTP.verifier`) : sous verrou `select_for_update` ; refus si déjà utilisé, expiré, ou **5 tentatives** atteintes (journalisé dans le logger `securite`). Chaque tentative est comptée ; un code juste est marqué `utilise`.
- Seul le **dernier** OTP non utilisé du type demandé est vérifié. Générer un nouveau code n'invalide pas les précédents ; ils expirent au bout de 10 minutes.
- **Canal d'envoi** (tâche Celery `envoyer_code_otp_email`) :

| `type_usage` | Envoyé à | Effet après vérification |
|---|---|---|
| `inscription` | l'email du compte créé | `email_verifie = true` |
| `changement_email` | la **nouvelle** adresse (preuve qu'on la possède) | `email` remplacé, `email_verifie = true` |
| `changement_telephone` | l'email **actuel** du compte (aucun fournisseur SMS branché, § 9) | `telephone` remplacé, `telephone_verifie = true` |
| `mdp_oublie` | l'email du compte | Géré par `mot-de-passe-oublie/confirmer/` |

- **Purge** : tâche `nettoyer_otp_expires` (tous les jours à 3 h 00) supprime les OTP expirés depuis plus de 24 heures.
- **Notification de connexion** (tâche `envoyer_notification_connexion`) : après chaque connexion réussie (mot de passe ou Google), email au titulaire avec l'adresse IP et le user-agent. Jamais sur un échec.

## 6. Connexion Google

`ConnexionGoogleView` vérifie l'`id_token` auprès de Google (`GOOGLE_OAUTH_CLIENT_ID`), puis délègue à `services.resoudre_utilisateur_google()` :

1. `sub`, `email` et `email_verified` obligatoires, sinon **400**.
2. Compte déjà lié à ce `google_id` → connexion.
3. Sinon, compte existant avec le même email :
   - désactivé → **403** « Ce compte a été désactivé. » (vérifié en premier) ;
   - **non vérifié et avec un mot de passe utilisable** → **409** : liaison refusée (protection contre la prise de contrôle d'un compte créé à l'avance par un tiers avec l'email de la victime) ;
   - sinon, lié à Google et marqué vérifié.
4. Aucun compte : création (email vérifié, mot de passe inutilisable).
5. Contrôle final : un compte désactivé ne reçoit jamais de jeton (**403**).

## 7. KYC

- **Dépôt** (`upload-kyc/`) : type de pièce obligatoire.
  - verso **obligatoire** : `cni`, `permis`, `carte_consulaire`, `carte_resident` ;
  - verso **refusé** : `passeport`, `attestation_identite`.
- Le dépôt **soumet la demande vendeur** : `statut_kyc` passe à `en_attente`.
- Un seul dossier par compte :
  - `en_attente` ou `valide` → 400 (« Une demande est déjà en attente de traitement. » / « Votre dossier KYC a déjà été validé. ») ;
  - **`refuse` → resoumission** : le dossier existant est remplacé (verso vidé s'il n'est pas redonné, commentaire et date de traitement effacés) ; les anciens fichiers ne sont supprimés du stockage **qu'après le commit** de la transaction.
- Concurrence : la ligne `Utilisateur` est verrouillée pendant la décision (deux dépôts simultanés → un seul dossier, pas d'erreur 500).
- `compte_bancaire` vide est enregistré `NULL`.
- **Décision** (validation, refus) : back-office du module vendeurs (`/api/vendeurs/administration/demandes/`), voir `MODULE_VENDEURS.md`.
- **Consultation des pièces** : uniquement via `kyc/<utilisateur_id>/<champ>/` (§ 3) ; l'admin Django affiche des liens vers cette vue, jamais l'URL brute.

## 8. Limites de débit

Taux (`DEFAULT_THROTTLE_RATES`, `config/settings/base.py`) : `login` 10/h, `otp` 5/h, `kyc` 5/h, `logout` 30/h, `anon` 50/h, `user` 300/h.

- Identifiant : l'id du compte si l'utilisateur est authentifié, sinon l'IP. L'IP est lue selon `REST_FRAMEWORK['NUM_PROXIES']` (0 en dev, 1 en prod derrière Nginx, qui réécrit `X-Forwarded-For`) : changer cet en-tête ne contourne pas les limites.
- Un scope est **un seul compteur partagé** entre toutes les vues qui l'utilisent : `otp` couvre `changement-contact/`, `verification-otp/`, `mot-de-passe-oublie/` et `mot-de-passe-oublie/confirmer/` ; `login` couvre `connexion/` et `connexion-google/`.
- `upload-kyc/` s'appuie sur les limites globales (`DEFAULT_THROTTLE_CLASSES` inclut `ScopedRateThrottle`) : `kyc` et `user` s'appliquent.

## 9. Dette connue

- **Notification de connexion à brancher sur `apps/core/reseau.py`** : `LoginThrottleView` et `ConnexionGoogleView` lisent `request.META['REMOTE_ADDR']` directement. En production, derrière Nginx, c'est l'IP du conteneur Nginx, pas celle de l'utilisateur : l'email « Nouvelle connexion » affiche donc une adresse inutile. Elles doivent utiliser `adresse_ip_client()` (`apps/core/reseau.py`), seule source de l'IP client du projet (même logique que les limites de débit).
- **`compte_bancaire` n'est pas chiffré** : `config/settings/base.py` indique que `DocumentKYC.compte_bancaire` est chiffré au repos via `apps.core.fields.EncryptedCharField` (et `prod.py` exige `FIELD_ENCRYPTION_KEY`), mais le champ est un `CharField` ordinaire depuis la migration `0001_initial` ; `EncryptedCharField` n'est utilisé nulle part. À corriger (migration de chiffrement des données existantes) ou à retirer du commentaire.
- **SMS** : aucun fournisseur branché ; le code de changement de téléphone part sur l'email actuel (`TODO` dans `DemandeChangementContactView`). `telephone_verifie` ne prouve donc pas la possession du numéro.
- **Durée de l'OTP dans l'email** : `envoyer_code_otp_email` lit `settings.__dict__.get('OTP_DUREE_VALIDITE_MINUTES', 10)`, un réglage qui n'existe pas (affiche toujours 10). Devrait lire `CodeOTP.DUREE_VALIDITE_MINUTES`.
- **Commentaires périmés** :
  - `services.revoquer_tokens_actifs` parle d'access tokens de 30 minutes (réglage actuel : 15) ;
  - `ProfilSerializer` cite `app/core/securite.py` du service FastAPI, qui n'existe pas (`backend-fastapi/app/core/` est vide) ;
  - `DemandeMotDePasseOublieView` garde un `TODO : envoyer le code par email` alors que l'envoi est fait.
- **À examiner lors de la refonte du module** (observations de lecture, non testées) :
  - connexion possible sans email vérifié (`email_verifie` n'est pas contrôlé à la connexion) : comportement à décider ;
  - l'inscription indique qu'un email ou un téléphone est déjà utilisé (énumération de comptes possible), avec la seule limite `anon` ;
  - le compteur `otp` partagé (5/h pour quatre endpoints) peut bloquer un utilisateur qui enchaîne changement de contact et vérification ;
  - aucune limite dédiée sur `connexion/rafraichir/` (seulement `anon`).
- `GOOGLE_OAUTH_CLIENT_ID` vaut `''` par défaut : la connexion Google échoue (400) tant qu'il n'est pas configuré.

## 10. Tests

⚠️ **Toujours lancer les tests sur PostgreSQL.** Sous SQLite, `ResoumissionKYCConcurrenceTestCase` est **sauté** (`select_for_update()` est sans effet).

```bash
cd backend-django
DJANGO_SETTINGS_MODULE=config.settings.ci DB_NAME=anitche_test DB_USER=postgres DB_PASSWORD=postgres DB_HOST=localhost \
  python manage.py test apps.utilisateurs -v 2
```

Vérifier dans la sortie `-v 2` que tout est `ok` et rien `skipped`.

`apps/utilisateurs/tests.py` — 74 tests : inscription et vérification de l'email, connexion (limite, notification), profil, `CodeOTP` (génération, expiration, tentatives), changement de contact, vérification OTP, mot de passe oublié (non-énumération, révocation des tokens), connexion Google (liaison, pré-hijacking, compte désactivé), révocation des tokens et déconnexion, admin Django, dépôt et resoumission KYC (dont concurrence), téléchargement des pièces KYC, manager.

Postman : `utilisateurs.postman_collection.json` (hors dépôt) couvre toutes les routes du module ; les autres collections n'utilisent que `connexion/` (et `inscription/`, `upload-kyc/`, `verification-otp/`, `connexion/rafraichir/` pour la mise en place d'un vendeur).
