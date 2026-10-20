# Module retours — contrat et règles

> Périmètre : backend Django, `backend-django/apps/retours/` (et ses points de contact dans `paiements`, `livraison`, `fidelite`, `notifications`).
> Principe : **un retour remboursé rend de l'argent au client et en retire au vendeur** : toute la conception part de là.

## 1. Ce sur quoi le module s'appuie, et qui s'appuie sur lui

| Module | Lien |
|---|---|
| `commandes` | Seule une commande **livrée** du client peut faire l'objet d'un retour ; `CommandeItem` (prix, quantité) est la base des quantités et du montant |
| `livraison` | `Livraison.date_livraison` (remise du colis avec le code du client) fait courir le délai de retour |
| `paiements` | Ouverture → `reversements.suspendre_reversement` ; décision définitive sans remboursement (`rejete`, `annule`) → `reprendre_reversement` ; remboursement → `services.rembourser_retour` (un `Remboursement` à traiter par l'administration, part du vendeur réduite, ou ajustement négatif si déjà versé ou en cours de transfert) ; `reversements.retour_ouvert` considère `rejete`, `annule`, `rembourse`, `cloture` comme clos : un refus encore contestable garde le reversement suspendu |
| `fidelite` | Écoute le signal `retour_status_change` (points, voir [`MODULE_FIDELITE.md`](./MODULE_FIDELITE.md)) |
| `notifications` | Vendeur prévenu de chaque demande (avec son échéance), rappelé 24 h avant ; client prévenu de chaque décision et de son délai de contestation ; modérateurs actifs prévenus d'un dossier en modération ; administration prévenue d'un dossier en arbitrage, d'un dossier sans décision depuis 48 h et d'un remboursement impossible. Lien `lien_retour_moderation` → `/moderation/retours/<uuid>` |
| `catalogue` | `Stock.incrementer` à la réception du colis |

## 2. Architecture

```text
views.py        HTTP : payloads, codes de réponse, téléchargement des photos
services.py     éligibilité (livrée, délai), montant remboursé, table des transitions
                (action, statut de départ, rôle, contrôle) appliquée sous
                select_for_update, historique, échéances, actions possibles,
                création, contrôle des photos, visibilité par rôle
tasks.py        appliquer_echeances_retours (Celery beat, toutes les 15 min)
serializers.py  validation de la demande (quantités déjà couvertes), représentation,
                historique
signals.py      retour_status_change (après commit) + notifications
admin.py        Django admin en lecture seule (demandes, historique)
```

## 3. Modèles

- **`DemandeRetour`** : `numero_retour` (`RET-<année>-<8 hex>`), `commande`, `client`, `boutique`, `motif`, `type_resolution` (seul `remboursement` est accepté à la création), `statut`, `description`, `montant_remboursement` (figé à la création), `frais_livraison_rembourses` (part des frais de livraison comprise dans ce montant, § 6), `reponse_vendeur`, dates (`date_traitement`, `date_cloture`), échéances (`date_limite_vendeur`, `date_limite_contestation`), entrée en recours (`date_moderation`, `date_arbitrage`), `date_relance` (dernier rappel ou alerte).
  Contraintes en base : une demande `demande` a toujours une `date_limite_vendeur`, un refus contestable une `date_limite_contestation` ; `montant_remboursement` est un nombre entier de FCFA, jamais négatif.
- **`RetourItem`** : ligne de commande retournée et quantité (retour partiel possible).
- **`PhotoRetour`** : preuve du client, fichier renommé en UUID (`retours/preuves/<uuid>.<ext>`).
- **`RetourHistorique`** : une ligne par étape (création comprise) : `ancien_statut`, `nouveau_statut`, `action`, `source` (`client`, `vendeur`, `moderation`, `administration`, `systeme`), `auteur` (vide pour le système), `commentaire` (motif), `created_at`. Écrite dans la transaction de la transition : une étape annulée ne laisse aucune trace.

## 4. Statuts et transitions

Le vendeur décide ; le client peut contester un refus devant la modération, puis devant l'administration, qui tranche en dernier recours.

```text
demande ─approuver (vendeur)──────────────────────────► approuve ─► en_transit ─► receptionne ─► rembourse ─► cloture
   │ └─rejeter (vendeur)─► refuse_vendeur ─contester─┐      ▲
   └─escalader (système, vendeur muet 48 h)──────────┴► en_moderation ─approuver (modérateur)─┤
                                                         └─rejeter─► refuse_moderation ─contester─► en_arbitrage ─approuver (admin)─┘
                                                                                                      └─rejeter (admin)─► rejete
refuse_vendeur, refuse_moderation ─clore_refus (système, 72 h sans contestation)─► rejete
```

| Action | Depuis | Vers | Qui |
|---|---|---|---|
| `approuver` | `demande` | `approuve` | vendeur, avant `date_limite_vendeur` |
| | `en_moderation` | `approuve` | modérateur, **motif obligatoire** |
| | `en_arbitrage` | `approuve` | administration, **motif obligatoire** |
| `rejeter` | `demande` | `refuse_vendeur` | vendeur, avant son échéance, **motif obligatoire** (`reponse`) |
| | `approuve` | `refuse_vendeur` | vendeur, seulement s'il a lui-même approuvé (pas une décision de recours) |
| | `en_moderation` | `refuse_moderation` | modérateur, motif obligatoire |
| | `en_arbitrage` | `rejete` (définitif) | administration, motif obligatoire |
| `contester` | `refuse_vendeur` | `en_moderation` | client, avant `date_limite_contestation`, motif de 10 caractères au moins |
| | `refuse_moderation` | `en_arbitrage` | client, mêmes conditions |
| `en_transit` | `approuve` | `en_transit` | client (« j'ai expédié ») ou boutique |
| `receptionner` | `approuve`, `en_transit` | `receptionne` | boutique ; stock réintégré sauf `"restock": false` |
| `rembourser` | `receptionne` | `rembourse` | boutique ; crée le `Remboursement` à traiter (§ 7) |
| `cloturer` | `rembourse` | `cloture` | boutique |
| `annuler` | `demande`, `approuve`, `refuse_vendeur`, `en_moderation`, `refuse_moderation`, `en_arbitrage` | `annule` (définitif) | client uniquement |
| `escalader` | `demande` | `en_moderation` | système, `date_limite_vendeur` passée |
| `clore_refus` | `refuse_vendeur`, `refuse_moderation` | `rejete` | système, `date_limite_contestation` passée |

« Boutique » : le vendeur propriétaire ou l'administration. Le modérateur n'a aucun pouvoir sur le colis ni l'argent ; l'administration ne décide qu'en arbitrage.

- **Échéances** : `RETOUR_DELAI_REPONSE_VENDEUR_HEURES` (48) depuis la création, `RETOUR_DELAI_CONTESTATION_HEURES` (72) depuis le refus, en heures calendaires. La tâche `appliquer_echeances_retours` (toutes les 15 min) escalade, rend les refus définitifs, rappelle le vendeur 24 h avant son échéance et alerte l'administration d'un dossier en modération ou en arbitrage depuis 48 h. Chaque demande est revérifiée sous son verrou (`skip_locked` : une demande en cours de traitement est laissée au passage suivant), et l'échec d'une demande n'empêche pas les suivantes.
- **Pas de rejet après réception** : un colis reçu se rembourse.
- **Codes** : **404** pour un tiers ; **403** pour une action qui ne revient jamais à l'appelant, ou qui revient à un autre à ce statut ; **400** pour une transition invalide, une échéance passée, un motif manquant ou un remboursement impossible (§ 7).
- Chaque action est appliquée **sous verrou** (`select_for_update`) : deux requêtes simultanées, ou une requête et la tâche, voient l'une après l'autre le statut à jour, et la seconde est refusée. Chaque étape est tracée dans `RetourHistorique` ; le signal part après le commit.
- Une demande `rejete` ou `annule` ne compte pas dans les quantités déjà retournées : le client peut en refaire une.

## 5. Endpoints — `/api/retours/`

| Méthode | Chemin | Qui | Détail |
|---|---|---|---|
| GET | `` | client | Ses demandes, paginées (20) |
| POST | `` | client | Corps : `commande_id`, `motif`, `type_resolution?` (`remboursement`), `description` (10 caractères min.), `articles` : `[{commande_item_id, quantite}]`. **201** : la demande. **400** : commande pas livrée, délai écoulé, date de livraison inconnue, commande d'un autre client, quantité au-delà de l'achat (demandes existantes et lignes répétées comprises), type de résolution autre que remboursement. **429** : `retour_creation` |
| GET | `<uuid>/` | client, vendeur de la boutique, administration | Détail ; **404** hors périmètre. Un vendeur voit aussi les retours qu'il a faits comme acheteur |
| PATCH | `<uuid>/traiter/` | selon l'action (§ 4) | Corps : `action` (actions du tableau du § 4 hors `escalader` et `clore_refus`), `reponse?` (motif : obligatoire pour `rejeter`, `contester` et toute décision de recours), `restock?` (défaut `true`). **200** : la demande. **400**, **403**, **404** : § 4 |
| GET | `<uuid>/historique/` | parties de la demande, modérateur d'un dossier passé en modération, administration | Étapes, de la plus ancienne à la plus récente : `id`, `ancien_statut`, `nouveau_statut`, `action`, `source`, `commentaire`, `created_at` ; `auteur` (id du compte) pour l'administration seulement. **404** hors périmètre |
| GET | `moderation/liste/?statut=` | modérateur, administration | Demandes passées par la modération, toutes boutiques : `en_moderation`, puis `en_arbitrage`, puis les autres, les plus anciennes en tête. Paginée. **400** statut inconnu, **403** autres rôles |
| POST | `<uuid>/photos/` | client de la demande | Multipart `image` (JPEG, PNG, WebP ; 3 Mo ; contenu vérifié). Avant la réception (y compris pendant un refus contestable et un recours), **5 photos au plus**. **201**, **400**, **404** (autre compte), **429** : `retour_photo` |
| GET | `<uuid>/photos/<photo_uuid>/` | client, vendeur de la boutique, modérateur (dossier en modération), administration | Le fichier (servi par Django après contrôle d'accès). **404** hors périmètre, **401** sans jeton |
| GET | `vendeur/liste/` | vendeur (sa boutique), administration (tout) | Paginée |

Représentation d'une demande : `id`, `numero_retour`, `commande`, `client`, `client_email` (**`null` pour un modérateur**), `boutique`, `boutique_nom`, `motif(_display)`, `type_resolution(_display)`, `statut(_display)`, `description`, `montant_remboursement`, `frais_livraison_rembourses`, `reponse_vendeur`, `date_limite_vendeur`, `date_limite_contestation`, **`actions_possibles`** (actions que l'appelant peut faire maintenant, calculées par la même table que `traiter`), `articles` (`id`, `commande_item`, `nom_produit`, `prix_unitaire`, `quantite`), `photos` (`id`, **`image` = URL de téléchargement authentifiée**, `date_ajout`), dates. Les listes calculent `actions_possibles` sans requête par demande.

## 6. Montant remboursé

`montant_remboursement` = prix des articles retournés × (montant payé pour les articles de la commande, frais de livraison exclus ÷ montant avant remise), **arrondi au franc inférieur**, + les frais de livraison rendus. Le client récupère ce qu'il a réellement payé : la remise d'un coupon (supportée par ANITCHE) est déduite au prorata. La somme des retours partiels d'une commande ne dépasse jamais ce qu'elle a payé.

**Frais de livraison** ([`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 9) : rendus **en totalité**, une seule fois par commande, pour un motif imputable au vendeur (`article_manquant`, `produit_defectueux`, `non_conforme`) ; jamais pour `changement_avis`, `mauvaise_taille` ou `autre`, ni pour une livraison offerte (le client n'a rien payé). Une demande rejetée ou annulée ne compte pas. Le calcul se fait sous le verrou de la commande : deux demandes simultanées ne les incluent pas toutes les deux. Au remboursement, ils sont facturés au vendeur par un `AjustementVendeur` (`frais_livraison_retour`), déduit de ses prochains reversements.

La part retirée au vendeur ne change pas (`paiements.reversements.part_vendeur_retournee`) : prix des articles moins leur commission, le frais fixe restant à ANITCHE. La remise étant supportée par ANITCHE, le vendeur ne perd que ce qu'il aurait touché.

## 7. Argent, de bout en bout

1. **Demande ouverte** (dans les 7 jours suivant la livraison) : le reversement au vendeur, alors en rétractation, est **suspendu**.
2. **Refus contestable et recours** (`refuse_vendeur`, `en_moderation`, `refuse_moderation`, `en_arbitrage`) : le reversement **reste suspendu**.
3. **Décision définitive sans remboursement** (`rejete`, par l'administration ou à la fin du délai de contestation ; `annule`) : le reversement reprend (rétractation ou disponible selon la date), sauf contestation de livraison ouverte.
4. **Remboursement** : un `Remboursement` (motif `retour`, montant de la demande, inchangé par le circuit de décision) est créé **une seule fois** par retour et l'administration est alertée ; la part du vendeur est retirée du reversement (avant versement) ou devient un **ajustement négatif** imputé une seule fois à un reversement suivant (reversement déjà versé, ou en cours de transfert : le montant d'un transfert lancé ne change pas ; si ce transfert échoue, l'ajustement est imputé à sa nouvelle tentative). La déduction n'est appliquée que si le `Remboursement` vient d'être créé : un second appel ne retire rien de plus.
5. **Remboursement impossible** → **400**, l'étape entière est annulée (statut, historique, part du vendeur), journal `securite` et alerte de l'administration (« Retour … non remboursable ») :
   - aucun paiement encaissé pour la commande ;
   - montant nul ou non entier ;
   - **plafond dépassé** : les remboursements non refusés d'un paiement pour une commande (retours, livraison non reçue, annulation) ne dépassent jamais son `montant_total` (articles et livraison). Voir [`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md).
6. **Traitement par l'administration** : un remboursement de retour se marque `effectue` (référence obligatoire) ; il **ne se refuse pas** (400) : la décision est prise dans le circuit du retour et la part du vendeur est déjà reprise. Un échec technique se règle par une nouvelle tentative chez le fournisseur ou un versement manuel tracé (référence et commentaire).

Avec le délai de 7 jours (égal à la rétractation), le cas « retour après versement » ne se produit que si `RETOUR_DELAI_JOURS` est augmenté ; le mécanisme reste en place pour ce cas.

## 8. Impact frontend

1. **Bouton « Retourner un article »** : seulement sur une commande `livree`, jusqu'à 7 jours après la livraison (`date_livraison` du suivi de livraison + 7 jours). Au-delà : **400** avec le motif.
2. **`type_resolution`** : ne proposer que « Remboursement » (échange et avoir renvoient **400**).
3. **Montant affiché** : `montant_remboursement` de la réponse (remise déduite), pas le prix catalogue.
4. **Photos** : `photos[].image` est une **URL d'API authentifiée** (pas un chemin `/media/…`). La charger avec le jeton (`Authorization: Bearer`), par exemple en `fetch` puis `URL.createObjectURL`, et non dans un `<img src>` nu. 5 photos au plus, uniquement avant la réception.
5. **Boutons d'action** : n'afficher que ceux de **`actions_possibles`** (même règle que le serveur, échéances comprises), au lieu de les déduire du statut.
6. **Client** : « J'ai expédié le colis » (`en_transit`), « Annuler ma demande » (`annuler`, avant l'expédition, recours compris), « Contester » (`contester`, motif de 10 caractères au moins) sur `refuse_vendeur` et `refuse_moderation`, avec le compte à rebours de `date_limite_contestation`.
7. **Vendeur** : le refus exige un motif (`reponse`) et n'est pas définitif ; afficher `date_limite_vendeur` sur une demande : passée, la demande part en modération et le vendeur ne peut plus décider.
8. **Statuts** : `refuse_vendeur`, `en_moderation`, `refuse_moderation`, `en_arbitrage` (en cours) ; `rejete` et `annule` (définitifs). Suivi : `GET <uuid>/historique/`.
9. **Espace modération** : `GET moderation/liste/` et détail `/moderation/retours/<uuid>` (lien des notifications) ; pas d'e-mail du client (`client_email` à `null`).
10. **`cloturer`** n'est pas possible après un rejet (le rejet est définitif).
11. **Erreurs à prévoir** : **429** au-delà de 10 demandes ou 30 photos par heure ; **400** au remboursement quand il est impossible (§ 7).
12. **Format d'erreur unifié.** Les refus suivent le format commun `{success: false, status_code, detail, errors}`, `errors` toujours un objet clé → liste de messages (`{}` si aucun champ n'est en cause) ; photo manquante → **400** `errors.image` (liste).

## 9. Sécurité — risques couverts

Chaque risque a son test permanent dans `apps/retours/tests.py` ou `apps/retours/test_circuit.py`.

| Risque | Protection | Test |
|---|---|---|
| Retour accepté sur une commande confirmée ou expédiée | Commande livrée exigée | `EligibiliteTests` |
| Aucun délai (retour un an après) | 7 jours après la livraison (`RETOUR_DELAI_JOURS`) | `EligibiliteTests` |
| Deux « rembourser » simultanés : deux 200, part du vendeur déduite deux fois | Transitions sous verrou ; déduction liée à la création du `Remboursement` | `RemboursementUniqueTests`, `RemboursementConcurrentTests` |
| Rejet après réception : stock réintégré, client sans rien | Pas de transition de rejet après réception | `TransitionsTests` |
| Remboursement au prix catalogue, remise ignorée (40 000 rendus pour 30 000 payés) | Prorata du montant payé, franc inférieur | `MontantRembourseTests` |
| Photo sur un retour clos, nom d'origine du fichier conservé, aucune limite, `/media/` public en dev et absent en production | Statuts ouverts, 5 photos, UUID, téléchargement authentifié | `PhotosTests` |
| Vendeur jamais prévenu d'une demande | Notification après commit | `NotificationEtVisibiliteTests` |
| Un vendeur ne voit pas ses propres retours d'acheteur | Visibilité client **ou** boutique | `NotificationEtVisibiliteTests` |
| Pas de limite dédiée | `retour_creation` 10/h, `retour_photo` 30/h | `LimitesDeDebitTests` |
| Une même ligne citée deux fois dans une demande dépasse la quantité achetée | Quantités cumulées dans la demande | `EligibiliteTests` |
| Refus du vendeur sans recours, ou demande bloquée par un vendeur muet (reversement suspendu sans fin) | Contestation devant la modération puis l'administration ; échéances posées en base (contraintes) et appliquées par la tâche | `CircuitTransitionsTests`, `EcheancesTests` |
| Vendeur et tâche simultanés : deux transitions | Revérification sous verrou, `skip_locked` | `EcheancesConcurrenceTests` |
| Statut écrit hors de la table des transitions | Garde-fou statique sur le code de production | `GardeFouStatutTests` |
| Modérateur qui agit sur le colis ou l'argent, voit tous les retours ou l'e-mail du client | Rôle `moderation` limité aux décisions de recours, visibilité limitée aux dossiers passés en modération, `client_email` à `null` | `CircuitTransitionsTests`, `ModerationVisibiliteTests`, `ApiLectureTests` |
| Auteur d'une étape révélé aux parties | `auteur` renvoyé à l'administration seulement | `ApiLectureTests` |
| Retour « remboursé » sans `Remboursement` (aucun paiement), montant non entier | 400, étape annulée, alerte ; contraintes en base | `RemboursementRetourTests` |
| Commande remboursée plus qu'elle n'a coûté (retour puis livraison non reçue, y compris en simultané) | Plafond sous verrou du paiement | `RemboursementRetourTests`, `PlafondConcurrenceTests` |
| Remboursement de retour refusé après coup, part du vendeur déjà reprise | Refus interdit pour le motif `retour` | `RetourPendantLeVersementTests` (`paiements`) |

Également vérifié : IDOR sur la liste et le détail, validation du contenu réel des images, quantités déjà couvertes et concurrence à la création (verrou sur la commande).

## 10. Limites de débit et performance

- `retour_creation` : 10/heure par compte (création seulement ; la liste garde le taux général). `retour_photo` : 30/heure. Relevées à 1 000/heure en dev (`dev.py`) pour Postman.
- Listes paginées (20), `select_related` + `prefetch_related` (articles, photos) : nombre de requêtes constant.
- Ordre des verrous : commande (création), demande puis stock, paiement et reversement (traitement).
- Tâche des échéances : une requête par type d'échéance (filtrée sur le statut, indexé), puis une transaction courte par demande. Pas d'index sur les dates d'échéance : à ajouter si les mesures le justifient.

## 11. Migrations

- **retours 0003** : statut `annule` ; `PhotoRetour.image` renommée en UUID (`CheminUploadUUID`). Les fichiers existants gardent leur nom.
- **retours 0004** : `frais_livraison_rembourses` (0 pour l'existant).
- **retours 0005** : quatre statuts du circuit, échéances et dates de recours, `RetourHistorique`.
- **retours 0006** : échéance du vendeur des demandes existantes en statut `demande`, comptée à partir de la migration (jamais avant la création + 48 h).
- **retours 0007** : contraintes d'échéance.
- **retours 0008** : contrainte de montant (entier, ≥ 0) ; la migration s'arrête en listant les demandes fautives s'il en existe.
- **notifications 0005** : aide du champ lien (route de modération).
- **paiements 0011** : contrainte de montant des remboursements (entier, > 0), même vérification préalable.

En dev : `docker exec anitche-backend python manage.py migrate`.

## 12. Tests

⚠️ **Toujours sur PostgreSQL** (`config.settings.ci`), en vérifiant « ok » et non « skipped » pour les tests de concurrence.

`apps/retours/tests.py` : parcours (livraison datée), concurrence à la création, admin en lecture seule, puis un test par faille (§ 9). `apps/retours/test_circuit.py` : circuit de décision (transitions et rôles, échéances et tâche, concurrence, historique, garde-fou, reversement, notifications, migration, API de lecture, argent et plafond). Tests adaptés hors module : `paiements` (`ReversementTests.livrer` date la livraison ; le scénario « retour sur un reversement déjà disponible » tourne avec `RETOUR_DELAI_JOURS=30` et un rejet motivé).

Postman : `postman_retours.json` (hors dépôt, nouvelle : aucune collection retours n'existait). Mise en place reprise de `postman_livraison.json` (comptes, produit, commande payée, livraison avec le code lu dans Mailpit). Parcours : retour refusé sur une commande confirmée → demande → notification du vendeur → reversement suspendu → photo (URL authentifiée, 404 pour un tiers, 401 sans jeton) → approbation → « j'ai expédié » → réception → remboursement (le second refusé) → remboursement à traiter côté administration → clôture → seconde demande annulée par le client. `admin_password` à renseigner.

## 13. Dette connue

- **Frais de retour** (qui paie le renvoi du colis) : toujours non modélisé. Les frais de livraison **payés à l'aller** sont traités (§ 6, [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 9).
- **Désaccord après réception** (colis vide, article abîmé par le client) : hors du circuit de décision, qui s'arrête à l'approbation ; il passe par un ticket support lié à la commande.
- **Réception bloquée** (le client a expédié, le vendeur ne réceptionne pas) : aucune échéance ; l'administration peut réceptionner elle-même.
- **Frontend** : écrans de contestation et espace modération à construire (§ 8).
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

### Pourquoi le vendeur décide, avec la modération puis l'administration en recours

Le vendeur connaît son produit et reçoit le colis : lui faire approuver, réceptionner et rembourser est le plus simple. Mais il est aussi la partie qui perd l'argent : son refus ne peut pas être définitif. Le client conteste devant un modérateur, qui tranche la plupart des cas, puis devant l'administration en dernier recours, ce qui garde la charge de l'administration faible. Les échéances (48 h pour le vendeur, 72 h pour contester) empêchent un dossier de rester ouvert, et donc un reversement de rester suspendu, sans fin. L'argent n'est rendu qu'après traitement du `Remboursement` par l'administration, dans la limite de ce que la commande a coûté.

### Pourquoi le prorata du montant payé

Rembourser au prix catalogue rendait plus que ce que le client avait payé quand un coupon avait servi. Le prorata garantit que la somme des remboursements d'une commande ne dépasse jamais son montant payé ; l'arrondi au franc inférieur évite qu'une accumulation d'arrondis dépasse ce montant.

### Pourquoi une URL de téléchargement authentifiée pour les photos

`/media/` n'est pas servi en production (les photos étaient donc invisibles pour le vendeur et l'administration) et l'est **sans contrôle** en développement. Les photos de litige peuvent montrer un domicile, un visage, un document : même traitement que les pièces KYC, servies par Django après contrôle d'accès. Le nom d'origine est remplacé par un UUID : il contenait parfois le nom du client.

### Pourquoi un statut `annule` distinct de `cloture`

`cloture` suit un remboursement ; `annule` signifie qu'aucun retour n'a eu lieu. Les distinguer permet de ne compter que les vrais retours dans les quantités déjà retournées, et de reprendre le reversement au bon moment.
