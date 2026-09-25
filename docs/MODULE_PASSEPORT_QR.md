# Module passeport QR — contrat et règles

> Périmètre : backend Django, `backend-django/apps/passeport_qr/`.
> Document vivant : à corriger dès qu'une règle est arbitrée en réunion.

## 1. Ce sur quoi le module s'appuie

| Élément | Où | Rôle |
|---|---|---|
| `EstVendeurValide`, `EstAdministrateur`, `ROLES_ADMINISTRATION`, `BoutiqueNonSuspendue` | `apps/vendeurs/permissions.py` | Accès à l'espace vendeur (mêmes règles que `ma-boutique/`). `is_staff` ne donne **aucun** pouvoir métier |
| `Produit.objects.visibles_publiquement()` | `apps/catalogue/models.py` | **La** règle de visibilité du catalogue (produit actif, boutique publiable, au moins une variante active ; sinon la fiche répond 404). Décide si un article est encore « disponible à la vente » : aucune règle propre au passeport |
| `Boutique.est_publiable` | `apps/vendeurs/models.py` | Boutique ouverte, non suspendue, vendeur validé et actif |
| `adresse_ip_client()` | `apps/core/reseau.py` | IP du client derrière les proxys de confiance (même source que les limites de débit DRF) |
| `FRONTEND_BASE_URL` | `config/settings/base.py` | Base de l'URL encodée dans le QR |

Aucun autre module n'importe `passeport_qr`. Aucun consommateur n'existe encore dans le dépôt (le frontend n'appelle pas ces routes ; le routeur FastAPI `scan_qr` est vide).

## 2. Modèle

- `PasseportProduit` : **un passeport = un lot** d'un produit (et éventuellement d'une variante).
  - `produit` et `variante` sont en `on_delete=PROTECT` (migration 0006) : un produit ou une variante certifiés ne peuvent pas être supprimés, même depuis le Django admin. L'API du catalogue ne supprime jamais, elle désactive (voir `MODULE_CATALOGUE.md`).
  - `code_passeport` : `PAS-<année>-<8 hex majuscules>`, unique, généré à la création. En cas de collision, nouvel essai (5 au maximum) au lieu d'une erreur 500. Format inchangé.
  - **Un seul passeport par (produit, variante, numero_lot)** quand `numero_lot` est renseigné : contrainte `passeport_unique_par_lot` (index unique partiel, `NULLS NOT DISTINCT` : « sans variante » compte comme une valeur ; PostgreSQL ≥ 15). Sans numéro de lot, pas de contrainte. La contrainte couvre aussi les passeports révoqués : pour un lot révoqué, on réactive le passeport, on n'en recrée pas un.
  - `statut_certification` : `standard` (défaut), `label_local`, `certifie_authentique`. Voir § 5.
  - `url_verification_publique` : **propriété calculée**, plus stockée : `FRONTEND_BASE_URL + "/qr/verifier/<code>"`. Le frontend dessine le QR à partir de cette URL (le champ `qr_code_image` a été supprimé).
  - `est_actif = False` : **certificat révoqué**. L'API ne supprime jamais un passeport.
  - `desactive_par` : `vendeur` ou `administration` (vide si actif), qui a désactivé le passeport. Contrainte `passeport_desactivation_coherente` : actif ⇔ `desactive_par` vide. Ne change que par `desactiver()` / `reactiver()` (UPDATE conditionnels, sûrs en cas de requêtes simultanées). Voir § 5 bis.
  - `nb_scans`, `dernier_scan` : mis à jour à chaque vérification publique (incrément SQL `F()`, aucun scan perdu en cas de requêtes simultanées).
- `HistoriqueScanPasseport` : un enregistrement par vérification publique (y compris d'un passeport révoqué).
  - `adresse_ip` : **tronquée** (/24 en IPv4, /48 en IPv6), jamais l'adresse complète.
  - Compteur et historique sont écrits dans la même transaction.

## 3. Endpoints — `/api/passeports/`

### Public (`AllowAny`, limite dédiée `passeport_verification`)

| Méthode | URL | Description |
|---|---|---|
| GET | `verifier/<code>/` | Vérification d'un code (insensible à la casse). Journalise un scan. Réponses au § 4 |

### Espace vendeur (`EstVendeurValideOuAdministrateur` + `BoutiqueDuVendeurNonSuspendue`)

| Méthode | URL | Description |
|---|---|---|
| GET | `vendeur/` | Liste **paginée** `{count, next, previous, results}` (20 par page). Vendeur : les passeports de sa boutique ; administration : tous |
| POST | `vendeur/` | Création. Corps : `produit_id`, `variante_id?`, `numero_lot?`, `origine_geographique?`, `materiaux_utilises?`, `date_fabrication?`, `artisan_createur?`, `statut_certification?` (défaut `standard`). **201** |
| GET / PUT / PATCH | `vendeur/<uuid>/` | Détail et modification |
| DELETE | `vendeur/<uuid>/` | **Révocation** (`est_actif = false`, `desactive_par` = qui agit), **204**. Aucune suppression réelle ; l'historique des scans est conservé. Désactivation aussi possible par PATCH `est_actif: false` ; réactivation par PATCH `est_actif: true` (règles au § 5 bis) |

Accès :
- **403** : client, vendeur non validé (`statut_kyc` ≠ validé), lecture comprise, comme `ma-boutique/`.
- **403** sur toute écriture (POST, PUT, PATCH, DELETE) quand la boutique du vendeur est suspendue ; la lecture reste possible. L'administration n'est pas bloquée (elle doit pouvoir révoquer un passeport d'une boutique suspendue).
- **404** sur le passeport d'une autre boutique (lecture comme écriture).
- Administration = rôle `admin` ou `super_admin` : peut créer un passeport sur le produit de n'importe quelle boutique (le passeport est rattaché à la boutique du produit).

### Refus (400)
- Produit d'une autre boutique → `errors.produit_id` (vérifié **avant** l'état du produit : on ne renseigne pas un vendeur sur les produits des autres).
- Produit inactif → `errors.produit_id` ; variante inactive → `errors.variante_id`. En modification, seul un **nouveau** rattachement à un produit/une variante inactifs est refusé : un produit désactivé après coup n'empêche pas de corriger les autres champs.
- Lot déjà couvert par un passeport → `errors.numero_lot` (aussi en PATCH, et en cas de course entre deux requêtes : c'est la contrainte qui tranche, voir § 6).
- `certifie_authentique` demandé par un vendeur → `errors.statut_certification`.
- F-21 : réassignation (PATCH) vers le produit d'une autre boutique, ou vers une variante d'un autre produit.

### Représentation vendeur
`id`, `code_passeport`, `produit`, `produit_nom`, `variante`, `boutique`, `numero_lot`, `origine_geographique`, `materiaux_utilises`, `date_fabrication`, `artisan_createur`, `statut_certification`, `statut_certification_display`, `nb_scans`, `dernier_scan`, `url_verification_publique`, `est_actif`, `desactive_par` (lecture seule), `date_creation`.

## 4. Vérification publique

| Situation | Statut HTTP | Réponse |
|---|---|---|
| Passeport actif, article vendable | 200 | Certificat complet, `statut_passeport: "valide"`, `boutique_nom`, `produit_slug`, `disponible_a_la_vente: true`, `motif_indisponibilite: null` |
| Passeport actif, article **non vendable** = produit absent de `Produit.objects.visibles_publiquement()` (boutique suspendue ou fermée, vendeur non validé ou désactivé, produit inactif, **aucune variante active**, produit sans variante), ou variante certifiée inactive | 200 | Certificat complet (il atteste la fabrication), mais `boutique_nom: "Vendeur indisponible"`, `produit_slug: null`, `disponible_a_la_vente: false`, `motif_indisponibilite: "Ce produit n'est plus proposé à la vente sur ANITCHE."` |
| Passeport révoqué (`est_actif = false`) | 200 | Uniquement `code_passeport`, `statut_passeport: "revoque"`, `statut_passeport_display: "Certificat révoqué"`, `disponible_a_la_vente: false` |
| Code inconnu | 404 | `Passeport numérique introuvable pour le code '…'.` |

- Le motif est **volontairement générique** : la réponse publique ne contient jamais le mot « suspendue » ni le nom d'une boutique non publiable.
- Champs du certificat : `code_passeport`, `statut_passeport`, `statut_passeport_display`, `produit_nom`, `produit_slug`, `boutique_nom`, `variante_nom`, `numero_lot`, `origine_geographique`, `materiaux_utilises`, `date_fabrication`, `artisan_createur`, `statut_certification`, `statut_certification_display`, `nb_scans`, `url_verification_publique`, `disponible_a_la_vente`, `motif_indisponibilite`.
- `dernier_scan` n'est **pas** public (il révélait quand quelqu'un d'autre avait scanné). `nb_scans` reste public : un nombre de scans anormal signale un QR recopié.
- Règle unique : `est_disponible_a_la_vente` ⇔ la fiche catalogue du produit répond 200 (et la variante certifiée, s'il y en a une, est active). La vue calcule la visibilité en SQL (`Exists` sur `Produit.objects.visibles_publiquement()`, dans la même requête que la lecture du passeport). Testé état par état contre la fiche catalogue (`test_meme_regle_que_la_fiche_catalogue`).
- Coût : 1 lecture (`select_related` + annotation de visibilité), puis UPDATE + INSERT du scan en transaction et relecture du compteur.

## 5. Certification « Certifié Authentique ANITCHE »

Ce label engage la plateforme : **seule l'administration l'attribue**, à la création comme en PATCH/PUT. Un vendeur choisit `standard` (défaut) ou `label_local`. Un vendeur peut modifier un passeport qui a déjà le label sans le perdre (renvoyer la même valeur est accepté) et peut le rétrograder, mais pas le réattribuer.

## 5 bis. Désactivation et réactivation

| Qui a désactivé | Le vendeur peut réactiver ? | L'administration peut réactiver ? |
|---|---|---|
| Le vendeur | Oui (PATCH `est_actif: true` → 200) | Oui |
| L'administration | **Non : 403** « Ce passeport a été révoqué par l'administration ANITCHE : seule l'administration peut le réactiver. » | Oui |

- Une révocation de l'administration **s'impose** à une désactivation du vendeur (elle en prend l'origine). À l'inverse, un vendeur qui « redésactive » (DELETE ou PATCH `est_actif: false`) un passeport révoqué par l'administration ne change rien (204/200 sans effet) : il ne peut pas reprendre la main.
- Réactivation refusée = **tout ou rien** : aucun autre champ de la même requête n'est appliqué.
- Une modification d'autres champs n'écrit que ces champs (`update_fields`) : un vendeur qui avait chargé le passeport avant une révocation simultanée ne l'annule pas en sauvegardant.
- Un vendeur peut toujours modifier les autres champs d'un passeport révoqué ; il reste révoqué.
- Django admin : (dés)activer un passeport compte comme une décision de l'administration.
- Public : un passeport révoqué affiche « Certificat révoqué », **sans** dire qui l'a révoqué.

## 6. Concurrence

- **Scans simultanés** : incrément SQL `F("nb_scans") + 1` dans la même transaction que l'historique. Testé : 80 scans parallèles donnent `nb_scans = 80` (auparavant ~25).
- **Création simultanée du même lot** : la vérification du serializer donne un message clair ; si deux requêtes passent la vérification en même temps, la contrainte `passeport_unique_par_lot` refuse la seconde, convertie en 400 `errors.numero_lot` (reconnue par le nom de contrainte, jamais une 500). Testé avec 6 requêtes parallèles : une seule 201.

## 7. Adresse IP et limite de débit

Chaîne de production déclarée (commentaires F-18 de `infra/nginx/nginx.conf`) : **Cloudflare → Nginx → Gunicorn/Django**. Django n'est joignable que via Nginx (aucun port publié).

- **Nginx** : le module `real_ip` remplace `$remote_addr` par `CF-Connecting-IP` **uniquement** pour les connexions venant des plages Cloudflare (`set_real_ip_from`). Un client qui contourne Cloudflare ne peut donc pas forger cet en-tête ; si Cloudflare est retiré, le bloc devient sans effet. Chaque `location` **écrase** `X-Forwarded-For` avec `$remote_addr`.
- **Django** : `REST_FRAMEWORK['NUM_PROXIES']` = `0` par défaut (dev, tests : seul `REMOTE_ADDR` compte), `1` en production (`prod.py`).
- `adresse_ip_client()` lit l'IP par la même logique que les limites de débit DRF, et renvoie `None` pour une valeur invalide.
- Limite `passeport_verification` : **600/heure par IP** (anonymes) ou par compte. Plus large que `anon` (50/h, partagée avec toute l'API) à cause du CGNAT des opérateurs mobiles, où beaucoup de clients partagent une même IP publique. Elle ne se contourne pas en changeant `X-Forwarded-For`.

Avant ce correctif, `X-Forwarded-For` était lu tel quel : IP falsifiable dans l'historique, **erreur 500** dès que l'en-tête contenait plusieurs adresses (forme produite par Nginx derrière Cloudflare), et contournement de **toutes** les limites de débit anonymes de l'API (login, otp, logout…) en changeant l'en-tête à chaque requête.

## 8. Changements de contrat (refactorisation de septembre 2026)

| Avant | Après |
|---|---|
| Espace vendeur ouvert à tout compte authentifié (client : 200 liste vide / 400) | 403 pour client et vendeur non validé ; 403 en écriture si boutique suspendue |
| Liste vendeur : tableau JSON | `{count, next, previous, results}` |
| `statut_certification` par défaut `certifie_authentique`, au choix du vendeur | Défaut `standard` ; `certifie_authentique` réservé à l'administration |
| DELETE = suppression réelle (historique compris) | DELETE = révocation, 204 |
| Réactivation libre (PATCH `est_actif`) | Une révocation de l'administration n'est levée que par elle (vendeur : 403) ; nouveau champ `desactive_par` (lecture seule) |
| Passeports existants en `certifie_authentique` | Repassés en `standard` (migration 0005) |
| Passeport désactivé : 404 comme un faux | 200 `statut_passeport: "revoque"` |
| Vérification publique indifférente à l'état de la boutique/du produit | Boutique masquée, `produit_slug: null`, `disponible_a_la_vente`, `motif_indisponibilite` ; ajout de `statut_passeport(_display)` |
| `dernier_scan` public | Retiré de la réponse publique (toujours visible côté vendeur) |
| `url_verification_publique` stockée, codée en dur `https://anitche.ci/qr/verifier/…` | Calculée depuis `FRONTEND_BASE_URL` |
| Champ `qr_code_image` (jamais rempli) | Supprimé |
| Plusieurs passeports possibles pour un même lot | 400 `errors.numero_lot` |
| Passeport accepté sur produit/variante inactifs | 400 |
| Limite `anon` globale (50/h) | Limite dédiée `passeport_verification` (600/h) |

## 9. Décisions en attente

- **Domaine et route de la page de vérification** : `FRONTEND_BASE_URL` (prod : `https://anitche.com` par défaut dans `docker-compose.prod.yml`, `http://localhost:5173` en dev) et `CHEMIN_VERIFICATION_PUBLIQUE = "/qr/verifier/{code}"` (`apps/passeport_qr/models.py`). Le dépôt mentionne à la fois `anitche.com` (Nginx, `.env.example`) et `anitche.ci` (ancienne URL, messages de `prod.py`). **À confirmer par l'équipe avant d'imprimer le moindre QR** : l'URL est figée dès l'impression.

## 10. Dette connue

- **Niveau 2 — durée de conservation de l'historique des scans** : aucune purge. L'IP est tronquée, mais IP réseau + user-agent + date restent des données de connexion. Prévoir une tâche Celery de purge (durée à fixer, conformité loi ivoirienne n° 2013-450 sur les données personnelles) et, à forte volumétrie, un partitionnement par date (voir `ARCHITECTURE_HAUTE_ECHELLE_100K.md`).
- **Plages Cloudflare** dans `nginx.conf` : relevées le 2026-09-25, à revérifier périodiquement (https://www.cloudflare.com/ips/). Une plage manquante ne crée pas de faille, mais regroupe des visiteurs sous une IP Cloudflare dans les limites de débit.
- Cloudflare n'est matérialisé dans le dépôt que par des commentaires (F-18) ; rien n'empêche d'atteindre l'origine sans passer par lui. Le réglage Nginx reste sûr dans les deux cas. Restreindre l'origine aux IP Cloudflare (pare-feu hôte) relève de l'infrastructure.
- Le point d'entrée public reste soumis à l'authentification JWT par défaut : un jeton expiré envoyé par un client connecté donne 401 au lieu du certificat.
- **À faire lors de la reprise du module `utilisateurs`** : la notification de connexion (`apps/utilisateurs/views.py`, envoi de `envoyer_notification_connexion`) lit encore `request.META['REMOTE_ADDR']` directement. Derrière Nginx, c'est l'IP du conteneur Nginx, pas celle de l'utilisateur. Elle doit passer par `adresse_ip_client()` (`apps/core/reseau.py`), seule source de l'IP client du projet.
- Le routeur FastAPI `scan_qr` est vide, contrairement à ce qu'annonce `ANALYSE_BACKEND.md`.

## 11. Tests

⚠️ **Toujours lancer les tests sur PostgreSQL.** Sous SQLite, `PasseportConcurrenceTestCase` et `test_contrainte_en_base_sans_variante` sont **sautés** (`NULLS NOT DISTINCT` et concurrence réelle n'y existent pas).

```bash
cd backend-django
DJANGO_SETTINGS_MODULE=config.settings.ci DB_NAME=anitche_test DB_USER=postgres DB_PASSWORD=postgres DB_HOST=localhost \
  python manage.py test apps.passeport_qr apps.core -v 2
```

Vérifier dans la sortie `-v 2` que `test_scans_simultanes_aucun_increment_perdu` et `test_creations_simultanees_du_meme_lot_une_seule_acceptee` affichent `ok` et non `skipped`.

Couverture : accès (client, anonyme, `is_staff`, admin/super_admin, vendeur non validé, boutique suspendue, passeports d'autrui), certification, révocation, désactivation/réactivation selon l'origine (§ 5 bis, y compris modification concurrente et contrainte en base), migration 0005, unicité par lot (API, base, concurrence), produit/variante inactifs, vérification publique (6 cas non vendables, produit sans variante active ou sans variante, équivalence avec la fiche catalogue, révoqué, 404, champs exposés, URL calculée, nombre de requêtes), IP (en-tête forgé, chaîne multi-adresses, proxy de confiance, troncature), atomicité du scan, limite de débit dédiée, collision de code, F-21.

## 12. Migration `0003_passeport_par_lot_sans_image_ni_url`

1. **Vérifie l'absence de doublons** (même produit, variante et lot). S'il y en a, elle **s'arrête et liste les codes** ; elle ne fusionne ni ne supprime rien. Choisir le passeport à conserver (celui déjà imprimé), corriger, relancer `migrate`.
2. Tronque les IP déjà journalisées (/24, /48).
3. Supprime `qr_code_image` et `url_verification_publique`, passe le défaut de `statut_certification` à `standard`, ajoute la contrainte `passeport_unique_par_lot`.

Base de dev vérifiée avant migration : 0 passeport, 0 doublon, 0 image, 0 IP. Migration appliquée en dev.

## 13. Migrations `0004_desactive_par` et `0005_certifications_vendeur_en_standard`

- **0004** : ajoute `desactive_par`. Les passeports **déjà inactifs** reçoivent `administration` : leur origine est inconnue, et l'attribuer au vendeur lui rendrait la main sur une éventuelle révocation pour fraude (un vendeur concerné demande la réactivation). Puis ajoute la contrainte `passeport_desactivation_coherente`.
- **0005** : repasse tous les `certifie_authentique` existants en `standard`. Dans l'ancien code, c'était le défaut choisi par le vendeur ; aucun n'a été attribué par l'administration, qui réattribue le label après vérification. **Irréversible** (on ne sait plus lesquels étaient certifiés).

Base de dev vérifiée avant application : 0 passeport (donc 0 `certifie_authentique`, 0 inactif). Migrations appliquées en dev.

## 14. Collection Postman

`postman_passeport_qr.json` (hors dépôt, à côté des autres collections), même organisation que celle du panier : 1. Mise en place → 2. Parcours vendeur → 3. Vérification publique → 4. Scénarios sécurité → 5. Nettoyage. Rejouable : chaque création utilise un numéro de lot unique ; le nettoyage désactive tous les passeports créés par l'exécution, y compris ceux qu'une régression aurait laissé créer, et remet la boutique en état. Prérequis : comptes de la collection vendeurs, `admin_password` à renseigner.
