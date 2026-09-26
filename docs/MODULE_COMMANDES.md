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
| `paiements` | Initie le paiement d'une commande ou d'un groupe (reprend l'adresse du checkout) ; la validation confirme la commande par `commandes.services.confirmer_commande` ; statut « à rembourser » (§ 6) |
| `livraison` | Fiche créée à la confirmation ; son expédition et sa livraison se répercutent sur la commande (§ 5) |
| `retours` | Restitue le stock à l'acceptation d'un retour |
| `support` | Un ticket peut viser une commande |

Aucun appel frontend à ce jour.

## 2. Modèle

- **`GroupeCommande`** : un checkout (une commande par boutique du panier) et **l'adresse de livraison** : `livraison_commune`, `livraison_quartier`, `livraison_point_de_repere`, `livraison_telephone` (téléphone choisi par le client pour cette livraison, visible par le vendeur et le livreur ; jamais celui du profil).
- **`Commande`** : une boutique, un client, `numero_commande` (`CMD-<année>-<8 hex>`, nouvel essai en cas de collision, format inchangé), `status`, `motif_annulation` (`client`, `expiration`, `administration`, `boutique_indisponible`), `montant_total`, `coupon_code`, `montant_remise`.
  - `client` et `boutique` en **PROTECT** : un historique de vente ne disparaît jamais avec un compte ou une boutique.
- **`CommandeItem`** : `nom_produit`, `prix_unitaire`, `quantite` figés au checkout ; `variante` en PROTECT.

## 3. Endpoints — `/api/commandes/`

### Client (`IsAuthenticated` ; ses commandes uniquement, 404 sinon)

| Méthode | URL | Description |
|---|---|---|
| POST | `valider-panier/` | + `EmailVerifie`, limite `commande_validation` 20/h par compte. Corps : `adresse_livraison` **obligatoire** (§ 3 bis), `coupon_code?`. Crée une commande par boutique, réserve le stock, vide le panier. **201**, liste des commandes créées |
| GET | `groupes/` | Groupes du client, avec `adresse_livraison` |
| GET | `` | Liste paginée des commandes du client |
| GET | `<uuid>/` | Détail **avec `articles` et `adresse_livraison`** |
| GET | `<uuid>/items/` | Articles (paginés) |
| POST | `<uuid>/annuler/` | **Nouveau.** Annulation tant que la commande n'est pas en préparation (`creee`, `confirmee`) ; stock restitué ; paiement encaissé → « à rembourser ». **200** (détail) ; **409** sinon |

### 3 bis. Adresse de livraison (checkout)

```json
"adresse_livraison": {
  "commune": "Cocody",
  "quartier": "Angré 8e Tranche",
  "point_de_repere": "Derrière la pharmacie",
  "telephone": "0707070707"
}
```

Les quatre champs sont obligatoires (400 sinon, aucune commande créée) : `commune` ≤ 100, `quartier` ≤ 150, `point_de_repere` ≤ 500 caractères, `telephone` 8 à 15 chiffres (« + » initial accepté, espaces, points et tirets retirés). Le paiement reprend cette adresse ; plus aucune adresse par défaut.

### Vendeur (`IsAuthenticated` + `EstVendeurValide` ; commandes de **sa** boutique, 404 sinon)

| Méthode | URL | Description |
|---|---|---|
| GET | `vendeur/` | Liste paginée |
| GET | `vendeur/<uuid>/` | Détail |
| POST | `vendeur/<uuid>/preparation/` | `confirmee` → `preparation`. **409** depuis un autre statut. Boutique suspendue : **403** sauf si la commande est déjà payée |

Représentation vendeur : `id`, `numero_commande`, `created_at`, `status`, `motif_annulation`, `montant_total`, `montant_remise`, `articles` (`nom_produit`, `variante`, `prix_unitaire`, `quantite`), `client` (**prénom + initiale du nom**, ex. « Awa K. »), `adresse_livraison` (commune, quartier, point de repère, téléphone de livraison). **Jamais** l'email ni le téléphone du profil du client. Une commande ne concerne qu'une boutique : un vendeur ne voit jamais les articles d'un autre vendeur.

### Administration (`IsAuthenticated` + `EstAdministrateur`)

| Méthode | URL | Description |
|---|---|---|
| POST | `administration/<uuid>/annuler/` | Annulation jusqu'à la préparation incluse ; stock restitué ; paiement encaissé → « à rembourser ». **409** après expédition. Journalisée (`securite`) |

`is_staff` ne donne aucun accès.

## 4. Montants

- Tout est **calculé côté serveur** : les montants envoyés par le client sont ignorés.
- Le **prix est figé** dans `CommandeItem` au checkout (prix effectif, promo comprise) : une commande passée ne change jamais de prix.
- **FCFA entiers** : la remise d'un coupon est arrondie **au franc inférieur** (le client ne paie jamais plus qu'annoncé, écart < 1 FCFA), puis répartie entre les boutiques **en francs entiers** au prorata de leur montant (la dernière absorbe le reste : la somme des parts égale exactement la remise).

## 5. Cycle de vie

Une seule fonction de transition (`apps/commandes/services.py`), appelée partout (vues, paiements, livraison, tâche d'expiration). Chaque transition est un UPDATE conditionnel sur le statut attendu : appliquée une seule fois, même sous requêtes simultanées.

| De | Vers | Qui | Effet |
|---|---|---|---|
| `creee` | `confirmee` | système : paiement validé, ou choix du paiement à la livraison | fiche de livraison créée |
| `creee` | `annulee` | client · expiration (30 min) · boutique indisponible au paiement · administration | stock restitué |
| `confirmee` | `preparation` | vendeur de la boutique | |
| `confirmee` | `annulee` | client · administration | stock restitué, paiement encaissé → « à rembourser » |
| `preparation` | `expediee` | la livraison passe « expédiée » | |
| `preparation` | `annulee` | administration | stock restitué, paiement encaissé → « à rembourser » |
| `expediee` | `livree` | la livraison passe « livrée » | |

Tout le reste est refusé (**409**) : retour arrière, saut d'étape, sortie d'un état final (`livree`, `annulee`). Côté livraison, le livreur ou l'admin qui passe une livraison « expédiée » alors que la commande n'est pas en préparation (ou annulée) reçoit 409 et rien ne change ; « en cours » et « échouée » ne changent pas la commande.

## 6. Commandes non payées, annulation et remboursement

- **Expiration** : tâche Celery `expirer_commandes_non_payees`, toutes les 5 minutes. Une commande encore `creee` 30 minutes après sa création (`COMMANDE_DELAI_PAIEMENT_MINUTES`) est annulée (`expiration`) et son stock restitué. Le paiement à la livraison confirme la commande immédiatement : il n'est pas concerné.
- **Restitution du stock** : sous le même verrou que le checkout, **une seule fois** (seule la requête qui a effectivement annulé restitue).
- **Paiement confirmé après l'annulation** (paiement tardif après expiration, ou course) : la commande **n'est pas réactivée** ; le paiement passe **« à rembourser »** (`Paiement.Statut.A_REMBOURSER`, détail dans `metadata.remboursements_dus`), l'administration est alertée (journal `securite` + notification in-app et email de chaque administrateur actif) et le signal `paiement_valide` n'est pas émis (ni confirmation, ni fiche de livraison, ni points de fidélité).
- **Annulation d'une commande payée** (client avant préparation, administration jusqu'à la préparation) : le paiement passe « à rembourser » de la même façon. Un paiement encore en attente dont toutes les commandes sont annulées est annulé.
- Le remboursement lui-même sera traité avec le module paiements.

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
2. **Paiement sans adresse.** `POST /api/paiements/initier/` n'a plus besoin de `adresse_livraison` (champ **déprécié et ignoré** : l'adresse vient du checkout). Nouveau refus **400** « Cette commande n'est plus disponible… » quand la boutique n'est plus disponible (la commande est alors annulée : rafraîchir le panier / les commandes).
3. **Annulation.** `POST /api/commandes/<id>/annuler/` (sans corps) : **200** avec le détail de la commande (`status: "annulee"`, `motif_annulation: "client"`) ; **409** si la commande est en préparation ou au-delà (proposer de contacter le support).
4. **Détail enrichi.** `GET /api/commandes/<id>/` renvoie aussi `articles` et `adresse_livraison` ; la liste et le détail portent `motif_annulation`. `GET /api/commandes/groupes/` renvoie `adresse_livraison`.
5. **Espace vendeur.** `GET /api/commandes/vendeur/`, `GET /api/commandes/vendeur/<id>/`, `POST /api/commandes/vendeur/<id>/preparation/` (réponses au § 3).
6. **Statuts désormais utilisés.** `creee` (en attente de paiement, **annulée automatiquement après 30 minutes**), `confirmee`, `preparation`, `expediee`, `livree`, `annulee` (+ `motif_annulation` : `client`, `expiration`, `administration`, `boutique_indisponible`). Afficher un compte à rebours de paiement sur une commande `creee`.
7. **Livraison.** `GET /api/livraison/…` expose `telephone_contact`. Faire passer une livraison « expédiée » avant que le vendeur ait mis la commande en préparation renvoie **409**.

## 10. Dette connue

- **Niveau 2 — `Idempotency-Key`** : aujourd'hui, un nouvel essai après une coupure réseau renvoie « panier vide » au lieu de la réponse 201 d'origine (aucune double commande possible grâce au verrou). Une clé d'idempotence permettrait de rejouer la même réponse.
- **Remboursements** : le statut « à rembourser » et son détail existent, mais aucun flux de remboursement (à traiter avec le module paiements).
- **Assignation du livreur** : uniquement via le Django admin ; à traiter avec le module livraison.
- **Anonymisation à la suppression de compte** : `Commande.client` en PROTECT empêche de supprimer un compte qui a commandé ; il faudra un flux d'anonymisation.
- **Retours** : le module retours accepte une demande sur une commande `confirmee` (pas encore expédiée) ; à revoir avec ce module maintenant que les statuts `expediee` et `livree` sont réellement posés.
- **Livraison d'une commande annulée** : la fiche de livraison existante reste « en attente » (le livreur ne peut plus l'expédier : 409) ; un statut d'annulation côté livraison est à prévoir.
- Commandes créées avant l'adresse obligatoire (`GroupeCommande` sans adresse) : non payables (400 « Adresse de livraison manquante ») ; elles expirent et libèrent leur stock.

## 11. Tests

⚠️ **Toujours lancer les tests sur PostgreSQL.** Sous SQLite, `ConcurrenceCommandesTests` est **sauté**.

```bash
cd backend-django
DJANGO_SETTINGS_MODULE=config.settings.ci DB_NAME=anitche_test DB_USER=postgres DB_PASSWORD=postgres DB_HOST=localhost \
  python manage.py test apps.commandes apps.paiements apps.livraison -v 2
```

`apps/commandes/tests.py` — 53 tests. Refonte : adresse (obligatoire, complète, téléphone valide, reprise par le paiement, livraison et téléphone de contact, paiement refusé sans adresse), montants (serveur, prix figé avec promo, remise et répartition en francs entiers), annulation client (stock restitué une fois, paiement à rembourser + alerte admin, 409 après préparation, 404 pour un autre client), expiration (30 min, une seule restitution, commandes confirmées épargnées, paiement tardif : commande non réactivée, clé interne de metadata protégée, alerte), machine à états (parcours complet, synchronisation depuis la livraison, pas de saut ni de retour arrière), espace vendeur (isolation, données minimales, accès, boutique suspendue et commandes payées, N+1), boutique indisponible au paiement (commande seule et groupe), annulation par l'administration, détail avec articles, collision de numéro, PROTECT, concurrence (double validation, double annulation).

Postman : `postman_commandes.json` (hors dépôt) — connexions, remise en état (admin), préparation, parcours client (checkout avec adresse, paiement à la livraison), parcours vendeur (préparation), annulation, scénarios de sécurité, nettoyage. Rejouable ; `admin_password` à renseigner. `postman_panier.json` et `postman_paiements.json` envoient désormais l'adresse au checkout.

## 12. Migrations

- **commandes 0005** : adresse de livraison sur `GroupeCommande` (vide pour l'existant), `motif_annulation`, PROTECT sur `client` et `boutique`.
- **paiements 0002** : statut `a_rembourser` ; `adresse_livraison` sans valeur par défaut fictive.

Base de dev avant application : 2 commandes `creee` anciennes (sans adresse), 0 paiement. Elles seront annulées par la tâche d'expiration — le service `celery-beat` de dev doit être redémarré pour charger la nouvelle planification.
