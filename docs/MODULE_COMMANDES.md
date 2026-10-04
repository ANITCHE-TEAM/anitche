# Module commandes — contrat et règles

> Périmètre : backend Django, `backend-django/apps/commandes/` (et ses points de contact dans `paiements`, `livraison`, `fidelite`).
> Document vivant : à corriger dès qu'une règle est arbitrée en réunion.

## 1. Ce sur quoi le module s'appuie, et qui s'appuie sur lui

| Élément | Où | Rôle |
|---|---|---|
| `PanierItem.est_disponible`, `prix_unitaire` | `apps/panier/models.py` | Articles commandables et prix effectif (promo comprise) figé dans la commande |
| `Stock.decrementer()` / `incrementer()` sous `select_for_update` | `apps/catalogue/models.py` | Réservation au checkout, restitution à l'annulation |
| `Boutique.est_publiable` | `apps/vendeurs/models.py` | Boutique indisponible → commande non payée annulée (§ 7) |
| `EmailVerifie` | `apps/utilisateurs/permissions.py` | Email vérifié obligatoire pour valider un panier |
| `EstVendeurValide`, `EstAdministrateur` | `apps/vendeurs/permissions.py` | Espace vendeur, annulation par l'administration |
| `CouponReduction.calculer_remise()` | `apps/fidelite/models.py` | Remise en francs entiers (§ 4) |

| Module | Dépendance |
|---|---|
| `paiements` | Paiement en ligne d'une commande ou d'un groupe (reprend l'adresse **texte** du checkout, jamais le point GPS : § 3 bis) ; la validation confirme la commande par `commandes.services.confirmer_commande` ; remboursements et reversements aux vendeurs (§ 6, [`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md)) ; barème des frais vendeur figés au checkout (§ 4) |
| `livraison` | Fiche créée à la confirmation ; son expédition et sa livraison se répercutent sur la commande (§ 5) |
| `retours` | Restitue le stock à l'acceptation d'un retour |
| `support` | Un ticket peut viser une commande |

Aucun appel frontend à ce jour.

## 2. Modèle

- **`GroupeCommande`** : un checkout (une commande par boutique du panier) et **l'adresse de livraison** : `livraison_zone` (zone tarifaire, vide pour les groupes antérieurs aux frais de livraison), `livraison_commune`, `livraison_quartier`, `livraison_point_de_repere`, `livraison_telephone` (téléphone choisi par le client pour cette livraison, visible par le vendeur et le livreur ; jamais celui du profil), **`livraison_latitude`, `livraison_longitude`** (point GPS facultatif, `numeric(9, 6)`, vides pour l'existant ; contrainte en base `groupe_commande_position_complete_ou_absente` : les deux renseignés ou les deux vides).
- **`Commande`** : une boutique, un client, `numero_commande` (`CMD-<année>-<8 hex>`, nouvel essai en cas de collision, format inchangé), `status`, `motif_annulation` (`client`, `expiration`, `administration`, `boutique_indisponible`), `montant_total` (articles − remise + frais de livraison : le montant payé), `coupon_code`, `montant_remise`, **frais de livraison figés** : `frais_livraison`, `livraison_offerte`, `frais_livraison_vendeur` (jamais exposé au client), voir [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 9.
  - `client` et `boutique` en **PROTECT** : un historique de vente ne disparaît jamais avec un compte ou une boutique.
- **`CommandeItem`** : `nom_produit`, `prix_unitaire`, `quantite` figés au checkout ; `variante` en PROTECT. **Frais vendeur figés** au checkout (migration 0006) : `taux_commission`, `frais_fixe_unitaire`, `montant_commission`, `montant_frais_fixes`, `montant_net_vendeur` — jamais exposés au client.

## 3. Endpoints — `/api/commandes/`

### Client (`IsAuthenticated` ; ses commandes uniquement, 404 sinon)

| Méthode | URL | Description |
|---|---|---|
| POST | `valider-panier/` | + `EmailVerifie`, limite `commande_validation` 20/h par compte. Corps : `adresse_livraison` **obligatoire** (§ 3 bis), `coupon_code?`. Crée une commande par boutique (frais de livraison compris), réserve le stock, vide le panier. **201**, liste des commandes créées (avec `frais_livraison`, `livraison_offerte`). **503** sans tarif de livraison ou barème en vigueur |
| POST | `simuler-frais/` | Limite `commande_simulation` 120/h par compte (elle accepte un code promo : sans limite dédiée, elle servirait à essayer des codes). Même corps que `valider-panier/`. **Aucune écriture** (ni stock, ni coupon). **200** : `zone` (déduite de la commune), `commandes` (par boutique : `boutique`, `boutique_nom`, `montant_articles`, `remise`, `frais_livraison`, `livraison_offerte`, `montant_total`), `total_articles`, `total_remise`, `total_frais_livraison`, `total_a_payer` (FCFA entiers). **400** : panier vide, article indisponible, coupon non valable, adresse invalide. **503** : aucun tarif. Même calcul que la validation (`services.calculer_checkout`) |
| GET | `groupes/` | Groupes du client, avec `adresse_livraison` (point GPS compris) |
| GET | `` | Liste paginée des commandes du client |
| GET | `<uuid>/` | Détail **avec `articles` et `adresse_livraison`** (point GPS compris) |
| GET | `<uuid>/items/` | Articles (paginés) |
| POST | `<uuid>/annuler/` | **Nouveau.** Annulation tant que la commande n'est pas en préparation (`creee`, `confirmee`) ; stock restitué ; paiement encaissé → `Remboursement` à traiter. **200** (détail) ; **409** sinon |

### 3 bis. Adresse de livraison (checkout)

```json
"adresse_livraison": {
  "commune": "Cocody",
  "quartier": "Angré 8e Tranche",
  "point_de_repere": "Derrière la pharmacie",
  "telephone": "0707070707"
}
```

Les quatre champs sont obligatoires (400 sinon, aucune commande créée) : `commune` ≤ 100 (fixe le tarif de livraison ; choisie dans la liste de `GET /api/livraison/tarifs/` pour Abidjan), `quartier` ≤ 150, `point_de_repere` ≤ 500 caractères, `telephone` 8 à 15 chiffres (« + » initial accepté, espaces, points et tirets retirés). Le paiement reprend cette adresse ; plus aucune adresse par défaut. **La zone tarifaire n'est pas envoyée** : le serveur la déduit de la commune (commune du district d'Abidjan → `abidjan`, sinon `hors_abidjan`) et la fige dans le groupe ; un champ `zone` envoyé est ignoré. Les représentations d'adresse (`adresse_livraison` des commandes et groupes, `adresse` des livraisons) contiennent la `zone` déduite.

Erreurs de l'adresse : clé à points (`adresse_livraison.telephone`, `adresse_livraison.latitude`…), à la validation comme à la simulation. **Changement de contrat (septembre 2026)** : `valider-panier/` renvoyait jusqu'ici ces erreurs sans préfixe (`errors.telephone`), contrairement à la simulation et au format documenté ([`GUIDE_FRONTEND.md`](./GUIDE_FRONTEND.md) § 5). Adresse absente : toujours `errors.adresse_livraison`.

Corps qui n'est pas un objet JSON (liste, chaîne, nombre, booléen, `null`) : **400** `errors.non_field_errors`, avec le même message aux deux endpoints (« Donnée non valide. Attendait un dictionnaire, a reçu list. »). Avant, `valider-panier/` répondait **500**.

#### Point GPS (facultatif)

```json
"adresse_livraison": {
  "commune": "Cocody",
  "quartier": "Angré 8e Tranche",
  "point_de_repere": "Derrière la pharmacie",
  "telephone": "0707070707",
  "latitude": 5.359952,
  "longitude": -3.986912
}
```

- **Facultatif** : sans point (clés absentes ou `null`), le checkout fonctionne comme avant. Le point ne change aucun montant (le tarif dépend de la commune). Il sert au suivi du livreur et à l'estimation d'arrivée ; **sans point, aucune distance ni estimation n'est calculée**, jamais de valeur inventée.
- **Les deux ou aucune** : une coordonnée seule → **400** sur la clé manquante (`adresse_livraison.longitude` ou `adresse_livraison.latitude`).
- **Des nombres JSON** (degrés décimaux, ceux de `navigator.geolocation`) : chaîne (même `"5.36"`), booléen ou liste → **400**. `NaN` et `Infinity` → **400** (refusés par la lecture JSON stricte, et par le champ lui-même).
- **Côte d'Ivoire, avec une marge** : latitude de 4.0 à 11.0, longitude de -9.0 à -2.0, bornes incluses ; hors bornes → **400** sur la coordonnée concernée.
- **Arrondi à 6 décimales** (environ 11 cm, arrondi au plus proche, moitié vers le haut) avant enregistrement dans `GroupeCommande`.
- **En sortie** : `latitude` et `longitude` sont des **nombres** (pas des chaînes décimales comme les montants), ou `null` sans point.
- **Copies de l'adresse** : aucune n'emporte le point. `Paiement.adresse_livraison` et `Livraison.adresse_livraison` restent la ligne de texte du checkout (`GroupeCommande.adresse_livraison_texte`) : le paiement n'en a pas besoin, et les représentations de livraison lisent l'adresse structurée directement dans `GroupeCommande`. Le point n'existe donc qu'à un seul endroit.

**Vie privée.** Le point est l'emplacement précis du domicile (ou du lieu de livraison) du client. Il n'est visible que par **le client** (sa commande), **le livreur assigné pendant la livraison** (masqué avec toute l'adresse une fois la livraison `livree` ou `annulee`) et **l'administration** ; **jamais par le vendeur**, à qui l'adresse texte et le téléphone de livraison suffisent pour préparer le colis. Il n'est jamais journalisé.

| Représentation | Point GPS | Où c'est appliqué |
|---|---|---|
| Client : `GET <uuid>/`, `groupes/`, réponse de `<uuid>/annuler/` | oui | `CommandeDetailSerializer`, `GroupeCommandeSerializer` → `adresse_du_groupe(…, avec_position=True)` ; querysets filtrés sur `client=request.user` |
| Vendeur : `vendeur/`, `vendeur/<uuid>/`, réponse de `preparation/` | **jamais** | `CommandeVendeurSerializer` → `adresse_du_groupe(groupe)` (sans point par défaut) |
| Administration : réponse de `administration/<uuid>/annuler/` ; Django admin (`GroupeCommande`) | oui | `CommandeDetailSerializer` ; champs du modèle |
| Livraison (client, livreur, administration) | voir [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 5 | `_CoordonneesClientMixin` |

### Vendeur (`IsAuthenticated` + `EstVendeurValide` ; commandes de **sa** boutique, 404 sinon)

| Méthode | URL | Description |
|---|---|---|
| GET | `vendeur/` | Liste paginée |
| GET | `vendeur/<uuid>/` | Détail |
| POST | `vendeur/<uuid>/preparation/` | `confirmee` → `preparation`. **409** depuis un autre statut. Boutique suspendue : **403** sauf si la commande est déjà payée |

Représentation vendeur : `id`, `numero_commande`, `created_at`, `status`, `motif_annulation`, `montant_total`, `montant_remise`, `frais_livraison`, `livraison_offerte`, `frais_livraison_vendeur` (ce que la livraison offerte déduit de son reversement), `articles` (`nom_produit`, `variante`, `prix_unitaire`, `quantite`), `client` (**prénom + initiale du nom**, ex. « Awa K. »), `adresse_livraison` (commune, quartier, point de repère, téléphone de livraison). **Jamais** l'email ni le téléphone du profil du client, **ni le point GPS** (§ 3 bis). Une commande ne concerne qu'une boutique : un vendeur ne voit jamais les articles d'un autre vendeur.

### Administration (`IsAuthenticated` + `EstAdministrateur`)

| Méthode | URL | Description |
|---|---|---|
| POST | `administration/<uuid>/annuler/` | Annulation jusqu'à la préparation incluse ; stock restitué ; paiement encaissé → `Remboursement` à traiter. **409** après expédition. Journalisée (`securite`) |

`is_staff` ne donne aucun accès.

## 4. Montants

- Tout est **calculé côté serveur** : les montants envoyés par le client sont ignorés.
- Le **prix est figé** dans `CommandeItem` au checkout (prix effectif, promo comprise) : une commande passée ne change jamais de prix.
- **FCFA entiers** : la remise d'un coupon est arrondie **au franc inférieur** (le client ne paie jamais plus qu'annoncé, écart < 1 FCFA), puis répartie entre les boutiques **en francs entiers** au prorata de leur montant (la dernière absorbe le reste : la somme des parts égale exactement la remise).
- **Frais vendeur** : à la validation du panier, le barème en vigueur de chaque boutique (offre de lancement, sinon barème de la plateforme : depuis le 28/09/2026, **14 % + 100 FCFA par article jusqu'à 3 000 FCFA, 200 FCFA au-delà, TVA incluse** ; 12 % + 200 FCFA avant) est appliqué **ligne par ligne et figé** dans le `CommandeItem` (`frais_fixe_unitaire` = frais fixe réellement appliqué). Commission et seuil portent sur le prix effectif de l'article (promotion comprise), **avant remise** : un coupon est supporté par ANITCHE, jamais par le vendeur. Changer le barème ne modifie jamais une vente passée. Sans barème en vigueur, la validation répond **503** (erreur de configuration journalisée). Détail : [`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md) § 5.
- **Frais de livraison** : un tarif par commande selon la commune (zone déduite par le serveur), figé dans la commande ; payé par le client, ou offert par la boutique (déduit de son reversement). Jamais réduits par le coupon. Détail : [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 9.

## 5. Cycle de vie

Une seule fonction de transition (`apps/commandes/services.py`), appelée partout (vues, paiements, livraison, tâche d'expiration). Chaque transition est un UPDATE conditionnel sur le statut attendu : appliquée une seule fois, même sous requêtes simultanées.

| De | Vers | Qui | Effet |
|---|---|---|---|
| `creee` | `confirmee` | système : paiement en ligne validé (**plus de paiement à la livraison**) | fiche de livraison et reversement vendeur créés |
| `creee` | `annulee` | client · expiration (30 min) · boutique indisponible au paiement · administration | stock restitué |
| `confirmee` | `preparation` | vendeur de la boutique | |
| `confirmee` | `annulee` | client · administration | stock restitué, paiement encaissé → remboursement à traiter, reversement annulé |
| `preparation` | `expediee` | la livraison passe « expédiée » | |
| `preparation` | `annulee` | administration | stock restitué, paiement encaissé → remboursement à traiter, reversement annulé |
| `expediee` | `livree` | la livraison passe « livrée » (code de livraison saisi par le livreur) | délai de rétractation du reversement ouvert |
| `expediee` | `annulee` | administration, **uniquement** en abandonnant une livraison échouée (motif `livraison_echouee`, [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md)) | stock restitué, paiement encaissé → remboursement à traiter, reversement annulé |

Tout le reste est refusé (**409**) : retour arrière, saut d'étape, sortie d'un état final (`livree`, `annulee`). Côté livraison, le livreur qui passe une livraison « expédiée » alors que la commande n'est pas en préparation (ou annulée) reçoit 409 et rien ne change ; « en cours » et « échouée » ne changent pas la commande. Toute annulation passe la fiche de livraison « annulée » dans la même transaction, et elle est refusée (409) si la livraison est déjà partie (expédiée, en cours).

## 6. Commandes non payées, annulation et remboursement

- **Expiration** : tâche Celery `expirer_commandes_non_payees`, toutes les 5 minutes. Une commande encore `creee` 30 minutes après sa création (`COMMANDE_DELAI_PAIEMENT_MINUTES`) est annulée (`expiration`) et son stock restitué. **Aucune exception** : le client paie en ligne avant la livraison, le paiement à la livraison n'existe plus (`espece_livraison` → 400).
  - **Paiement en attente : le fournisseur est consulté d'abord** (octobre 2026, contre-audit M1 ; `paiements.services.reconcilier_avant_expiration`, appel hors transaction). S'il répond succès, le paiement est validé et la commande confirmée : pas d'expiration. S'il répond échec, le paiement passe `echoue` et l'expiration suit. S'il répond « en attente », ou s'il est injoignable, l'expiration est repoussée de `PAIEMENT_RECONCILIATION_DELAI_GRACE_MINUTES` (30 min par défaut, soit 60 min après la création), puis faite quand même. Un succès plus tardif est rattrapé par la réconciliation planifiée et devient un remboursement. Une commande sans paiement en attente expire à 30 minutes, sans appel au fournisseur. Détail : [`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md) § 8.
  - **Robustesse** (contre-audit A1) : la liste des commandes à expirer est figée avant la boucle (triée par `pk`), et chaque commande est annulée dans sa propre transaction. Un `OperationalError` (interblocage, conflit de sérialisation) sur une commande est journalisé en `WARNING` (« Expiration de la commande … reportée ») et n'arrête pas les suivantes. Cette commande est reprise à l'exécution suivante. L'ordre des verrous Commande → Paiement est désormais le même partout ([`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md) § 3).
- **Restitution du stock** : sous le même verrou que le checkout, **une seule fois** (seule la requête qui a effectivement annulé restitue).
- **Paiement confirmé après l'annulation** (paiement tardif après expiration, ou course) : la commande **n'est pas réactivée** ; le paiement est validé (l'argent est encaissé) et un **`Remboursement` à traiter** est créé pour la commande ; l'administration est alertée (journal `securite` + notification in-app de chaque administrateur actif, sans email depuis la refonte des notifications) ; ni confirmation, ni fiche de livraison, ni points de fidélité.
- **Annulation d'une commande payée** (client avant préparation, administration jusqu'à la préparation) : un `Remboursement` à traiter est créé pour cette commande seule (les autres commandes du même paiement restent payées) et son reversement vendeur est annulé. Un paiement encore en attente dont toutes les commandes sont annulées est annulé.
- Le traitement du remboursement (manuel par l'administration au lancement) : [`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md) § 6. Le statut « à rembourser » du paiement n'existe plus.

## 7. Vendeur suspendu (boutique non publiable)

- **Commande non payée** : le paiement est refusé (400, message générique « Cette commande n'est plus disponible… », sans le mot « suspendue ») ; la commande est annulée (`boutique_indisponible`) et son stock restitué. Pour un groupe, seules les commandes des boutiques indisponibles sont annulées ; le groupe reste payable pour le reste.
- **Commande déjà payée** : peut être honorée (le vendeur peut encore la passer en préparation) ; l'administration peut l'annuler.
- Toute autre nouvelle transition du vendeur sur une boutique suspendue : 403.

## 8. Concurrence et idempotence

- Double validation du même panier (double clic, nouvel essai réseau) : le panier est verrouillé ; une seule commande, stock décrémenté une fois, la seconde requête reçoit 400 « Le panier est vide. ».
- Double annulation : une seule réussit (200), l'autre reçoit 409 ; stock restitué une fois.
- Coupon : verrouillé pendant le checkout, consommé une seule fois.
- **Ordre des verrous de stock (octobre 2026, contre-audit A5).** Le checkout (`ValiderPanierView`), l'annulation (`annuler_commande`) et la réintégration d'un retour (`retours.services._reintegrer_stock`) verrouillent leurs stocks par `select_for_update().filter(variante_id__in=…).order_by("variante_id")` : toujours dans l'ordre croissant des clés, **quel que soit le plan** choisi par PostgreSQL. Sans `ORDER BY`, les lignes sont verrouillées dans l'ordre que produit le plan : ordre des clés pour un parcours d'index, ordre physique pour une lecture séquentielle ou un *bitmap heap scan*, et l'ordre physique d'un stock change à chaque vente (chaque `UPDATE` crée une nouvelle version de la ligne). Un checkout et une annulation (ou un retour) sur deux mêmes variantes pouvaient donc s'interbloquer, avec un 500 pour l'un des deux après 1 s. Ce sont les trois seuls verrous de plusieurs stocks ; les autres (`catalogue`) n'en prennent qu'un. Ordre complet : checkout = Panier → Coupon → Stocks ; annulation = Livraison → Commande → Stocks → Paiement → Reversement → Coupon ([`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md) § 3).
- **Annulation pendant la validation du paiement (octobre 2026, H1).** Le client (ou l'administration) annule au moment où le webhook de succès confirme la commande : l'annulation ne trouve pas encore de fiche de livraison, puis son `UPDATE` attend la commande verrouillée par le webhook ; réévalué après le commit, il annule la commande confirmée. Avant, la fiche créée entre-temps restait « en attente » sur une commande annulée, sans aucune action de l'API pour l'annuler. `annuler_commande` relit maintenant la fiche après l'`UPDATE` quand la première lecture n'a rien trouvé, et l'annule : paiement validé, **un seul** remboursement, stock restitué une fois, reversement annulé ([`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 12).

## 9. Impact frontend

1. **Adresse obligatoire au checkout.** `POST /api/commandes/valider-panier/` exige `adresse_livraison` : `commune`, `quartier`, `point_de_repere`, `telephone` (format au § 3 bis). Sans elle : **400** (`errors.adresse_livraison`). Prévoir le formulaire avant la validation du panier ; le téléphone saisi est celui que le vendeur et le livreur utiliseront.
2. **Paiement en ligne uniquement, sans adresse.** `methode: "espece_livraison"` → **400** : le client paie en ligne (Wave, Orange Money, MTN MoMo, Moov Money, carte) avant la livraison ; voir [`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md) § 10. `POST /api/paiements/initier/` n'a plus besoin de `adresse_livraison` (champ **déprécié et ignoré** : l'adresse vient du checkout). Nouveau refus **400** « Cette commande n'est plus disponible… » quand la boutique n'est plus disponible (la commande est alors annulée : rafraîchir le panier / les commandes).
3. **Annulation.** `POST /api/commandes/<id>/annuler/` (sans corps) : **200** avec le détail de la commande (`status: "annulee"`, `motif_annulation: "client"`) ; **409** si la commande est en préparation ou au-delà (proposer de contacter le support).
4. **Détail enrichi.** `GET /api/commandes/<id>/` renvoie aussi `articles` et `adresse_livraison` ; la liste et le détail portent `motif_annulation`. `GET /api/commandes/groupes/` renvoie `adresse_livraison`.
5. **Espace vendeur.** `GET /api/commandes/vendeur/`, `GET /api/commandes/vendeur/<id>/`, `POST /api/commandes/vendeur/<id>/preparation/` (réponses au § 3).
6. **Statuts désormais utilisés.** `creee` (en attente de paiement, **annulée automatiquement après 30 minutes**, jusqu'à 60 minutes si un paiement est encore en attente chez le fournisseur), `confirmee`, `preparation`, `expediee`, `livree`, `annulee` (+ `motif_annulation` : `client`, `expiration`, `administration`, `boutique_indisponible`). Afficher un compte à rebours de paiement sur une commande `creee`.
7. **Livraison.** `GET /api/livraison/…` expose `telephone_contact`. Faire passer une livraison « expédiée » avant que le vendeur ait mis la commande en préparation renvoie **409**.
8. **Frais de livraison (changement de contrat, septembre 2026).** Ne **pas** envoyer de zone (ignorée) : proposer la commune dans un menu déroulant construit depuis `GET /api/livraison/tarifs/` (communes du district d'Abidjan et leur tarif), plus « Autre ville » en saisie libre ; la zone réellement appliquée revient dans la simulation et dans `adresse_livraison.zone`. Appeler `POST /api/commandes/simuler-frais/` à chaque changement d'adresse ou de coupon et afficher les montants par commande avant le paiement. `montant_total` inclut désormais les frais ; nouveaux champs `frais_livraison` et `livraison_offerte` (badge « Livraison offerte »).
9. **Format d'erreur unifié (septembre 2026).** Toutes les erreurs suivent le format commun `{success: false, status_code, detail, errors}`, `errors` toujours un objet clé → liste de messages (`{}` si aucun champ n'est en cause). Les **409** (annulation client ou administration, passage en préparation) et le **403** « boutique suspendue » du passage en préparation portaient avant un simple `{"detail": …}` : lire désormais `detail` dans l'enveloppe commune. Un « stock insuffisant » à la validation arrive dans `errors.non_field_errors`.
10. **Point GPS au checkout (septembre 2026), facultatif.** Bouton « Utiliser ma position » (`navigator.geolocation`) qui ajoute `latitude` et `longitude` **en nombres** à `adresse_livraison`, les deux ou aucune (§ 3 bis ; exemple de code : [`GUIDE_FRONTEND.md`](./GUIDE_FRONTEND.md) § 11). Sans point, rien ne change. Refus **400** sur `adresse_livraison.latitude` / `adresse_livraison.longitude` (coordonnée seule, hors Côte d'Ivoire, chaîne) : proposer de continuer sans point. En lecture, `adresse_livraison.latitude` / `longitude` (nombres ou `null`) pour le client ; **absents** de la représentation vendeur.
11. **Erreurs d'adresse à la validation (changement de contrat).** `POST valider-panier/` renvoie désormais les erreurs de l'adresse sous des clés à points (`errors["adresse_livraison.telephone"]`), comme `simuler-frais/`. Avant : `errors.telephone`. Aucun appel frontend n'existait encore (§ 1). Un corps qui n'est pas un objet JSON reçoit maintenant un **400** (`errors.non_field_errors`) au lieu d'un 500.

## 10. Dette connue

- **Niveau 2 — `Idempotency-Key`** : aujourd'hui, un nouvel essai après une coupure réseau renvoie « panier vide » au lieu de la réponse 201 d'origine (aucune double commande possible grâce au verrou). Une clé d'idempotence permettrait de rejouer la même réponse.
- ~~Assignation du livreur~~ : traitée (API d'administration, [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md)).
- **Anonymisation à la suppression de compte** : `Commande.client` en PROTECT empêche de supprimer un compte qui a commandé ; il faudra un flux d'anonymisation.
- **Retours** : le module retours accepte une demande sur une commande `confirmee` (pas encore expédiée) ; à revoir avec ce module maintenant que les statuts `expediee` et `livree` sont réellement posés.
- ~~Livraison d'une commande annulée~~ : traitée (statut `annulee` côté livraison, [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md)).
- Commandes créées avant l'adresse obligatoire (`GroupeCommande` sans adresse) : non payables (400 « Adresse de livraison manquante ») ; elles expirent et libèrent leur stock.
- **Cycle coupon public / stock** (rapport PR 1, point 2b, reproduit, non corrigé) : le checkout verrouille le Coupon **puis** les Stocks, l'annulation les Stocks **puis** le Coupon (`restituer_coupon`). Avec un code promo partagé, un checkout et une annulation simultanés peuvent s'interbloquer (500 pour l'un des deux), indépendamment du plan. Pas de perte d'argent. À traiter avec le module fidélité, **avant toute vente flash avec un code promo public**.

## 11. Tests

⚠️ **Toujours lancer les tests sur PostgreSQL.** Sous SQLite, `ConcurrenceCommandesTests` est **sauté**.

```bash
cd backend-django
DJANGO_SETTINGS_MODULE=config.settings.ci DB_NAME=anitche_test DB_USER=postgres DB_PASSWORD=postgres DB_HOST=localhost \
  python manage.py test apps.commandes apps.paiements apps.livraison -v 2
```

`apps/commandes/tests.py` — 53 tests (adaptés au module paiements : paiement en ligne au lieu du paiement à la livraison, `Remboursement` au lieu du statut « à rembourser » ; frais figés testés dans `apps/paiements/tests.py`, `FraisTests`). Refonte : adresse (obligatoire, complète, téléphone valide, reprise par le paiement, livraison et téléphone de contact, paiement refusé sans adresse), montants (serveur, prix figé avec promo, remise et répartition en francs entiers), annulation client (stock restitué une fois, remboursement à traiter + alerte admin, 409 après préparation, 404 pour un autre client), expiration (30 min, une seule restitution, commandes confirmées épargnées, paiement en attente : délai de grâce puis expiration, et paiement tardif : commande non réactivée, remboursement, alerte ; succès constaté chez le fournisseur : commande confirmée au lieu d'expirer ; interblocage sur une commande sans effet sur les suivantes, reprise à l'exécution suivante), machine à états (parcours complet, synchronisation depuis la livraison, pas de saut ni de retour arrière), espace vendeur (isolation, données minimales, accès, boutique suspendue et commandes payées, N+1), boutique indisponible au paiement (commande seule et groupe), annulation par l'administration, détail avec articles, collision de numéro, PROTECT, concurrence (double validation, double annulation).

**Frais de livraison** : `FraisDeLivraisonCheckoutTests` et `SimulationDuCheckoutTests` (détail : [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 14) ; les montants attendus des tests existants incluent le tarif Abidjan de la migration (`FRAIS_ABIDJAN`).

**Ordre des verrous de stock (A5, octobre 2026)** — `ConcurrenceCommandesTests.test_stocks_verrouilles_dans_l_ordre_des_cles_quel_que_soit_le_plan` (PostgreSQL, 84 tests au total dans `apps/commandes/tests.py`) : deux variantes X < Y d'une même boutique, la ligne de X déplacée physiquement après Y (vérifié : `[X, Y]` par index, `[Y, X]` en lecture séquentielle) ; un autre checkout tient Y ; l'annulation sur la vraie route (lecture séquentielle forcée par `SET enable_indexscan / enable_bitmapscan = off`) puis un checkout sur la vraie route (index forcé par `SET enable_seqscan / enable_bitmapscan = off`) attendent, synchronisés par `pg_locks`. Attendu : annulation 200, checkout 201, stocks exacts. Avant la correction : `deadlock detected`, l'une des deux requêtes en 500.

**Corps du checkout** : `AdresseLivraisonTests.test_corps_qui_n_est_pas_un_objet_400_comme_la_simulation` (liste, liste vide, chaîne, nombre, booléen, `null` : 400 `non_field_errors` à la validation, mêmes `detail` et `errors` qu'à la simulation, rien de créé ; échouait en 500 avant la correction).

**Point GPS (§ 3 bis)** — 12 tests, `apps/commandes/tests.py` (81 au total) : `PositionLivraisonCheckoutTests` (checkout sans point inchangé, `null` = absent, point enregistré sans effet sur les montants, arrondi à 6 décimales avec moitié vers le haut, entiers et bornes incluses, refus 400 sur la clé à points à la validation **et** à la simulation : coordonnée seule, hors bornes dans les quatre directions, Paris, chaîne numérique, chaîne, booléen, liste, entier démesuré ; `NaN` / `Infinity` refusés par la lecture JSON et par le champ ; erreurs d'adresse de la validation sur des clés à points), `PositionLivraisonVisibiliteTests` (client : détail, groupes, annulation, en nombres ; autre client : rien ; vendeur : jamais, détail, liste et passage en préparation ; administration : oui ; contrainte en base). Côté livraison : `livraison.PositionLivraisonTests` (5 tests, [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 14) ; démo : `demo.SeedDemoTests.test_un_seul_point_gps_sur_la_commande_en_preparation`. Suite complète : **835 tests, OK, aucun « skipped »** (27/09/2026).

Postman : `postman_commandes.json` (hors dépôt) — connexions, remise en état (admin), préparation, parcours client (checkout avec adresse **et point GPS**, **paiement en ligne simulé** : initiation puis notification signée du fournisseur simulé), parcours vendeur (préparation, **pas de point GPS**), annulation, scénarios de sécurité (dont **coordonnée seule → 400** et **hors Côte d'Ivoire → 400**), nettoyage. Rejouable ; `admin_password` à renseigner. `postman_panier.json` et `postman_paiements.json` envoient désormais l'adresse au checkout.

## 12. Migrations

- **commandes 0005** : adresse de livraison sur `GroupeCommande` (vide pour l'existant), `motif_annulation`, PROTECT sur `client` et `boutique`.
- **paiements 0002** : statut `a_rembourser` ; `adresse_livraison` sans valeur par défaut fictive.
- **commandes 0006** : frais vendeur figés sur `CommandeItem` (ventes passées : aucun frais, net = prix de la ligne).
- **paiements 0003 / 0004** : refonte du module paiements (voir [`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md) § 13) ; les paiements « à la livraison » encore actifs passent annulés.
- **commandes 0008** : frais de livraison figés sur `Commande` (0 pour l'existant) et `GroupeCommande.livraison_zone` (vide pour l'existant). Voir [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 13.
- **commandes 0009** : `GroupeCommande.livraison_latitude` et `livraison_longitude` (`numeric(9, 6)`, `NULL`) et la contrainte `groupe_commande_position_complete_ou_absente`. Deux `ADD COLUMN … NULL` sans valeur par défaut : aucune réécriture de la table sous PostgreSQL ; le `CHECK` relit les lignes existantes (toutes sans point, donc valides) sans les modifier.
- **livraison 0006** (données, octobre 2026) : fiches de livraison non terminées des commandes annulées → `annulee` (H1, [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 13). Aucune migration côté commandes.

Base de dev avant application : 2 commandes `creee` anciennes (sans adresse), 0 paiement. Elles seront annulées par la tâche d'expiration — le service `celery-beat` de dev doit être redémarré pour charger la nouvelle planification.
