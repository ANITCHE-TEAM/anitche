# Module fidélité — contrat et règles

> Périmètre : backend Django, `backend-django/apps/fidelite/` (et ses points de contact dans `commandes`, `livraison`, `retours`, `paiements`).
> Principe : **un point est une promesse de réduction, donc de l'argent** : il ne doit naître que d'un achat définitivement acquis.

## 1. Ce sur quoi le module s'appuie, et qui s'appuie sur lui

| Module | Lien |
|---|---|
| `commandes` | `synchroniser_depuis_livraison` (commande livrée) → `fidelite.services.ouvrir_gain` ; `annuler_commande` → `restituer_coupon` ; `ValiderPanierView` verrouille le coupon puis `consommer_coupon` |
| `livraison` | Contestation « non reçu » ouverte : le crédit attend ; fondée → `annuler_gain` |
| `retours` | Signal `retour_status_change` (`rembourse`) → `appliquer_retour_rembourse` ; un retour ouvert retarde le crédit |
| `paiements` | Plus aucun écouteur fidélité sur `paiement_valide` ; même délai que le reversement (`REVERSEMENT_DELAI_RETRACTATION_JOURS`) |
| `notifications` | Client prévenu quand ses points sont crédités |

## 2. Architecture

```text
services.py     points_pour, gains (ouverture, crédit à échéance, annulation, retours),
                reprise plafonnée, coupons (consommation, restitution)
tasks.py        crediter_points_echus (Celery beat, toutes les heures à :15)
signals.py      retour remboursé → ajustement des points
models.py       CompteFidelite (crediter/debiter sous verrou), TransactionFidelite (journal),
                GainFidelite, CouponReduction, UtilisationCoupon
views.py        compte, gains, journal, coupons, conversion, vérification
admin.py        journal, gains et utilisations en lecture seule ; solde non modifiable
```

## 3. Cycle des points

```text
commande livrée ──► gain « en attente » (fin = livraison + 7 jours)
    │   retour remboursé pendant l'attente → points recalculés sur ce qui reste payé (annulé si 0)
    │   contestation « non reçu » fondée    → gain annulé
    ▼   délai écoulé, aucun retour ni contestation ouverts (tâche périodique)
gain crédité ──► retour remboursé après le crédit → reprise plafonnée au solde (une fois par retour)
```

- **1 point par tranche entière de 1 000 FCFA** du montant payé pour les articles de la commande (remise déduite), par commande. **Jamais sur les frais de livraison** ([`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 9) : ni au gain, ni au recalcul après un retour (les frais rendus au client ne comptent pas comme des articles remboursés), ni à la reprise de points déjà crédités.
- Une commande annulée (client, expiration, administration, boutique indisponible, livraison échouée) n'est jamais livrée : elle **n'a jamais de gain**.
- **Crédit** : même moment que le reversement au vendeur devenu disponible (délai écoulé, aucun retour ni contestation ouverts), tâche `fidelite-crediter-points-echus` à la minute 15 de chaque heure (celle des reversements tourne à la minute 10).
- **Idempotence** : un gain par commande (`OneToOne`), une transaction par (compte, type, référence) en base (gain : numéro de commande ; reprise : numéro de retour ; conversion : code du coupon). Le crédit se fait sous verrou du gain.
- **Commandes dont les points ont déjà été crédités au paiement** (données antérieures au gain à la livraison) : à la livraison, aucun second gain n'est ouvert (transaction de gain déjà présente pour le paiement).

## 4. Coupons

- **Nominatif** (converti depuis des points) : `utilisations_max` = 1, réservé à son client.
- **Public** (code promo créé dans le Django admin) : `utilisations_max` vide = illimité, ou un plafond global ; **chaque client une seule fois** (`UtilisationCoupon`, unique par coupon et client).
- **Consommation** au checkout, sous le verrou du coupon posé par `ValiderPanierView` : une `UtilisationCoupon` (liée au checkout) et le compteur ; `est_utilise` passe à vrai quand la limite globale est atteinte (« épuisé »).
- **Restitution** : quand **toutes** les commandes du checkout sont annulées et que le coupon n'a pas expiré, l'utilisation est effacée et le coupon redevient utilisable. Une commande restante garde le coupon consommé (la remise lui a été répartie).
- **Valeurs** (contraintes en base) : valeur > 0, pourcentage ≤ 100, minimum d'achat ≥ 0. La remise n'excède jamais le montant, arrondie au franc inférieur ; elle est supportée par ANITCHE.

## 5. Endpoints — `/api/fidelite/`

| Méthode | Chemin | Détail |
|---|---|---|
| GET | `mon-compte/` | `solde_points`, **`points_en_attente`** (nouveau), `points_cumules_total`, `palier`… |
| GET | `points-en-attente/` | **Nouveau.** Gains par commande : `commande`, `numero_commande`, `points`, `statut` (`en_attente`, `credite`, `annule`), `date_disponibilite`, `motif_annulation`, dates. Paginé |
| GET | `transactions/` | Journal (types : `gain`, `depense`, `reprise` (nouveau), `ajustement`…). Paginé |
| GET | `mes-coupons/` | Coupons nominatifs du client |
| POST | `convertir-points/` | `option` (`50_PTS_5PCT`, `100_PTS_10PCT`, `250_PTS_2500FCFA`, `500_PTS_6000FCFA`). **201** : coupon (90 jours). **400** : solde insuffisant, option inconnue. **429** : `fidelite_conversion` 20/h |
| POST | `verifier-coupon/` | `code`, `montant_commande`. **200** : remise. **400** : désactivé, expiré, déjà utilisé (par vous ou épuisé), minimum non atteint. **404** : code inexistant **ou nominatif d'un autre client** (même réponse). **429** : `coupon_verification` 30/h |

Toutes les vues filtrent par `request.user` (IDOR vérifié).

## 6. Impact frontend

1. **Plus de points au paiement** : les afficher « en attente » après la livraison (`points_en_attente` dans `mon-compte/`, détail par commande dans `points-en-attente/` avec `date_disponibilite`), puis crédités 7 jours après la livraison. Supprimer tout message « vous avez gagné X points » au paiement.
2. **Statuts de gain** : `en_attente` (date de disponibilité), `credite`, `annule` (`motif_annulation` : retour remboursé, colis non reçu).
3. **Nouveau type de transaction** `reprise` (« Reprise (achat remboursé) »).
4. **`verifier-coupon/`** : un coupon nominatif d'un autre client renvoie **404** « n'existe pas » (pas de 400 « nominatif »). Nouveau message 400 « Vous avez déjà utilisé ce coupon ».
5. **Coupon rendu** : après l'annulation de tout un checkout, le coupon réapparaît utilisable dans `mes-coupons/` (`est_utilise: false`).
6. **Format d'erreur unifié (septembre 2026).** `verifier-coupon/` ne renvoie plus `{"valide": false, "detail": …}` en cas de refus : code inexistant (ou nominatif d'un autre client) → **404** au format commun `{success: false, status_code, detail, errors}`, `errors` toujours un objet clé → liste de messages (`{}` si aucun champ n'est en cause), message dans `detail` ; coupon inapplicable (déjà utilisé, montant minimum, expiré…) → **400** avec le motif dans `errors.code`. La réponse **200** garde `valide: true`.

## 7. Sécurité — risques couverts

Chaque risque a son test permanent dans `apps/fidelite/tests.py`.

| Risque | Protection | Test |
|---|---|---|
| Points crédités au paiement et gardés après annulation remboursée (idem administration, contestation fondée) → points fabriqués, convertibles en coupons | Points « en attente » à la livraison, crédités après le délai de rétractation ; annulés si contestation fondée | `PointsEnAttenteTests` |
| Reprise sur retour non idempotente (signal rejoué) | Recalcul pendant l'attente ; reprise unique par retour, contrainte d'unicité | `RetourEtPointsTests` |
| Coupon perdu à l'annulation ou à l'expiration de la commande | Restitution si tout le checkout est annulé | `CouponsTests` |
| Coupon public fermé à tous après le premier usage | Limite globale + une utilisation par client | `CouponsTests` |
| Coupon à 150 % ou négatif accepté | Contraintes en base, remise plafonnée au montant | `CouponsTests` |
| Solde modifiable dans l'admin sans trace, journal falsifiable | Admin en lecture seule | `AdminFideliteTests` |
| `verifier-coupon/` révélant les coupons nominatifs d'autrui | Réponse identique à un code inexistant | `CouponsTests` |

« Points sur tout le groupe, commandes annulées comprises » (module paiements) : sans objet, les points sont ouverts par commande livrée.

## 8. Migrations

- **fidelite 0002** : `GainFidelite`, `UtilisationCoupon`, `CouponReduction.utilisations_max` / `nombre_utilisations`, type `reprise`, contraintes (valeurs de coupon, unicité des transactions par référence, une utilisation par client).
- **fidelite 0003** (données) : coupons nominatifs → 1 utilisation ; coupons publics → illimités et **rouverts**, avec une `UtilisationCoupon` pour chaque client qui l'avait déjà utilisé (commandes portant son code).

En dev : `docker exec anitche-backend python manage.py migrate`, puis redémarrer **worker et beat** Celery (nouvelle tâche planifiée).

## 9. Tests

`apps/fidelite/tests.py` (PostgreSQL) : parcours historiques (compte, paliers, conversion, vérification, limites de débit), puis un test par faille (§ 7). Tests adaptés hors module : `paiements` (plus de points au paiement, 4 assertions).

Postman : `postman_fidelite.json` (hors dépôt, nouvelle). Parcours : état initial → commande payée (aucun point) → livraison avec le code lu dans Mailpit → gain « en attente » visible (et invisible pour un autre client) → commande payée puis annulée (aucun gain) → coupons (option inconnue, conversion si solde ≥ 50, code inexistant 404, coupon nominatif d'autrui 404). Le crédit après 7 jours est couvert par les tests automatisés. `admin_password` à renseigner.

## 10. Dette connue

- **Expiration des points** : le type `expiration` existe, aucune règle n'est définie (décision produit).
- **Création de codes promo** : Django admin uniquement (pas d'API d'administration).
- **Reprise plafonnée** : des points déjà convertis en coupon avant un retour remboursé après crédit restent acquis (perte assumée, journalisée) ; ne se produit que si `RETOUR_DELAI_JOURS` dépasse le délai de rétractation.

## 11. Justification des choix

### Pourquoi à la fin du délai de rétractation (et pas au paiement ni à la livraison)

- **Au paiement** : la commande peut encore être annulée et remboursée ; il fallait reprendre les points à chaque annulation, dans chaque module qui annule (client, expiration, administration, livraison échouée), avec le risque d'en oublier un.
- **À la livraison** : un retour ou une contestation « non reçu » restent possibles 7 jours ; il aurait fallu reprendre des points peut-être déjà dépensés.
- **À la fin du délai** : l'achat est acquis, exactement quand l'argent l'est pour le vendeur. Les annulations n'ont rien à reprendre, un retour ou une contestation pendant le délai ajustent un gain encore en attente. La reprise sur le solde ne sert plus que dans le cas rare d'un remboursement après le crédit.
- Le client voit ses points « en attente » dès la livraison : il n'a pas l'impression d'avoir été oublié.

### Pourquoi recalculer (et non annuler en bloc) pour un retour partiel

Retourner un article sur cinq n'annule pas l'achat des quatre autres : le gain est recalculé sur ce qui reste payé ; un retour total l'annule. Le calcul repart du montant payé moins tous les retours remboursés : rejouer le signal donne le même résultat.

### Pourquoi une table d'utilisations de coupon

Un simple booléen `est_utilise` ne sait pas dire *qui* a utilisé un code public : soit il est fermé à tous après le premier usage (défaut constaté), soit rien n'empêche un client de l'utiliser dix fois. Une ligne par (coupon, client), unique en base, règle les deux, et permet de rendre le coupon au bon client quand son checkout est annulé.

### Pourquoi les contraintes en base

Les coupons publics sont créés à la main dans le Django admin : une faute de frappe (150 au lieu de 15 %) donnait une remise supérieure au panier. La contrainte refuse la saisie quel que soit le chemin (admin, shell, migration).
