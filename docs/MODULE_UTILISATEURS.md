# Module utilisateurs — contrat et règles

> Périmètre : backend Django, `backend-django/apps/utilisateurs/`.
> Document vivant : à corriger dès qu'une règle est arbitrée en réunion.
>
> Passe 2 (septembre 2026) : diagnostic confirmé par tests, puis refonte (chiffrement des données financières du KYC, email vérifié obligatoire pour les actions sensibles, limites de débit séparées, IP réelle dans la notification de connexion). Les changements visibles par le frontend sont regroupés au § 11.

## 1. Rôle du module et dépendances

Comptes, authentification (JWT, Google), codes OTP et dossier KYC. C'est le socle des autres modules :

| Élément | Utilisé par | Pour |
|---|---|---|
| `Role` (`client`, `vendeur`, `livreur`, `moderateur`, `support`, `admin`, `super_admin`) | tous | Les permissions métier s'appuient sur le rôle. `ROLES_ADMINISTRATION = (admin, super_admin)` est défini dans `apps/vendeurs/permissions.py` |
| `StatutKYC` (`non_soumis`, `en_attente`, `valide`, `refuse`) | vendeurs, catalogue, passeport_qr, panier | **Source de vérité** de l'état vendeur (`EstVendeurValide`, `Boutique.est_publiable`) |
| `is_active` | vendeurs, catalogue | Un compte désactivé ne publie rien (`est_publiable`) et ne reçoit pas de jeton |
| `EmailVerifie` (`apps/utilisateurs/permissions.py`) | utilisateurs, commandes | Actions sensibles réservées aux emails vérifiés (§ 6) |
| `DocumentKYC` | vendeurs | Instruction des demandes vendeur (validation / refus : `MODULE_VENDEURS.md`) |

Le module dépend de `apps/core` (validateurs de fichiers, `CheminUploadUUID`, `EncryptedCharField`, `adresse_ip_client`) et de `rest_framework_simplejwt` (+ `token_blacklist`), `google-auth`, `cryptography` (Fernet), Celery (envoi des emails, tâches planifiées).

`is_staff` ne sert qu'au Django admin : aucun module ne l'utilise comme pouvoir métier.

## 2. Modèle

- **`Utilisateur`** (`AbstractBaseUser` + `PermissionsMixin`) : identifiant de connexion = `email` (unique). `telephone` unique (facultatif, ajouté après l'inscription), `nom`, `prenom`, `role` (défaut `client`), `statut_kyc` (défaut `non_soumis`), `email_verifie`, `telephone_verifie` (voir § 10), `google_id` (unique), `is_active`, `is_staff`.
  - `soumettre_demande_vendeur()` : passe `statut_kyc` à `en_attente` ; lève `StatutsKYCImpossibles` si une demande est déjà en attente ou validée.
- **`DocumentKYC`** (un par utilisateur) : `type_piece`, `piece_identite_recto`, `piece_identite_verso`, `selfie`, **`numero_mobile_money` (chiffré)**, `adresse`, **`compte_bancaire` (chiffré, facultatif)**, `date_soumission`, `date_traitement`, `commentaire_admin`.
  - Fichiers stockés sous un nom UUID. Pièce d'identité : JPEG, PNG, PDF, **10 Mo**, signature binaire vérifiée. Selfie : JPEG, PNG, WebP, **5 Mo**.
  - Les deux champs financiers sont des `EncryptedCharField` (§ 4) : jamais en clair en base ni dans ses sauvegardes, relus en clair par l'application. Longueurs métier validées par l'API : 20 caractères (mobile money), 50 (compte bancaire).
- **`CodeOTP`** : `code_hash` (jamais en clair), `type_usage` (`inscription`, `mdp_oublie`, `changement_email`, `changement_telephone`), `nouvelle_valeur`, `date_expiration`, `nombre_tentatives`, `utilise`.

## 3. Endpoints — `/api/utilisateurs/`

| Méthode | URL | Accès | Limite | Description |
|---|---|---|---|---|
| POST | `inscription/` | AllowAny | `inscription` | `email`, `password`, `nom`, `prenom`. **`telephone` refusé (400)**. Compte créé avec `email_verifie = false` ; OTP `inscription` envoyé à l'adresse. **201** |
| POST | `connexion/` | AllowAny | `login` | `email`, `password` → `{access, refresh}`. Possible **même sans email vérifié**. Notification email au titulaire à chaque connexion réussie (§ 5) |
| POST | `connexion/rafraichir/` | AllowAny | `rafraichissement` | `refresh` → nouveaux `access` et `refresh` (rotation, § 4) |
| POST | `connexion-google/` | AllowAny | `login` | `id_token` Google → `{access, refresh}` (§ 7) |
| POST | `deconnexion/` | AllowAny, sans authentification | `logout` | `refresh` → liste noire. **205** |
| POST | `renvoyer-code-inscription/` | IsAuthenticated | `otp_envoi` | **Nouveau.** Envoie un nouveau code de vérification de l'email. 400 si l'email est déjà vérifié. **200** |
| POST | `verification-otp/` | IsAuthenticated | `otp_verification` | `code`, `type_usage`. Vérifie le dernier OTP non utilisé de ce type et applique l'effet (§ 5) |
| GET / PUT / PATCH | `profil/` | IsAuthenticated | `user` | Seuls `nom` et `prenom` sont modifiables |
| POST | `changement-contact/` | IsAuthenticated + **email vérifié** | `otp_envoi` | `nouvel_email` **ou** `nouveau_telephone`. Refus si la valeur appartient déjà à un autre compte |
| POST | `mot-de-passe-oublie/` | AllowAny | `otp_envoi` | `email`. Réponse identique que le compte existe ou non |
| POST | `mot-de-passe-oublie/confirmer/` | AllowAny | `otp_verification` | `email`, `code`, `nouveau_password`. Erreur unique « Code invalide ou expiré. » ; succès : mot de passe changé, **tous les refresh tokens révoqués** |
| POST | `upload-kyc/` | IsAuthenticated + **email vérifié** | `kyc` | Dépôt du dossier = demande vendeur (§ 8). Fichiers en écriture seule |
| GET | `kyc/<utilisateur_id>/<champ>/` | propriétaire, `admin`, `super_admin` | `user` | Seul accès aux pièces. Non autorisé → 403 avant toute recherche ; champ inconnu ou non fourni → 404 ; **fichier référencé mais absent du stockage → 404** « Ce document n'est plus disponible. » (journalisé) |

Également protégé par `EmailVerifie` hors de ce module : `POST /api/commandes/valider-panier/`.

Format d'erreur commun (`config/exceptions.py`) : `success`, `status_code`, `detail`, `errors` ; les vues OTP et mot de passe oublié renvoient directement `{"message": …}`.

## 4. JWT et chiffrement

| Réglage `SIMPLE_JWT` | Valeur | Effet |
|---|---|---|
| `ACCESS_TOKEN_LIFETIME` | 15 minutes | |
| `REFRESH_TOKEN_LIFETIME` | 7 jours | |
| `ROTATE_REFRESH_TOKENS` + `BLACKLIST_AFTER_ROTATION` | oui | Chaque rafraîchissement émet un nouveau refresh et met l'ancien en liste noire |
| `CHECK_REVOKE_TOKEN` | oui | Tout jeton émis avant un changement de mot de passe devient invalide (access compris) |

Déconnexion : le refresh fourni va en liste noire. Mot de passe oublié confirmé : tous les refresh tokens de l'utilisateur. Purge quotidienne des jetons expirés (`purger_tokens_expires`, 3 h 15).

**Chiffrement au repos** (`apps/core/fields.py`) : Fernet (AES-128-CBC + HMAC-SHA256, paquet `cryptography`) via `MultiFernet`. La **première** clé de `FIELD_ENCRYPTION_KEYS` chiffre, toutes déchiffrent. Chiffrement non déterministe : ces colonnes ne peuvent être ni filtrées ni indexées. Une valeur qu'aucune clé ne déchiffre est lue comme « [valeur illisible — clé de chiffrement invalide ou modifiée] » (et journalisée) ; **une sauvegarde réécrit alors le jeton chiffré d'origine tel quel** : une clé erronée ne détruit jamais la donnée.

## 4 bis. Gestion de la clé

**Où la garder.** Les clés de `FIELD_ENCRYPTION_KEYS` vivent dans un gestionnaire de secrets (ou un coffre équivalent), injectées en variable d'environnement (`infra/.env` → `docker-compose.prod.yml`, services Django, celery-worker, celery-beat). **Jamais** :
- dans la base, ni au même endroit que la base ;
- dans les sauvegardes produites par `infra/scripts/backup_db.sh`, ni sur le même support ou le même compte de stockage que ces sauvegardes ;
- dans le dépôt Git (seule la clé de **dev**, publique, y figure ; `prod.py` refuse de démarrer avec elle, sans clé, ou avec une clé mal formée ; `check_prod_env.sh` exige la variable).

Sinon, une seule fuite (serveur de sauvegarde, dump) livre à la fois les données et la clé : le chiffrement ne protège plus rien.

**Générer une clé.**
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Rotation, pas à pas** (clé compromise, départ d'une personne qui y avait accès, ou rotation périodique) :
1. Générer la nouvelle clé et l'enregistrer dans le gestionnaire de secrets **à côté** de l'ancienne (ne rien supprimer).
2. Mettre `FIELD_ENCRYPTION_KEYS=NOUVELLE,ANCIENNE` (la nouvelle **en premier**) et redéployer Django **et** Celery. Dès cet instant, tout ce qui est écrit l'est avec la nouvelle clé, et l'ancien reste lisible.
3. Contrôler : `python manage.py rechiffrer_donnees_sensibles --simulation` (compte sans rien écrire).
4. Re-chiffrer : `python manage.py rechiffrer_donnees_sensibles`. Le rapport doit indiquer **0 illisible**.
5. Mettre `FIELD_ENCRYPTION_KEYS=NOUVELLE` et redéployer.
6. Vérifier qu'un dossier KYC s'affiche correctement, puis révoquer l'ancienne clé dans le gestionnaire de secrets. Les **anciennes sauvegardes** de la base restent chiffrées avec l'ancienne clé : la conserver (archivée, hors ligne) aussi longtemps que ces sauvegardes, ou accepter qu'elles deviennent illisibles.

Ne jamais passer à l'étape 5 tant que l'étape 4 signale des valeurs illisibles.

**Perte de la clé.** Les valeurs chiffrées avec elle sont **définitivement irrécupérables** (c'est le principe même du chiffrement) : aucune procédure ne les retrouve. L'application reste fonctionnelle (champ affiché « illisible », rien n'est écrasé) ; les vendeurs concernés doivent ressaisir leur numéro mobile money et leur compte bancaire (resoumission du dossier KYC). D'où l'obligation d'une copie de secours de la clé, elle aussi hors de la base et de ses sauvegardes.

**Migration `utilisateurs 0008`** : colonnes passées en texte, valeurs existantes chiffrées avec la clé active, puis passage au champ chiffré ; le retour arrière déchiffre. À exécuter avec la **même** clé que celle du serveur qui relira les données.

## 5. OTP et notifications

- **Génération** : 6 chiffres (`secrets`), stockés hachés. Validité : 10 minutes (`CodeOTP.DUREE_VALIDITE_MINUTES`) ; l'email annonce cette durée réelle.
- **Vérification** : sous verrou ; refus si déjà utilisé, expiré, ou **5 tentatives** atteintes. Seul le **dernier** OTP non utilisé du type demandé est vérifié.

| `type_usage` | Envoyé à | Effet après vérification |
|---|---|---|
| `inscription` | l'email du compte (à l'inscription, ou via `renvoyer-code-inscription/`) | `email_verifie = true` |
| `changement_email` | la **nouvelle** adresse | `email` remplacé, `email_verifie = true` |
| `changement_telephone` | l'email **actuel** du compte (aucun fournisseur SMS) | `telephone` remplacé, **`telephone_verifie = false`** (§ 10) |
| `mdp_oublie` | l'email du compte | Géré par `mot-de-passe-oublie/confirmer/` |

- **Purge** : `nettoyer_otp_expires` (3 h 00) supprime les OTP expirés depuis plus de 24 heures.
- **Notification de connexion** : après chaque connexion réussie (mot de passe ou Google), email au titulaire avec l'**IP réelle** du client (`adresse_ip_client()`, `apps/core/reseau.py`, qui ne lit `X-Forwarded-For` que derrière le proxy de confiance) et le user-agent. Jamais sur un échec.

## 6. Email vérifié

La connexion reste possible sans email vérifié (navigation, panier). Sont refusées tant que l'email n'est pas vérifié, avec **403** et `errors.code = "email_non_verifie"` :
- `POST upload-kyc/` (dépôt KYC = demande vendeur) ;
- `POST changement-contact/` ;
- `POST /api/commandes/valider-panier/`.

Déblocage : saisir le code d'inscription dans `verification-otp/` (`type_usage = inscription`) ; code expiré ou perdu → `renvoyer-code-inscription/`.

Adresse mal saisie à l'inscription : la personne ne reçoit aucun code et ne peut pas corriger son email depuis ce compte (le changement de contact exige un email vérifié). Ce n'est pas un blocage : elle se réinscrit avec la bonne adresse (le téléphone n'étant plus demandé à l'inscription, aucune donnée unique ne l'en empêche). Le compte erroné reste non vérifié ; sa suppression relève de la dette niveau 2 du § 12.

## 7. Connexion Google

`ConnexionGoogleView` vérifie l'`id_token` auprès de Google (`GOOGLE_OAUTH_CLIENT_ID` ; vide = tout jeton refusé, 400), puis `services.resoudre_utilisateur_google()` :
1. `sub`, `email`, `email_verified` obligatoires, sinon **400** ;
2. compte déjà lié à ce `google_id` → connexion ;
3. compte existant avec le même email : désactivé → **403** ; non vérifié avec mot de passe utilisable → **409** (anti-prise de contrôle) ; sinon lié à Google et marqué vérifié ;
4. aucun compte : création (email vérifié, mot de passe inutilisable) ;
5. un compte désactivé ne reçoit jamais de jeton (**403**).

## 8. KYC

- Verso **obligatoire** : `cni`, `permis`, `carte_consulaire`, `carte_resident` ; **refusé** : `passeport`, `attestation_identite`.
- Le dépôt soumet la demande vendeur (`statut_kyc = en_attente`). `en_attente` ou `valide` → 400 ; **`refuse` → resoumission** (dossier remplacé, anciens fichiers supprimés après le commit).
- Concurrence : ligne `Utilisateur` verrouillée pendant la décision.
- Décision (validation, refus) : back-office vendeurs (`MODULE_VENDEURS.md`).
- **`compte_bancaire`** : conservé, facultatif et chiffré. **Minimisation à trancher avec le module paiements**, selon le canal de reversement aux vendeurs : si seul le mobile money est utilisé, le champ ne devrait plus être collecté.

## 9. Limites de débit

| Scope | Taux | Endpoints | Clé |
|---|---|---|---|
| `inscription` | 10/h | `inscription/` | IP |
| `login` | 10/h | `connexion/`, `connexion-google/` | IP |
| `rafraichissement` | 300/h | `connexion/rafraichir/` | IP |
| `otp_envoi` | 5/h | `changement-contact/`, `mot-de-passe-oublie/`, `renvoyer-code-inscription/` | compte, ou IP si anonyme |
| `otp_verification` | 10/h | `verification-otp/`, `mot-de-passe-oublie/confirmer/` | compte, ou IP si anonyme |
| `kyc` | 5/h | `upload-kyc/` | compte |
| `logout` | 30/h | `deconnexion/` | IP |
| `user` | 300/h | autres routes authentifiées | compte |

- Un scope = un compteur partagé par ses endpoints. Envoi et vérification des codes sont désormais comptés à part : demander plusieurs codes ne bloque plus la vérification. Chaque code reste limité à 5 essais.
- `rafraichissement` : avec un access token de 15 minutes, une session active rafraîchit ~4 fois par heure ; derrière le CGNAT des opérateurs mobiles, de nombreux utilisateurs partagent une IP (l'ancien partage du taux `anon` de 50/h était épuisé par une dizaine de sessions). Aucun risque de force brute (refresh signé).
- IP selon `REST_FRAMEWORK['NUM_PROXIES']` : changer `X-Forwarded-For` ne contourne aucune limite.

## 10. Décisions d'équipe

- **`telephone_verifie`** (septembre 2026) : ne passe **plus** à `true` tant que le code de changement de téléphone n'est pas envoyé par SMS au numéro lui-même. Aujourd'hui le code part sur l'email du compte : il prouve que le titulaire demande le changement, pas qu'il possède le numéro. À revoir quand un fournisseur SMS sera branché. Les valeurs `true` déjà en base (obtenues par l'ancien flux email) n'ont pas été modifiées.
- **Énumération à l'inscription** : le message « Un compte existe déjà avec cet email. » est **conservé** (choix d'expérience utilisateur), borné par la limite `inscription` (10/h par IP). Le téléphone n'est plus demandé à l'inscription, ce qui supprime l'énumération par numéro.
- **Minimisation de `compte_bancaire`** : voir § 8.

## 11. Impact frontend

Changements de contrat à intégrer (tous testés côté backend) :

1. **Inscription sans téléphone.** `POST /api/utilisateurs/inscription/` accepte `email`, `password`, `nom`, `prenom`. Envoyer `telephone` renvoie **400** (`errors.telephone`). Retirer le champ du formulaire d'inscription ; proposer l'ajout du numéro ensuite, depuis le compte (`POST /api/utilisateurs/changement-contact/` avec `nouveau_telephone`, puis `verification-otp/` avec `type_usage = changement_telephone`).
2. **Email vérifié obligatoire pour les actions sensibles.** Réponse **403** avec `errors.code = "email_non_verifie"` (et un message dans `detail`) sur :
   - `POST /api/utilisateurs/upload-kyc/` (devenir vendeur) ;
   - `POST /api/utilisateurs/changement-contact/` ;
   - `POST /api/commandes/valider-panier/` (commander).

   À ce code, afficher l'écran de vérification de l'email (saisie du code) plutôt qu'une erreur générique. Se fier à `errors.code`, pas au texte. La connexion, la navigation et le panier restent accessibles sans vérification.
3. **Nouvel endpoint de renvoi du code.** `POST /api/utilisateurs/renvoyer-code-inscription/` (connecté, sans corps) : **200** `{"message": "Code envoyé."}` ; **400** si l'email est déjà vérifié ; **429** au-delà de 5 envois de code par heure (compteur partagé avec les autres envois). Le code se saisit dans `POST /api/utilisateurs/verification-otp/` avec `{"code": "…", "type_usage": "inscription"}`.
4. **`telephone_verifie` reste `false`** après un changement de téléphone (le code part par email) : ne pas afficher de badge « téléphone vérifié » sur la base de ce champ pour l'instant.
5. **Limites de débit** (réponses **429**) : inscription 10/h par IP ; rafraîchissement du jeton 300/h par IP ; envois de code 5/h ; vérifications de code 10/h.
6. **Téléchargement d'une pièce KYC perdue** : **404** « Ce document n'est plus disponible. » au lieu d'une erreur 500.

Aucun autre champ de réponse ne change (le profil, les jetons et le dépôt KYC gardent leur format ; `numero_mobile_money` et `compte_bancaire` sont renvoyés en clair au titulaire comme avant).

## 12. Dette connue

- **Fournisseur SMS** absent : conditionne `telephone_verifie` (§ 10).
- `GOOGLE_OAUTH_CLIENT_ID` vide par défaut : la connexion Google refuse tout jeton tant qu'il n'est pas configuré.
- **Niveau 2 — comptes jamais vérifiés** : aucune purge. Une tâche Celery planifiée (même modèle que `nettoyer_otp_expires`) devra supprimer les comptes dont l'email n'a jamais été vérifié après N jours (N à fixer en équipe), pour éviter les comptes fantômes (adresses mal saisies, inscriptions abandonnées, comptes créés avec l'email d'un tiers). À cadrer avant de l'écrire : exclure tout compte lié à des données (commande, dossier KYC, boutique, ticket) et prévenir le titulaire avant suppression.

## 13. Tests

⚠️ **Toujours lancer les tests sur PostgreSQL.** Sous SQLite, `ResoumissionKYCConcurrenceTestCase` est **sauté** (`select_for_update()` sans effet). Le paquet `cryptography` doit être installé (`pip install -r requirements.txt`).

```bash
cd backend-django
DJANGO_SETTINGS_MODULE=config.settings.ci DB_NAME=anitche_test DB_USER=postgres DB_PASSWORD=postgres DB_HOST=localhost \
  python manage.py test apps.utilisateurs -v 2
```

Vérifier dans la sortie `-v 2` que tout est `ok` et rien `skipped`. Les tests écrivent leurs fichiers dans un `MEDIA_ROOT` temporaire (`config/settings/test.py`), jamais dans `media/`.

`apps/utilisateurs/tests.py` — 97 tests. Passe 2 : chiffrement en base et relecture, longueurs métier, valeur illisible jamais écrasée, rotation de clé et commande de re-chiffrement (dont simulation et valeurs illisibles), migration 0008 aller-retour, IP de la notification (mot de passe, Google, sans proxy), durée affichée dans l'email OTP, pièce KYC perdue (404), email vérifié (connexion libre, 3 actions refusées avec le code machine, renvoi du code puis vérification), inscription sans téléphone et limite par IP, limites OTP séparées, limite de rafraîchissement dédiée, `telephone_verifie` non validé sans SMS, isolation de `MEDIA_ROOT`.

Postman : `utilisateurs.postman_collection.json` (hors dépôt) ; scénarios automatiques 5 (email non vérifié) et 6 (téléphone refusé à l'inscription), les autres demandent de saisir les codes OTP à la main.
