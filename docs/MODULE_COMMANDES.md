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
| `paiements` | Paiement en ligne d'une commande ou d'un groupe (reprend l'adresse du checkout) ; la validation confirme la commande par `commandes.services.confirmer_commande` ; remboursements et reversements aux vendeurs (§ 6, [`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md)) ; barème des frais vendeur figés au checkout (§ 4) |
| `livraison` | Fiche créée à la confirmation ; son expédition et sa livraison se répercutent sur la commande (§ 5) |
| `retours` | Restitue le stock à l'acceptation d'un retour |
| `support` | Un ticket peut viser une commande |

Aucun appel frontend à ce jour.

## 2. Modèle

- **`GroupeCommande`** : un checkout (une commande par boutique du panier) et **l'adresse de livraison** : `livraison_zone` (zone tarifaire, vide pour les groupes antérieurs aux frais de livraison), `livraison_commune`, `livraison_quartier`, `livraison_point_de_repere`, `livraison_telephone` (téléphone choisi par le client pour cette livraison, visible par le vendeur et le livreur ; jamais celui du profil).
- **`Commande`** : une boutique, un client, `numero_commande` (`CMD-<année>-<8 hex>`, nouvel essai en cas de collision, format inchangé), `status`, `motif_annulation` (`client`, `expiration`, `administration`, `boutique_indisponible`), `montant_total` (articles − remise + frais de livraison : le montant payé), `coupon_code`, `montant_remise`, **frais de livraison figés** : `frais_livraison`, `livraison_offerte`, `frais_livraison_vendeur` (jamais exposé au client), voir [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 9.
  - `client` et `boutique` en **PROTECT** : un historique de vente ne disparaît jamais avec un compte ou une boutique.
- **`CommandeItem`** : `nom_produit`, `prix_unitaire`, `quantite` figés au checkout ; `variante` en PROTECT. **Frais vendeur figés** au checkout (migration 0006) : `taux_commission`, `frais_fixe_unitaire`, `montant_commission`, `montant_frais_fixes`, `montant_net_vendeur` — jamais exposés au client.

## 3. Endpoints — `/api/commandes/`

### Client (`IsAuthenticated` ; ses commandes uniquement, 404 sinon)

| Méthode | URL | Description |
|---|---|---|
| POST | `valider-panier/` | + `EmailVerifie`, limite `commande_validation` 20/h par compte. Corps : `adresse_livraison` **obligatoire** (§ 3 bis), `coupon_code?`. Crée une commande par boutique (frais de livraison compris), réserve le stock, vide le panier. **201**, liste des commandes créées (avec `frais_livraison`, `livraison_offerte`). **503** sans tarif de livraison ou barème en vigueur |
| POST | `simuler-frais/` | Limite `commande_simulation` 120/h par compte (elle accepte un code promo : sans limite dédiée, elle servirait à essayer des codes). Même corps que `valider-panier/`. **Aucune écriture** (ni stock, ni coupon). **200** : `zone` (déduite de la commune), `commandes` (par boutique : `boutique`, `boutique_nom`, `montant_articles`, `remise`, `frais_livraison`, `livraison_offerte`, `montant_total`), `total_articles`, `total_remise`, `total_frais_livraison`, `total_a_payer` (FCFA entiers). **400** : panier vide, article indisponible, coupon non valable, adresse invalide. **503** : aucun tarif. Même calcul que la validation (`services.calculer_checkout`) |
| GET | `groupes/` | Groupes du client, avec `adresse_livraison` |
| GET | `` | Liste paginée des commandes du client |
| GET | `<uuid>/` | Détail **avec `articles` et `adresse_livraison`** |
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

### Vendeur (`IsAuthenticated` + `EstVendeurValide` ; commandes de **sa** boutique, 404 sinon)

| Méthode | URL | Description |
|---|---|---|
| GET | `vendeur/` | Liste paginée |
| GET | `vendeur/<uuid>/` | Détail |
| POST | `vendeur/<uuid>/preparation/` | `confirmee` → `preparation`. **409** depuis un autre statut. Boutique suspendue : **403** sauf si la commande est déjà payée |

Représentation vendeur : `id`, `numero_commande`, `created_at`, `status`, `motif_annulation`, `montant_total`, `montant_remise`, `frais_livraison`, `livraison_offerte`, `frais_livraison_vendeur` (ce que la livraison offerte déduit de son reversement), `articles` (`nom_produit`, `variante`, `prix_unitaire`, `quantite`), `client` (**prénom + initiale du nom**, ex. « Awa K. »), `adresse_livraison` (commune, quartier, point de repère, téléphone de livraison). **Jamais** l'email ni le téléphone du profil du client. Une commande ne concerne qu'une boutique : un vendeur ne voit jamais les articles d'un autre vendeur.

### Administration (`IsAuthenticated` + `EstAdministrateur`)

| Méthode | URL | Description |
|---|---|---|
| POST | `administration/<uuid>/annuler/` | Annulation jusqu'à la préparation incluse ; stock restitué ; paiement encaissé → `Remboursement` à traiter. **409** après expédition. Journalisée (`securite`) |

`is_staff` ne donne aucun accès.

## 4. Montants

- Tout est **calculé côté serveur** : les montants envoyés par le client sont ignorés.
- Le **prix est figé** dans `CommandeItem` au checkout (prix effectif, promo comprise) : une commande passée ne change jamais de prix.
- **FCFA entiers** : la remise d'un coupon est arrondie **au franc inférieur** (le client ne paie jamais plus qu'annoncé, écart < 1 FCFA), puis répartie entre les boutiques **en francs entiers** au prorata de leur montant (la dernière absorbe le reste : la somme des parts égale exactement la remise).
- **Frais vendeur** : à la validation du panier, le barème en vigueur de chaque boutique (offre de lancement, sinon barème de la plateforme : 12 % + 200 FCFA par article par défaut) est appliqué **ligne par ligne et figé** dans le `CommandeItem`. La commission porte sur le prix **avant remise** : un coupon est supporté par ANITCHE, jamais par le vendeur. Changer le barème ne modifie jamais une vente passée. Sans barème en vigueur, la validation répond **503** (erreur de configuration journalisée). Détail : [`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md) § 5.
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

## 9. Impact frontend

1. **Adresse obligatoire au checkout.** `POST /api/commandes/valider-panier/` exige `adresse_livraison` : `commune`, `quartier`, `point_de_repere`, `telephone` (format au § 3 bis). Sans elle : **400** (`errors.adresse_livraison`). Prévoir le formulaire avant la validation du panier ; le téléphone saisi est celui que le vendeur et le livreur utiliseront.
2. **Paiement en ligne uniquement, sans adresse.** `methode: "espece_livraison"` → **400** : le client paie en ligne (Wave, Orange Money, MTN MoMo, Moov Money, carte) avant la livraison ; voir [`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md) § 10. `POST /api/paiements/initier/` n'a plus besoin de `adresse_livraison` (champ **déprécié et ignoré** : l'adresse vient du checkout). Nouveau refus **400** « Cette commande n'est plus disponible… » quand la boutique n'est plus disponible (la commande est alors annulée : rafraîchir le panier / les commandes).
3. **Annulation.** `POST /api/commandes/<id>/annuler/` (sans corps) : **200** avec le détail de la commande (`status: "annulee"`, `motif_annulation: "client"`) ; **409** si la commande est en préparation ou au-delà (proposer de contacter le support).
4. **Détail enrichi.** `GET /api/commandes/<id>/` renvoie aussi `articles` et `adresse_livraison` ; la liste et le détail portent `motif_annulation`. `GET /api/commandes/groupes/` renvoie `adresse_livraison`.
5. **Espace vendeur.** `GET /api/commandes/vendeur/`, `GET /api/commandes/vendeur/<id>/`, `POST /api/commandes/vendeur/<id>/preparation/` (réponses au § 3).
6. **Statuts désormais utilisés.** `creee` (en attente de paiement, **annulée automatiquement après 30 minutes**), `confirmee`, `preparation`, `expediee`, `livree`, `annulee` (+ `motif_annulation` : `client`, `expiration`, `administration`, `boutique_indisponible`). Afficher un compte à rebours de paiement sur une commande `creee`.
7. **Livraison.** `GET /api/livraison/…` expose `telephone_contact`. Faire passer une livraison « expédiée » avant que le vendeur ait mis la commande en préparation renvoie **409**.
8. **Frais de livraison (changement de contrat, septembre 2026).** Ne **pas** envoyer de zone (ignorée) : proposer la commune dans un menu déroulant construit depuis `GET /api/livraison/tarifs/` (communes du district d'Abidjan et leur tarif), plus « Autre ville » en saisie libre ; la zone réellement appliquée revient dans la simulation et dans `adresse_livraison.zone`. Appeler `POST /api/commandes/simuler-frais/` à chaque changement d'adresse ou de coupon et afficher les montants par commande avant le paiement. `montant_total` inclut désormais les frais ; nouveaux champs `frais_livraison` et `livraison_offerte` (badge « Livraison offerte »).
9. **Format d'erreur unifié (septembre 2026).** Toutes les erreurs suivent le format commun `{success: false, status_code, detail, errors}`, `errors` toujours un objet clé → liste de messages (`{}` si aucun champ n'est en cause). Les **409** (annulation client ou administration, passage en préparation) et le **403** « boutique suspendue » du passage en préparation portaient avant un simple `{"detail": …}` : lire désormais `detail` dans l'enveloppe commune. Un « stock insuffisant » à la validation arrive dans `errors.non_field_errors`.

## 10. Dette connue

- **Niveau 2 — `Idempotency-Key`** : aujourd'hui, un nouvel essai après une coupure réseau renvoie « panier vide » au lieu de la réponse 201 d'origine (aucune double commande possible grâce au verrou). Une clé d'idempotence permettrait de rejouer la même réponse.
- ~~Assignation du livreur~~ : traitée (API d'administration, [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md)).
- **Anonymisation à la suppression de compte** : `Commande.client` en PROTECT empêche de supprimer un compte qui a commandé ; il faudra un flux d'anonymisation.
- **Retours** : le module retours accepte une demande sur une commande `confirmee` (pas encore expédiée) ; à revoir avec ce module maintenant que les statuts `expediee` et `livree` sont réellement posés.
- ~~Livraison d'une commande annulée~~ : traitée (statut `annulee` côté livraison, [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md)).
- Commandes créées avant l'adresse obligatoire (`GroupeCommande` sans adresse) : non payables (400 « Adresse de livraison manquante ») ; elles expirent et libèrent leur stock.

## 11. Tests

⚠️ **Toujours lancer les tests sur PostgreSQL.** Sous SQLite, `ConcurrenceCommandesTests` est **sauté**.

```bash
cd backend-django
DJANGO_SETTINGS_MODULE=config.settings.ci DB_NAME=anitche_test DB_USER=postgres DB_PASSWORD=postgres DB_HOST=localhost \
  python manage.py test apps.commandes apps.paiements apps.livraison -v 2
```

`apps/commandes/tests.py` — 53 tests (adaptés au module paiements : paiement en ligne au lieu du paiement à la livraison, `Remboursement` au lieu du statut « à rembourser » ; frais figés testés dans `apps/paiements/tests.py`, `FraisTests`). Refonte : adresse (obligatoire, complète, téléphone valide, reprise par le paiement, livraison et téléphone de contact, paiement refusé sans adresse), montants (serveur, prix figé avec promo, remise et répartition en francs entiers), annulation client (stock restitué une fois, remboursement à traiter + alerte admin, 409 après préparation, 404 pour un autre client), expiration (30 min, une seule restitution, commandes confirmées épargnées, paiement tardif : commande non réactivée, remboursement, alerte), machine à états (parcours complet, synchronisation depuis la livraison, pas de saut ni de retour arrière), espace vendeur (isolation, données minimales, accès, boutique suspendue et commandes payées, N+1), boutique indisponible au paiement (commande seule et groupe), annulation par l'administration, détail avec articles, collision de numéro, PROTECT, concurrence (double validation, double annulation).

**Frais de livraison** : `FraisDeLivraisonCheckoutTests` et `SimulationDuCheckoutTests` (détail : [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 14) ; les montants attendus des tests existants incluent le tarif Abidjan de la migration (`FRAIS_ABIDJAN`).

Postman : `postman_commandes.json` (hors dépôt) — connexions, remise en état (admin), préparation, parcours client (checkout avec adresse, **paiement en ligne simulé** : initiation puis notification signée du fournisseur simulé), parcours vendeur (préparation), annulation, scénarios de sécurité, nettoyage. Rejouable ; `admin_password` à renseigner. `postman_panier.json` et `postman_paiements.json` envoient désormais l'adresse au checkout.

## 12. Migrations

- **commandes 0005** : adresse de livraison sur `GroupeCommande` (vide pour l'existant), `motif_annulation`, PROTECT sur `client` et `boutique`.
- **paiements 0002** : statut `a_rembourser` ; `adresse_livraison` sans valeur par défaut fictive.
- **commandes 0006** : frais vendeur figés sur `CommandeItem` (ventes passées : aucun frais, net = prix de la ligne).
- **paiements 0003 / 0004** : refonte du module paiements (voir [`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md) § 13) ; les paiements « à la livraison » encore actifs passent annulés.
- **commandes 0008** : frais de livraison figés sur `Commande` (0 pour l'existant) et `GroupeCommande.livraison_zone` (vide pour l'existant). Voir [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 13.

Base de dev avant application : 2 commandes `creee` anciennes (sans adresse), 0 paiement. Elles seront annulées par la tâche d'expiration — le service `celery-beat` de dev doit être redémarré pour charger la nouvelle planification.
