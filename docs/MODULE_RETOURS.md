# Module retours — contrat et règles

> Périmètre : backend Django, `backend-django/apps/retours/` (et ses points de contact dans `paiements`, `livraison`, `fidelite`, `notifications`).
> État : refonte de septembre 2026. **Un retour remboursé rend de l'argent au client et en retire au vendeur** : toute la conception part de là.

## 1. Ce sur quoi le module s'appuie, et qui s'appuie sur lui

| Module | Lien |
|---|---|
| `commandes` | Seule une commande **livrée** du client peut faire l'objet d'un retour ; `CommandeItem` (prix, quantité) est la base des quantités et du montant |
| `livraison` | `Livraison.date_livraison` (remise du colis avec le code du client) fait courir le délai de retour |
| `paiements` | Ouverture → `reversements.suspendre_reversement` ; rejet ou annulation → `reprendre_reversement` ; remboursement → `services.rembourser_retour` (un `Remboursement` à traiter par l'administration, part du vendeur réduite, ou ajustement négatif si déjà versé) ; `reversements.retour_ouvert` considère `rejete`, `annule`, `rembourse`, `cloture` comme clos |
| `fidelite` | Écoute le signal `retour_status_change` (points, voir [`MODULE_FIDELITE.md`](./MODULE_FIDELITE.md)) |
| `notifications` | Vendeur prévenu de chaque demande et des actions du client ; client prévenu des décisions ; administration alertée à chaque rejet |
| `catalogue` | `Stock.incrementer` à la réception du colis |

## 2. Architecture

```text
views.py        HTTP : payloads, codes de réponse, téléchargement des photos
services.py     éligibilité (livrée, délai), montant remboursé, table des transitions
                appliquée sous select_for_update, création, contrôle des photos,
                visibilité par rôle
serializers.py  validation de la demande (quantités déjà couvertes), représentation
signals.py      retour_status_change (après commit) + notifications
admin.py        Django admin en lecture seule sur statut, montant, quantités, photos
```

## 3. Modèles

- **`DemandeRetour`** : `numero_retour` (`RET-<année>-<8 hex>`), `commande`, `client`, `boutique`, `motif`, `type_resolution` (seul `remboursement` est accepté à la création), `statut`, `description`, `montant_remboursement` (figé à la création), `frais_livraison_rembourses` (part des frais de livraison comprise dans ce montant, § 6), `reponse_vendeur`, dates (`date_traitement`, `date_cloture`).
- **`RetourItem`** : ligne de commande retournée et quantité (retour partiel possible).
- **`PhotoRetour`** : preuve du client, fichier renommé en UUID (`retours/preuves/<uuid>.<ext>`).

## 4. Statuts et transitions

| Action | Depuis | Vers | Qui |
|---|---|---|---|
| `approuver` | `demande` | `approuve` | boutique (vendeur propriétaire ou administration) |
| `rejeter` | `demande`, `approuve` | `rejete` (définitif) | boutique, **motif obligatoire** (`reponse`) |
| `en_transit` | `approuve` | `en_transit` | client (« j'ai expédié ») ou boutique |
| `receptionner` | `approuve`, `en_transit` | `receptionne` | boutique ; stock réintégré sauf `"restock": false` |
| `rembourser` | `receptionne` | `rembourse` | boutique ; crée le `Remboursement` à traiter |
| `cloturer` | `rembourse` | `cloture` | boutique |
| `annuler` | `demande`, `approuve` | `annule` (définitif) | client uniquement |

- **Pas de rejet après réception** : un colis reçu se rembourse. Un désaccord (article abîmé par le client, colis vide) passe par l'administration via le support.
- Chaque action est appliquée **sous verrou** (`select_for_update`) : deux requêtes simultanées voient l'une après l'autre le statut à jour, et la seconde est refusée (400).
- Une demande `rejete` ou `annule` ne compte pas dans les quantités déjà retournées : le client peut en refaire une.

## 5. Endpoints — `/api/retours/`

| Méthode | Chemin | Qui | Détail |
|---|---|---|---|
| GET | `` | client | Ses demandes, paginées (20) |
| POST | `` | client | Corps : `commande_id`, `motif`, `type_resolution?` (`remboursement`), `description` (10 caractères min.), `articles` : `[{commande_item_id, quantite}]`. **201** : la demande. **400** : commande pas livrée, délai écoulé, date de livraison inconnue, commande d'un autre client, quantité au-delà de l'achat (demandes existantes et lignes répétées comprises), type de résolution autre que remboursement. **429** : `retour_creation` |
| GET | `<uuid>/` | client, vendeur de la boutique, administration | Détail ; **404** hors périmètre. Un vendeur voit aussi les retours qu'il a faits comme acheteur |
| PATCH | `<uuid>/traiter/` | selon l'action (§ 4) | Corps : `action`, `reponse?` (obligatoire pour `rejeter`), `restock?` (défaut `true`). **200** : la demande. **400** : transition invalide, motif manquant. **403** : action qui ne revient pas à l'appelant. **404** : tiers |
| POST | `<uuid>/photos/` | client de la demande | Multipart `image` (JPEG, PNG, WebP ; 3 Mo ; contenu vérifié). Statut `demande`, `approuve` ou `en_transit` seulement, **5 photos au plus**. **201**, **400**, **404** (autre compte), **429** : `retour_photo` |
| GET | `<uuid>/photos/<photo_uuid>/` | client, vendeur de la boutique, administration | Le fichier (servi par Django après contrôle d'accès). **404** hors périmètre, **401** sans jeton |
| GET | `vendeur/liste/` | vendeur (sa boutique), administration (tout) | Paginée |

Représentation d'une demande : `id`, `numero_retour`, `commande`, `client`, `client_email`, `boutique`, `boutique_nom`, `motif(_display)`, `type_resolution(_display)`, `statut(_display)`, `description`, `montant_remboursement`, `frais_livraison_rembourses`, `reponse_vendeur`, `articles` (`id`, `commande_item`, `nom_produit`, `prix_unitaire`, `quantite`), `photos` (`id`, **`image` = URL de téléchargement authentifiée**, `date_ajout`), dates.

## 6. Montant remboursé

`montant_remboursement` = prix des articles retournés × (montant payé pour les articles de la commande, frais de livraison exclus ÷ montant avant remise), **arrondi au franc inférieur**, + les frais de livraison rendus. Le client récupère ce qu'il a réellement payé : la remise d'un coupon (supportée par ANITCHE) est déduite au prorata. La somme des retours partiels d'une commande ne dépasse jamais ce qu'elle a payé.

**Frais de livraison** ([`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 9) : rendus **en totalité**, une seule fois par commande, pour un motif imputable au vendeur (`article_manquant`, `produit_defectueux`, `non_conforme`) ; jamais pour `changement_avis`, `mauvaise_taille` ou `autre`, ni pour une livraison offerte (le client n'a rien payé). Une demande rejetée ou annulée ne compte pas. Le calcul se fait sous le verrou de la commande : deux demandes simultanées ne les incluent pas toutes les deux. Au remboursement, ils sont facturés au vendeur par un `AjustementVendeur` (`frais_livraison_retour`), déduit de ses prochains reversements.

La part retirée au vendeur ne change pas (`paiements.reversements.part_vendeur_retournee`) : prix des articles moins leur commission, le frais fixe restant à ANITCHE. La remise étant supportée par ANITCHE, le vendeur ne perd que ce qu'il aurait touché.

## 7. Argent, de bout en bout

1. **Demande ouverte** (dans les 7 jours suivant la livraison) : le reversement au vendeur, alors en rétractation, est **suspendu**.
2. **Rejet ou annulation** : le reversement reprend (rétractation ou disponible selon la date), sauf contestation de livraison ouverte.
3. **Remboursement** : un `Remboursement` (motif `retour`, montant de la demande) est créé **une seule fois** par retour et l'administration est alertée ; la part du vendeur est retirée du reversement (avant versement) ou devient un **ajustement négatif** imputé au prochain reversement (après versement). La déduction n'est appliquée que si le `Remboursement` vient d'être créé : un second appel ne retire rien de plus.

Avec le délai de 7 jours (égal à la rétractation), le cas « retour après versement » ne se produit que si `RETOUR_DELAI_JOURS` est augmenté ; le mécanisme reste en place pour ce cas.

## 8. Impact frontend

1. **Bouton « Retourner un article »** : seulement sur une commande `livree`, jusqu'à 7 jours après la livraison (`date_livraison` du suivi de livraison + 7 jours). Au-delà : **400** avec le motif.
2. **`type_resolution`** : ne proposer que « Remboursement » (échange et avoir renvoient **400**).
3. **Montant affiché** : `montant_remboursement` de la réponse (remise déduite), pas le prix catalogue.
4. **Photos** : `photos[].image` est désormais une **URL d'API authentifiée** (avant : chemin `/media/…`). La charger avec le jeton (`Authorization: Bearer`), par exemple en `fetch` puis `URL.createObjectURL`, et non dans un `<img src>` nu. 5 photos au plus, uniquement avant la réception.
5. **Client** : nouveaux boutons « J'ai expédié le colis » (`action: en_transit`, statut `approuve`) et « Annuler ma demande » (`action: annuler`, statuts `demande` ou `approuve`). Nouveau statut **`annule`** (libellé « Annulée par le client »).
6. **Vendeur** : le rejet exige un motif (`reponse`) ; plus de bouton « Rejeter » après réception ; la réception est possible directement depuis `approuve`. **403** pour une action du client, **400** pour une transition invalide.
7. **`cloturer`** n'existe plus après un rejet (le rejet est définitif).
8. **Erreurs à prévoir** : **429** au-delà de 10 demandes ou 30 photos par heure.
9. **Format d'erreur unifié (septembre 2026).** Les refus portaient avant un simple `{"detail": …}` (et `{"image": "…"}` pour une photo manquante) : ils suivent désormais le format commun `{success: false, status_code, detail, errors}`, `errors` toujours un objet clé → liste de messages (`{}` si aucun champ n'est en cause) ; photo manquante → **400** `errors.image` (liste).

## 9. Sécurité — failles corrigées (diagnostic de septembre 2026)

Chaque constat a été confirmé par un test jetable sur PostgreSQL avant correction ; chacun a son test permanent dans `apps/retours/tests.py`.

| # | Faille | Correction | Test |
|---|---|---|---|
| R-a | Retour accepté sur une commande confirmée ou expédiée | Commande livrée exigée | `EligibiliteTests` |
| R-b | Aucun délai (retour un an après) | 7 jours après la livraison (`RETOUR_DELAI_JOURS`) | `EligibiliteTests` |
| R-c | Deux « rembourser » simultanés : deux 200, part du vendeur déduite deux fois | Transitions sous verrou ; déduction liée à la création du `Remboursement` | `RemboursementUniqueTests`, `RemboursementConcurrentTests` |
| R-d | Rejet après réception : stock réintégré, client sans rien | Transition supprimée | `TransitionsTests` |
| R-e | Remboursement au prix catalogue, remise ignorée (40 000 rendus pour 30 000 payés) | Prorata du montant payé, franc inférieur | `MontantRembourseTests` |
| R-h | Photo sur un retour clos, nom d'origine du fichier conservé, aucune limite, `/media/` public en dev et absent en production | Statuts ouverts, 5 photos, UUID, téléchargement authentifié | `PhotosTests` |
| R-i | Vendeur jamais prévenu d'une demande | Notification après commit | `NotificationEtVisibiliteTests` |
| R-j | Un vendeur ne voyait pas ses propres retours d'acheteur | Visibilité client **ou** boutique | `NotificationEtVisibiliteTests` |
| R-m | Pas de limite dédiée | `retour_creation` 10/h, `retour_photo` 30/h | `LimitesDeDebitTests` |
| — | Une même ligne citée deux fois dans une demande dépassait la quantité achetée (trouvé pendant la refonte) | Quantités cumulées dans la demande | `EligibiliteTests` |

Vérifié et correct dès le diagnostic : IDOR sur la liste et le détail, validation du contenu réel des images, quantités déjà couvertes et concurrence à la création (verrou sur la commande).

## 10. Limites de débit et performance

- `retour_creation` : 10/heure par compte (création seulement ; la liste garde le taux général). `retour_photo` : 30/heure. Relevées à 1 000/heure en dev (`dev.py`) pour Postman.
- Listes paginées (20), `select_related` + `prefetch_related` (articles, photos) : nombre de requêtes constant.
- Ordre des verrous : commande (création), demande puis stock et reversement (traitement).

## 11. Migrations

- **retours 0003** : statut `annule` ; `PhotoRetour.image` renommée en UUID (`CheminUploadUUID`). Les fichiers existants gardent leur nom.
- **retours 0004** : `frais_livraison_rembourses` (0 pour l'existant).

En dev : `docker exec anitche-backend python manage.py migrate`.

## 12. Tests

⚠️ **Toujours sur PostgreSQL** (`config.settings.ci`), en vérifiant « ok » et non « skipped » pour les tests de concurrence.

`apps/retours/tests.py` : parcours historiques (adaptés : livraison datée), concurrence à la création, admin en lecture seule, puis un test par faille (§ 9). Tests adaptés hors module : `paiements` (`ReversementTests.livrer` date la livraison ; le scénario « retour sur un reversement déjà disponible » tourne avec `RETOUR_DELAI_JOURS=30` et un rejet motivé).

Postman : `postman_retours.json` (hors dépôt, nouvelle : aucune collection retours n'existait). Mise en place reprise de `postman_livraison.json` (comptes, produit, commande payée, livraison avec le code lu dans Mailpit). Parcours : retour refusé sur une commande confirmée → demande → notification du vendeur → reversement suspendu → photo (URL authentifiée, 404 pour un tiers, 401 sans jeton) → approbation → « j'ai expédié » → réception → remboursement (le second refusé) → remboursement à traiter côté administration → clôture → seconde demande annulée par le client. `admin_password` à renseigner.

## 13. Dette connue

- **Frais de retour** (qui paie le renvoi du colis) : toujours non modélisé. Les frais de livraison **payés à l'aller** sont traités (§ 6, [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 9).
- **Arbitrage** : aucun statut « litige » ; un désaccord passe par un ticket support lié à la commande (module support).
- **Échange et avoir** : valeurs conservées en base pour l'historique, non proposées. À concevoir si le besoin apparaît (réservation de stock, bon d'achat).
- **Colis reçu abîmé par le client** : la boutique peut réceptionner avec `"restock": false` ; le remboursement reste dû, l'administration peut ensuite trancher hors application.

## 14. Justification des choix

### Pourquoi une commande livrée seulement

Avant la livraison, la bonne voie est l'**annulation** de la commande (stock restitué, paiement remboursé, reversement annulé en une transaction). Un « retour » d'un colis jamais remis ouvrait un circuit parallèle, sans colis à renvoyer, et suspendait un reversement qui n'était même pas encore ouvert.

### Pourquoi 7 jours, réglables

- C'est le délai de rétractation déjà utilisé pour le reversement et la contestation « non reçu » : un seul délai à expliquer aux clients et aux vendeurs.
- Pendant ces 7 jours l'argent du vendeur n'est pas encore versé : la suspension suffit, sans ajustement négatif à recouvrer.
- `RETOUR_DELAI_JOURS` permet de l'allonger sans déploiement (par exemple pour les produits défectueux) : le mécanisme d'ajustement négatif couvre alors les retours après versement.

### Pourquoi pas de rejet après réception

Réceptionner, c'est reconnaître que le colis est revenu : le stock est réintégré. Un rejet ensuite laissait le vendeur avec l'article **et** l'argent. Le désaccord (article abîmé, incomplet) doit être arbitré par ANITCHE, pas tranché seul par la partie qui y gagne.

### Pourquoi le vendeur décide, avec l'administration en recours

Le vendeur connaît son produit et reçoit le colis : lui faire approuver, réceptionner et rembourser est le plus simple au lancement. Les garde-fous : motif obligatoire au rejet, **alerte de l'administration à chaque rejet**, l'administration peut agir sur toute demande, et l'argent n'est rendu qu'après traitement du `Remboursement` par l'administration.

### Pourquoi le prorata du montant payé

Rembourser au prix catalogue rendait plus que ce que le client avait payé quand un coupon avait servi. Le prorata garantit que la somme des remboursements d'une commande ne dépasse jamais son montant payé ; l'arrondi au franc inférieur évite qu'une accumulation d'arrondis dépasse ce montant.

### Pourquoi une URL de téléchargement authentifiée pour les photos

`/media/` n'est pas servi en production (les photos étaient donc invisibles pour le vendeur et l'administration) et l'est **sans contrôle** en développement. Les photos de litige peuvent montrer un domicile, un visage, un document : même traitement que les pièces KYC, servies par Django après contrôle d'accès. Le nom d'origine est remplacé par un UUID : il contenait parfois le nom du client.

### Pourquoi un statut `annule` distinct de `cloture`

`cloture` suit un remboursement ; `annule` signifie qu'aucun retour n'a eu lieu. Les distinguer permet de ne compter que les vrais retours dans les quantités déjà retournées, et de reprendre le reversement au bon moment.
