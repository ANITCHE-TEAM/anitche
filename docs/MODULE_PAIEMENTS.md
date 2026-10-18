# Module paiements — contrat et règles

> Périmètre : backend Django, `backend-django/apps/paiements/` (et ses points de contact dans `commandes`, `livraison`, `retours`, `fidelite`, `notifications`, `utilisateurs`).
> État : **GeniusPay est le fournisseur de production** (essai réel en sandbox réussi, `docs/ESSAI_GENIUSPAY_SANDBOX.md` § 7) ; les clés `live` du compte marchand restent à renseigner (§ 9). CinetPay, ancien fournisseur, retiré le 15 octobre 2026 ; en développement, le **fournisseur simulé** reste le défaut.

## 1. Ce sur quoi le module s'appuie, et qui s'appuie sur lui

| Module | Lien |
|---|---|
| `commandes` | Le paiement couvre une commande ou un checkout (`GroupeCommande`) ; la validation confirme par `commandes.services.confirmer_commande` ; l'annulation d'une commande appelle `paiements.services.traiter_paiements_apres_annulation` ; la livraison confirmée appelle `paiements.reversements.ouvrir_retractation` ; le checkout fige les frais vendeur (`paiements.frais`) |
| `livraison` | Fiche créée à la confirmation (adresse reprise du checkout) ; contestation « non reçu » → reversement suspendu, puis repris ou annulé avec remboursement |
| `retours` | Retour ouvert → reversement suspendu ; retour remboursé → `Remboursement` à traiter + part du vendeur réduite ; retour clos → reversement repris |
| `fidelite`, `notifications` | Écoutent le signal `paiement_valide` (commandes confirmées seulement) |
| `utilisateurs` | `DocumentKYC.numero_mobile_money` : numéro de reversement du vendeur |

## 2. Le flux de l'argent

```text
Client ──paie en ligne (Wave, Orange Money, MTN MoMo, Moov Money, carte)──▶ GeniusPay ──▶ compte ANITCHE
                                                                              │
     commande confirmée ─▶ Reversement « en attente de livraison »           │ ANITCHE conserve les fonds
     livraison confirmée ─▶ « délai de rétractation » (7 jours)             │
     délai écoulé        ─▶ « disponible »                                   │
     versement           ─▶ « versé » : prix − commission − frais fixes ─────┴──▶ mobile money du vendeur
```

- **Aucun paiement à la livraison** : le client paie avant ; une commande non payée expire à 30 minutes (`commandes`). Si elle a un paiement en attente, son état est d'abord **redemandé au fournisseur** (§ 8, « Réconciliation ») : un client débité dont le webhook s'est perdu n'est jamais laissé avec une commande annulée sans paiement validé ni remboursement.
- **Tous les montants sont en FCFA entiers**, calculés côté serveur ; un montant envoyé par le client est ignoré.
- Argent à rendre au client : un **`Remboursement`** par commande (§ 6). Argent dû au vendeur : un **`Reversement`** par commande (§ 7).

## 3. Modèles

- **`Paiement`** : `reference` (`PAY-<année>-<10 hex>`, 30 caractères au plus), `client` (**PROTECT**), `commandes` (**relation** : les commandes réellement payées ; remplace la liste JSON `metadata.commandes_couvertes`), `commande` / `groupe_commande` (cible demandée, contrat API conservé), `methode` (moyen choisi par le client), **`fournisseur`** (qui encaisse : `simule`, `geniuspay`), `statut`, `montant`, `devise` (`XOF`), `transaction_id_externe`, `hash_jeton_notification` (empreinte SHA-256, jamais le jeton), `url_paiement`, `adresse_livraison`, `metadata` (motifs internes, jamais exposés, jamais alimentés par une notification), `date_derniere_reconciliation` (dernier état demandé au fournisseur hors webhook ; interne, non exposé).
- **Transitions** (`services.py`, sous verrou de ligne) : `en_attente → valide | echoue | annule` ; `echoue → valide` ; `annule → valide` (succès tardif) ; `valide` est final. Les statuts `a_rembourser` et `rembourse` n'existent plus. Origine d'un succès ou d'un échec : le webhook du fournisseur, ou la réconciliation (§ 8).
- **Ordre des verrous, unique dans tout le code : Commandes (triées par `pk`) → Paiement.** Voir le tableau ci-dessous. L'ordre inverse (Paiement → Commandes) dans `valider_paiement` créerait un interblocage avec l'expiration : webhook en 500, paiement annulé, commande annulée, argent encaissé, aucun remboursement.

| Chemin | Ordre des verrous |
|---|---|
| `initier_paiement` | Commandes (`select_for_update`, `order_by("pk")`) → création du Paiement ; appel au fournisseur **après** la transaction |
| `valider_paiement` (webhook, réconciliation) | Commandes du paiement (`select_for_update(of=("self",))`, `order_by("pk")`) → Paiement → Livraison (création) → Reversement (création) → Remboursement |
| `annuler_commande` (client, administration, expiration, boutique indisponible, `abandonner_livraison`) | Livraison → Commande (`UPDATE` conditionnel) → Livraison relue si la première lecture n'a rien trouvé → Stocks (triés par `variante_id`) → Paiement (`traiter_paiements_apres_annulation` → `_passer_statut`) → Reversement |
| `marquer_echoue`, `annuler_paiement_client` | Paiement seul |
| Retours (`retours.services`) | DemandeRetour (ou Commande à la création) → Stocks (triés par `variante_id`) → Remboursement, Reversement ; jamais le Paiement |
| Livraison (transitions, contestation) | Livraison → Commande (`UPDATE`) ; jamais le Paiement |
| Reversements (`paiements.reversements`) | Reversement, AjustementVendeur ; jamais Commande ni Paiement |

La Livraison est prise avant la Commande par `annuler_commande`, et créée après elle par `valider_paiement`. Il n'y a pas de cycle : `valider_paiement` ne la crée que pour une commande « créée », qui n'a pas encore de fiche à verrouiller. Seule exception, voulue : si `annuler_commande` n'a trouvé aucune fiche, il la relit **après** l'`UPDATE` de la commande, car une validation concurrente a pu la créer pendant que cet `UPDATE` attendait son verrou. Détail : [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 12.
- **`JournalWebhook`** : idempotence (`fournisseur`, `evenement_id` uniques) et audit ; statuts `recu`, `traite`, `ignore` (transaction non finalisée : rejouable), `erreur` (rejouable).
- **`Remboursement`** (§ 6), **`BaremeFrais`** (§ 5), **`Reversement`** et **`AjustementVendeur`** (§ 7).

## 4. Endpoints — `/api/paiements/`

### Client (`IsAuthenticated` ; ses paiements uniquement, 404 sinon)

| Méthode | Chemin | Rôle / réponses |
|---|---|---|
| POST | `initier/` | Corps : `commande_id` **ou** `groupe_commande_id`, `methode` (`wave`, `orange_money`, `mtn_money`, `moov_money`, `carte_bancaire`). **201** : le paiement, avec `url_paiement` vers la page du fournisseur. **400** : commande d'un autre client, déjà payée ou en cours de paiement, annulée, adresse manquante, boutique indisponible (commande annulée), `espece_livraison`, montant hors limites du fournisseur. **502** : fournisseur injoignable (le paiement passe `echoue`, un nouvel essai est possible). **429** : limite `paiements` |
| GET | `` | Ses paiements (administration : tous, avec `fournisseur`, `transaction_id_externe`, `metadata`). Filtre `?reference=PAY-…` : correspondance exacte (valeur bornée à 30 caractères), appliquée **après** le filtre par compte : une référence inconnue ou appartenant à un autre compte donne une liste vide (pas 404). Valeur vide : filtre ignoré. **`?perimetre=achats`** : seulement **ses propres** paiements et représentation `Paiement` (celle du client), quel que soit le rôle ; sans lui, un administrateur voit tous les paiements en représentation `PaiementAdmin`. Valeur inconnue : ignorée. Combinable avec `reference` |
| GET | `<uuid>/` | Détail. Avec `?perimetre=achats` : **404** si le paiement n'est pas celui du compte, représentation `Paiement` |
| POST | `<uuid>/annuler/` | Abandonne un paiement `en_attente` pour en relancer un autre (autre moyen). **200** ; **409** si le paiement n'est plus en attente. Un succès tardif de l'ancien devient un remboursement dû |

Réponse d'un paiement (client) : `id`, `reference`, `client`, `client_email`, `commande`, `groupe_commande`, `commandes`, `methode`, `methode_display`, `statut`, `statut_display`, `montant`, `devise`, `url_paiement`, `adresse_livraison`, `remboursements` (`reference`, `commande`, `montant`, `motif`, `statut`, dates), `date_creation`, `date_validation`, `date_mise_a_jour`.

### Fournisseurs (sans JWT ; authentifiés par l'adaptateur)

| Méthode | Chemin | Rôle |
|---|---|---|
| POST | `webhook/<fournisseur>/` | Notification de paiement → authentification → **revérification serveur à serveur** → application sous verrou. **200** traité, déjà traité ou non finalisé ; **401** non authentique ; **404** fournisseur ou paiement inconnu (ou ouvert chez un autre fournisseur) ; **400** incohérente (montant, devise, identifiants) ; **503** vérification impossible (le fournisseur réessaie) |
| POST | `webhook/<fournisseur>/transfert/` | Notification d'un transfert de reversement, même chaîne |

### Vendeur (`IsAuthenticated` + `EstVendeurValide` ; sa boutique uniquement)

| Méthode | Chemin | Rôle |
|---|---|---|
| GET | `vendeur/reversements/` | Paginé, filtre `?statut=`. Par commande : `numero_commande`, `statut`, `montant_brut`, `montant_commission`, `montant_frais_fixes`, `montant_retours`, `montant_livraison`, `montant_net`, `montant_ajustements`, `montant_a_verser`, `lignes` (article, quantité, prix, taux, frais fixe unitaire, commission, frais fixes, net), `date_livraison`, `date_disponibilite` (« quand »), `date_versement`, `canal`, `reference_externe` |
| GET | `vendeur/reversements/resume/` | `en_attente_livraison`, `en_retractation` (dont suspendus), `disponible` (dont versements en cours), `verse` (FCFA nets) et leurs `nombre_*`, `ajustements_en_attente` (≤ 0), `delai_retractation_jours` |

### Administration (`IsAuthenticated` + `EstAdministrateur` : rôle admin, jamais `is_staff` seul)

| Méthode | Chemin | Rôle |
|---|---|---|
| GET | `admin/remboursements/` | `?statut=a_traiter` pour la file de travail |
| POST | `admin/remboursements/<uuid>/traiter/` | `decision` = `effectue` (`reference_externe` obligatoire) ou `refuse` (`commentaire` obligatoire). **409** si déjà traité. Le client est notifié |
| GET | `admin/reversements/` | `?statut=disponible` pour les versements à faire |
| POST | `admin/reversements/<uuid>/verser/` | Versement **manuel** : l'admin a envoyé l'argent, il saisit `reference_externe`. **409** si pas disponible |
| POST | `admin/reversements/<uuid>/transferer/` | Versement par l'**API de transfert** du fournisseur ; `operateur` facultatif (déduit du numéro : 07 Orange, 05 MTN, 01 Moov ; Wave à préciser). **409** si refus **certain** (rien n'a été créé chez le fournisseur : le reversement reste `disponible`). **200 avec `statut = "en_cours"`** si l'issue est incertaine (délai dépassé, 5xx, réponse illisible) ou pas encore confirmée : **ne pas relancer**, le reversement est réconcilié (§ 8) ; une relance répond 409. `date_debut_transfert` (lecture seule) dit depuis quand il est en cours |
| GET, POST | `admin/baremes/` | Barèmes de frais (§ 5), dont `seuil_petit_article` / `frais_fixe_petit_article` (facultatifs, ensemble, sinon **400**) |
| GET, PUT, PATCH, DELETE | `admin/baremes/<uuid>/` | Selon que le barème a commencé ou non (règle ci-dessous) |

**Règle des barèmes (septembre 2026)** : un barème commencé a pu s'appliquer à des ventes, son historique ne se réécrit donc plus, et un barème ne commence jamais dans le passé (il deviendrait rétroactivement « en vigueur » sur une période déjà vendue). Changer de barème, c'est **clôturer l'actuel puis en créer un nouveau**.

| Barème | `POST` (création) / `PUT` / `PATCH` | `DELETE` |
|---|---|---|
| Nouveau ou pas encore commencé (`date_debut` future) | tout se modifie, mais **`date_debut` maintenant ou plus tard**. Plus ancienne → **400**, `errors.code = ["date_debut_passee"]`, `errors.date_debut`. `date_debut` absente : à la création, le barème commence à l'instant de l'enregistrement ; en modification, elle ne change pas | **204** |
| Commencé (`date_debut` ≤ maintenant) | **seule `date_fin`, pour le clôturer** : maintenant ou plus tard, et seulement s'il n'est pas déjà clôturé dans le passé. Toute autre modification (autre champ, `date_fin` passée ou vide, barème déjà clôturé) → **400**, `errors.code = ["bareme_deja_applique"]`, message dans `detail`, champs en cause dans `errors`. Renvoyer une valeur identique n'est pas une modification | **409**, `errors.code = ["bareme_deja_applique"]` |

- **« Maintenant »** : pour `date_debut` comme pour `date_fin`, une date passée de moins d'une minute (latence, horloge du client en retard) est acceptée et **ramenée à l'heure du serveur**, jamais avant.
- Même contrôle pour `POST`, `PUT` et `PATCH` (`BaremeFraisSerializer`) et pour `DELETE` (`BaremeFraisDetailView.perform_destroy`). `DELETE` n'existait pas avant (405) : il est ouvert pour les seuls barèmes programmés. 409 plutôt que 400 : la requête est correcte, c'est l'état du barème qui l'interdit (comme les autres 409 du module).
- Un barème créé sans `date_debut` commence immédiatement et ne peut plus qu'être clôturé : pour pouvoir encore le corriger, le créer avec une `date_debut` future.
- Création sans `date_debut` mais avec une `date_fin` passée : **400** sur `date_fin` (répondait 500, contrainte en base, avant septembre 2026).
- Les migrations de données (0004, 0008) écrivent directement en base : elles ne passent pas par cette règle.

Le Django admin est **en lecture seule** pour tous ces modèles (aucun ajout, aucune modification, aucune suppression, même pour un superutilisateur : testé pour les barèmes).

## 5. Frais vendeur

- **Montants TVA INCLUSE** (décision d'équipe, septembre 2026) : la commission et les frais fixes sont ce que le vendeur paie, **aucune TVA ne s'y ajoute**. La décomposition HT / TVA n'est pas faite (point ouvert, § 15).
- **Barème** (`BaremeFrais`) : `taux_commission` (%, 0 à 100), `frais_fixe_article` (FCFA), `seuil_petit_article` et `frais_fixe_petit_article` (FCFA, **facultatifs, ensemble** : contrainte en base et 400 à l'API), `boutique` (vide = plateforme ; renseignée = offre de lancement), `date_debut`, `date_fin`, `libelle`, `cree_par`. Seuil vide : un seul frais fixe (`frais_fixe_article`), comme tous les barèmes créés avant la migration 0007. Aucun taux n'est codé en dur : tout est modifiable par l'administration (API).
- **Barème de la plateforme en vigueur** (migration 0008, 28/09/2026, libellé « Plateforme : 14 % + 100 FCFA/article jusqu'à 3 000 FCFA (200 FCFA au-delà), TVA incluse ») : **14 % du prix de l'article, plus 100 FCFA par article si ce prix est inférieur ou égal à 3 000 FCFA, sinon 200 FCFA**. Il remplace **12 % + 200 FCFA** (migration 0004), clôturé au même instant et jamais modifié : les commandes passées avant gardent 12 % + 200 FCFA.
- **Barème appliqué** : celui de la boutique s'il est en vigueur (il reste prioritaire sur celui de la plateforme), sinon celui de la plateforme ; le plus récent l'emporte en cas de chevauchement.
- **Calcul, par ligne, au checkout**, figé dans `CommandeItem` (`taux_commission`, `frais_fixe_unitaire` = frais fixe **réellement appliqué** à l'article, `montant_commission`, `montant_frais_fixes`, `montant_net_vendeur`), puis additionné dans le `Reversement` :
  - le **prix** est le prix effectif de l'article (`prix_promo` s'il est valide, sinon `prix`) : une promotion qui fait passer l'article sous 3 000 FCFA lui donne le frais fixe réduit ;
  - commission = prix × quantité × taux, **arrondie au franc sur le total de la ligne, demi-franc au franc supérieur** (`ROUND_HALF_UP`), sur le prix **avant remise** : **ANITCHE supporte les coupons** ;
  - frais fixes = frais fixe de l'article × quantité ; le seuil porte sur le prix **d'un** article, jamais sur le total de la ligne (3 × 2 000 FCFA : 3 × 100) ;
  - net = brut − commission − frais fixes, **jamais négatif** (sur un article à moins de ~117 FCFA, les frais sont plafonnés au prix).

| Article (prix effectif) | Quantité | Commission 14 % | Frais fixes | Net vendeur |
|---|---|---|---|---|
| 2 999 FCFA | 1 | 420 (419,86) | 100 | 2 479 |
| 3 000 FCFA | 1 | 420 | 100 | 2 480 |
| 3 001 FCFA | 1 | 420 (420,14) | 200 | 2 381 |
| 3 500 FCFA en promotion à 2 800 | 1 | 392 | 100 | 2 308 |
| 2 000 FCFA | 3 | 840 | 300 | 4 860 |
| 5 000 FCFA | 2 | 1 400 | 400 | 8 200 |

- **Abonnement selon le volume de ventes** : non implémenté (0 au lancement), voir § 15.

## 6. Remboursements

Un **`Remboursement`** par commande à rembourser : `reference` (`RMB-…`), `paiement`, `commande`, `retour` (si issu d'un retour), `montant`, `motif`, `statut` (`a_traiter` → `effectue` / `refuse`), `reference_externe`, `commentaire`, `traite_par`, dates.

| Motif | Quand | Montant |
|---|---|---|
| `commande_annulee` | Commande annulée alors que son paiement est encaissé (client, administration, ou paiement arrivé après l'expiration) | `montant_total` de la commande |
| `paiement_en_double` | Un paiement réussit pour une commande déjà payée par un autre (paiement abandonné puis succès tardif) | `montant_total` de la commande |
| `retour` | Retour marqué remboursé (module retours) | `montant_remboursement` du retour |
| `livraison_non_recue` | Contestation « colis non reçu » jugée fondée par l'administration ([`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md)) ; le reversement est annulé | `montant_total` de la commande |

- Unicité : une seule fois par (paiement, commande) hors retours, une seule fois par retour. Une annulation signalée deux fois ne crée rien de plus.
- **Traitement au lancement : manuel.** L'administration est alertée (journal `securite` + notification), rembourse depuis le **tableau de bord du fournisseur**, puis saisit la référence (`admin/remboursements/<id>/traiter/`). Le client voit l'état dans `remboursements` de son paiement et reçoit une notification.
- **Plus tard** : `FournisseurPaiement.rembourser()` existe dans l'interface ; l'adaptateur GeniusPay n'implémente pas le remboursement : il répond « non supporté ».
- **Retour remboursé et vendeur** : la commission des articles retournés est **rendue au vendeur**, le **frais fixe reste acquis à ANITCHE** : sa part baisse de (prix − commission) des articles retournés (§ 7).

## 7. Reversements aux vendeurs

Un **`Reversement`** par commande confirmée : `reference` (`REV-…`), `boutique`, `montant_brut`, `montant_commission`, `montant_frais_fixes`, `montant_retours`, `montant_livraison` (livraison offerte par la boutique), `montant_net`, `montant_ajustements` (≤ 0), `montant_a_verser`, `statut`, `date_livraison`, `date_disponibilite`, `date_versement`, `canal` (`mobile_money`), `numero_destinataire` (**chiffré**, figé au versement), `operateur`, `fournisseur`, `reference_externe`, `verse_par`, et le suivi d'un transfert en cours : `date_debut_transfert`, `date_derniere_reconciliation`, `date_derniere_alerte` (remis à zéro à chaque nouveau transfert).

| Statut | Passage |
|---|---|
| `en_attente_livraison` | Paiement validé, commande confirmée |
| `en_retractation` | Livraison confirmée : `date_disponibilite` = livraison + **7 jours** (`REVERSEMENT_DELAI_RETRACTATION_JOURS`) |
| `suspendu` | Retour ouvert ou livraison contestée « non reçu » pendant le délai (ou reversement disponible pas encore versé) ; reprend quand plus aucun retour ni contestation n'est ouvert |
| `disponible` | Délai écoulé (tâche Celery horaire `rendre_reversements_disponibles`) |
| `en_cours` | Transfert demandé au fournisseur, en attente de sa confirmation (notification, réconciliation, ou résolution manuelle). Ni nouveau transfert ni versement manuel possibles |
| `verse` | Versé (manuellement avec référence, ou transfert confirmé) ; le vendeur est notifié |
| `annule` | Commande annulée avant versement |

- **Retour remboursé avant versement** : `montant_retours` augmente, `montant_net` est recalculé.
- **Retour remboursé après versement** : un **`AjustementVendeur`** négatif est créé ; il est **déduit du prochain reversement** de la boutique (du plus ancien au plus récent, tant que le montant versé reste ≥ 0 ; le reste attend le suivant).
- **Frais de livraison** ([`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 9) : payés par le client, ils reviennent à ANITCHE et n'entrent **jamais** dans le reversement. **Livraison offerte** par la boutique : `montant_net` = brut − commission − frais fixes − retours − `montant_livraison`, où `montant_livraison` = min(tarif figé, net des articles) ; si le tarif dépasse le net, le reste devient un `AjustementVendeur` à la livraison (une commande annulée avant ne coûte rien). Une contestation « non reçu » fondée efface ce reste tant qu'il n'est pas déduit d'un versement.
- **`AjustementVendeur.nature`** : `retour` (retour remboursé après versement, lié au retour), `livraison_offerte` (reste non couvert), `frais_livraison_retour` (frais de livraison rendus au client pour un retour imputable au vendeur). Au plus un ajustement de chacune des deux dernières natures par commande (contrainte en base) : rejouer le traitement ne facture jamais deux fois.
- **Canal : mobile money uniquement**, sur `DocumentKYC.numero_mobile_money` du vendeur (format ivoirien, converti en +225…). En conséquence, **`compte_bancaire` n'est plus collecté** (supprimé, migration utilisateurs 0009, voir `MODULE_UTILISATEURS.md`).
- **Versement manuel** par l'administration (`verser/`, avec la référence du virement). Le fournisseur GeniusPay n'implémente pas les transferts : `transferer` et `verifier_transfert` lèvent `OperationNonSupportee`, `POST admin/reversements/<id>/transferer/` répond **409** sans appel réseau et laisse le reversement `disponible`, et `webhook/geniuspay/transfert/` répond 401. La route de transfert et l'interface `lire_notification_transfert` restent pour un fournisseur qui les implémente (le fournisseur simulé s'en sert).
- **Une seule règle d'application** (`reversements.appliquer_etat_transfert`) pour la réponse immédiate de `transferer`, la notification de transfert vérifiée et la réconciliation, sous verrou de ligne, statut relu : `SUCCES` **conforme** (montant entier égal au `montant_a_verser` lu sous verrou, devise `XOF`, `services.controler_montant_et_devise`, montant et devise lus dans la réponse du fournisseur, jamais recopiés du reversement) → `verse`, vendeur notifié une fois ; `SUCCES` non conforme → reste `en_cours` (l'argent a pu partir), `ERROR` dans `securite` et **alerte de l'administration** ; `ECHEC` → `disponible`, ajustements libérés ; `EN_ATTENTE` → rien.
- **Issue incertaine après l'envoi** (`reversements.transferer_reversement`) : seuls `TransfertRefuse`, `MontantHorsLimites` et `OperationNonSupportee` (`REFUS_CERTAINS` : le fournisseur garantit que rien n'a été créé) rendent le reversement `disponible` (409). **Toute autre `ErreurFournisseur`** (délai dépassé, 5xx, redirection, réponse illisible, erreur non classée d'un futur adaptateur) le laisse **`en_cours`**, montant et ajustements figés (200 `en_cours`, `ERROR` « issue incertaine ») : un délai dépassé ne prouve pas que l'argent n'est pas parti, et un retour à `disponible` permettrait de payer deux fois. Un `ResultatTransfert` `ECHEC` est accepté comme confirmation du fournisseur ; l'adaptateur ne le renvoie que pour un statut final explicite.
- **Succès tardif** : un `SUCCES` reçu (notification ou réconciliation) pour un reversement qui n'est plus `en_cours` (`disponible`, versé à la main, versé par un autre transfert, `annule`…) ne change rien et donne une **alerte « double paiement possible »** ; s'il confirme le transfert qui a versé le reversement (même fournisseur, même référence), il est silencieux. La notification d'un reversement versé à la main (`fournisseur` vide) est donc acceptée, puis vérifiée chez le fournisseur qui l'envoie.

## 8. Fournisseurs de paiement

### Architecture

```text
apps/paiements/fournisseurs/
  base.py      interface FournisseurPaiement + types communs (SessionPaiement, Notification, EtatTransaction, ResultatTransfert)
  simule.py    fournisseur simulé (dev, tests, Postman) — refusé en production
  geniuspay.py adaptateur GeniusPay (API marchand v1, encaissement seulement ; inactif tant que PAIEMENT_FOURNISSEUR n'est pas geniuspay)
  __init__.py  registre ; fournisseur actif = PAIEMENT_FOURNISSEUR
```

Interface commune : `initier(paiement)`, `lire_notification(request)`, `authentifier_notification(notification, objet)`, `verifier_transaction(paiement, notification)`, `rembourser(remboursement)`, `transferer(reversement, telephone, operateur)`, `lire_notification_transfert`, `verifier_transfert`.

**Règle commune** : une notification n'est qu'un signal. Chaîne obligatoire, identique pour tous les fournisseurs (`services.traiter_notification_paiement`) : lire → le paiement doit avoir été ouvert **chez ce fournisseur** → authentifier → journal d'idempotence → **redemander l'état réel au fournisseur** → contrôler montant, devise et identifiants → transition sous verrou.

### Réconciliation (webhook perdu)

Un webhook peut ne jamais arriver : panne du fournisseur, déploiement en cours, erreur 500, routage. Sans autre chemin, le client serait débité et sa commande annulée sans remboursement. Deux chemins redemandent donc l'état au fournisseur (`verifier_transaction(paiement)`, sans notification). Les deux passent par `services.reconcilier_paiement` et suivent **le même chemin que le webhook** : `valider_paiement` ou `marquer_echoue`, avec le montant et la devise recontrôlés. L'appel réseau se fait hors transaction et sans verrou. Les transitions sont idempotentes : un webhook simultané, ou deux exécutions en parallèle, ne produisent qu'un seul effet.

1. **Avant l'expiration d'une commande** (`commandes.services.expirer_commandes_impayees` → `reconcilier_avant_expiration`), pour chaque paiement `en_attente` :

   | Réponse du fournisseur | Effet |
   |---|---|
   | succès | paiement validé, commande confirmée, **pas d'expiration** |
   | échec | paiement `echoue` (`metadata.motif_echec` = « échec constaté par réconciliation »), puis expiration normale |
   | en attente, injoignable, réponse incohérente, transaction introuvable | expiration **repoussée** jusqu'à `COMMANDE_DELAI_PAIEMENT_MINUTES` + `PAIEMENT_RECONCILIATION_DELAI_GRACE_MINUTES` (30 + 30 min), puis faite quand même : un succès plus tardif devient un remboursement (point 2) |

   Un fournisseur injoignable n'est appelé qu'une fois par exécution. Les commandes suivantes sont traitées comme « incertaines ». Une transaction introuvable ne concerne que son paiement : les commandes suivantes sont vérifiées normalement.

2. **Tâche planifiée** `apps.paiements.tasks.reconcilier_paiements`, toutes les 10 minutes (`CELERY_BEAT_SCHEDULE`). Elle traite :
   - les paiements `en_attente` depuis plus de `PAIEMENT_RECONCILIATION_AGE_MINUTES` ;
   - les paiements `annule` ou `echoue` créés dans les `PAIEMENT_RECONCILIATION_FENETRE_HEURES` dernières heures et qui ont un `transaction_id_externe`.

   Un succès constaté sur un paiement annulé suit la règle habituelle : la commande annulée donne un remboursement `commande_annulee` (un seul : verrou et `get_or_create`), et une commande déjà payée donne un remboursement `paiement_en_double`. La tâche traite au plus `PAIEMENT_RECONCILIATION_LOT` paiements, d'abord ceux jamais vérifiés, puis les moins récemment vérifiés (`date_derniere_reconciliation`) : aucun paiement n'est laissé de côté d'une exécution à l'autre. Elle **s'arrête proprement** dès que le fournisseur est injoignable.

Chaque changement de statut fait par la réconciliation est journalisé dans le logger `securite` : `Paiement PAY-… : en_attente → valide (source=reconciliation).` Une incohérence de montant ou de devise y est journalisée en `ERROR` (`… rejeté (source=reconciliation)`), sans transition. Aucun `JournalWebhook` n'est créé, car il ne s'agit pas d'une notification.

#### Classement des réponses du fournisseur

Une réponse d'erreur ne veut pas toujours dire « fournisseur en panne ». Si **toute** réponse HTTP ≥ 400 du fournisseur était traitée comme une panne, un seul paiement inconnu du fournisseur (404) arrêterait la tâche à chaque exécution, avant les autres paiements, et rendrait l'expiration « incertaine » pour toutes les commandes suivantes ; un paiement encaissé placé derrière ne serait jamais vérifié. `services.reconcilier_paiement` distingue donc :

| Réponse à `verifier_transaction(paiement)` | Exception de l'adaptateur | Réconciliation (tâche et vérification avant expiration) |
|---|---|---|
| succès, échec, en attente | aucune (`EtatTransaction`) | validé, échoué ou rien ; paiement daté (`date_derniere_reconciliation`) |
| **404 `TRANSACTION_NOT_FOUND` sur la vérification de CE paiement** (`GET /payments/{référence}`), ou paiement sans référence GeniusPay (initiation inaboutie) | **`TransactionIntrouvable`**, qui **n'hérite pas** d'`ErreurFournisseur` | **refus** (`refuse`) : `ERROR` dans `securite` (`Réconciliation de PAY-… : transaction refusée par le fournisseur geniuspay (…)`), paiement daté (il passe ensuite après ceux jamais vérifiés), **passage au paiement suivant** ; le fournisseur **n'est pas** marqué indisponible. Avant expiration : état incertain, donc délai de grâce, puis expiration |
| identifiants qui ne correspondent pas | `NotificationInvalide` | refus (`ERROR`, paiement daté) |
| montant ou devise incohérents | aucune (contrôle `IncoherenceTransaction` sous verrou) | refus (`ERROR`, paiement daté, sans transition) |
| délai dépassé, erreur de connexion, TLS, redirection, 5xx, 429, 400, 401, 403, 404 d'un autre code ou illisible (page d'un proxy), réponse 200 illisible ou sans `success` / `data` | `ErreurFournisseur` | **injoignable** : la tâche s'**arrête** (« Interrompue : fournisseur injoignable ») ; l'expiration ne rappelle plus ce fournisseur dans l'exécution ; paiement **non** daté |

Seule la vérification d'un paiement classe un 404 `TRANSACTION_NOT_FOUND` comme « propre à la transaction » : à l'initiation, il reste une `ErreurFournisseur`. Le classement se fait sur le code HTTP et le code d'erreur du corps.

**Webhook.** Une vérification qui lève `TransactionIntrouvable` après une notification authentique donne **503** et un `JournalWebhook` en `erreur` : le fournisseur peut rejouer la notification, et la réconciliation reste le filet de sécurité. Raison de ne pas répondre 4xx : juste après la notification, la transaction pourrait ne pas être encore consultable (hypothèse, à vérifier en sandbox) ; un refus définitif ferait perdre un succès que le rejeu aurait rattrapé. Un 404 durable ne coûte que des rejeux.

| Réglage (`config/settings/base.py`, variable d'environnement du même nom) | Défaut | Rôle |
|---|---|---|
| `PAIEMENT_RECONCILIATION_DELAI_GRACE_MINUTES` | 30 | Délai supplémentaire avant d'expirer une commande dont le paiement est encore incertain |
| `PAIEMENT_RECONCILIATION_AGE_MINUTES` | 15 | Âge minimal d'un paiement en attente avant vérification par la tâche : en deçà, le webhook arrive normalement |
| `PAIEMENT_RECONCILIATION_FENETRE_HEURES` | 24 | Fenêtre de vérification des paiements annulés ou échoués |
| `PAIEMENT_RECONCILIATION_LOT` | 50 | Paiements vérifiés au plus par exécution : 50 × 10 s (`GENIUSPAY_TIMEOUT`) reste sous l'intervalle de 10 minutes |

### Réconciliation des reversements (transfert incertain)

Une notification de transfert peut se perdre et une demande de transfert peut échouer après l'envoi (délai dépassé) : sans autre chemin, un reversement resterait `en_cours` indéfiniment. La tâche planifiée `apps.paiements.tasks.reconcilier_reversements` (toutes les 10 minutes, décalée de 5 minutes par rapport à `reconcilier_paiements`) redemande l'état au fournisseur (`verifier_transfert(reversement)`, sans notification, par la référence `REV-…` envoyée comme référence marchande) et l'applique avec `appliquer_etat_transfert` (§ 7). Appel réseau hors transaction, sans verrou.

- Elle traite au plus `REVERSEMENT_RECONCILIATION_LOT` reversements `en_cours` depuis plus de `REVERSEMENT_RECONCILIATION_AGE_MINUTES` (depuis `date_debut_transfert`), d'abord ceux jamais vérifiés, puis les moins récemment vérifiés (`date_derniere_reconciliation`).
- Puis elle **alerte l'administration** (journal `securite` en `ERROR` + notification, lien `/administration/reversements`) pour chaque reversement `en_cours` depuis plus de `REVERSEMENT_ALERTE_BLOCAGE_HEURES`, au plus une fois par 24 h et par reversement (`date_derniere_alerte`, réservée par une mise à jour conditionnelle : deux exécutions simultanées n'alertent qu'une fois). L'alerte « succès non conforme » suit la même règle.

| Réponse à `verifier_transfert(reversement)` | Effet |
|---|---|
| succès conforme / échec | `verse` (vendeur notifié) / `disponible`, `INFO` ou `WARNING` « (source=reconciliation) » |
| succès non conforme | reste `en_cours`, `ERROR` + alerte |
| en attente | rien, reversement daté |
| `TransactionIntrouvable` | reste `en_cours` (**jamais** `disponible` : un transfert absent de la recherche n'est pas une preuve), `ERROR`, daté, reversement suivant |
| `OperationNonSupportee` (GeniusPay) | reste `en_cours`, `WARNING`, daté, sans appel réseau ; l'alerte de blocage mène à la résolution manuelle |
| `NotificationInvalide`, fournisseur inconnu | reste `en_cours`, `ERROR`, daté |
| `ErreurFournisseur` | **injoignable** : la tâche s'arrête (« Interrompue : fournisseur injoignable »), reversement non daté |

**Diagnostic et résolution (administration, en ligne de commande).**

```bash
python manage.py reconcilier_reversements                                   # comme la tâche
python manage.py reconcilier_reversements --reference REV-… --lecture-seule  # état chez le fournisseur, rien n'est écrit
python manage.py reconcilier_reversements --reference REV-…                  # applique l'état pour ce reversement
python manage.py resoudre_reversement REV-… non-parti --par admin@… --motif "…"                        # → disponible
python manage.py resoudre_reversement REV-… verse --par admin@… --motif "…" --reference-externe T-…    # → verse
```

`resoudre_reversement` est la **seule** sortie d'un reversement que le fournisseur ne tranche pas, après vérification dans son tableau de bord ; aucune résolution n'est automatique et aucune route d'API ne l'expose. Administrateur actif de l'administration (`ROLES_ADMINISTRATION`), motif non vide, référence du fournisseur obligatoire pour `verse`, reversement `en_cours` relu sous verrou ; `WARNING` dans `securite` avec l'administrateur, la décision et le motif. `non-parti` libère les ajustements ; `verse` notifie le vendeur et garde le fournisseur du transfert (un succès tardif de ce même transfert est silencieux).

| Réglage | Défaut | Rôle |
|---|---|---|
| `REVERSEMENT_RECONCILIATION_AGE_MINUTES` | 10 | Âge minimal d'un transfert en cours avant vérification : en deçà, la notification arrive normalement |
| `REVERSEMENT_RECONCILIATION_LOT` | 50 | Reversements vérifiés au plus par exécution |
| `REVERSEMENT_ALERTE_BLOCAGE_HEURES` | 2 | Âge d'un transfert en cours au-delà duquel l'administration est alertée (répétée toutes les 24 h) |

### Fournisseur simulé

Aucun argent ne circule ; la chaîne de sécurité est la même. Notification signée **HMAC-SHA256** de `"<horodatage>.<corps brut>"` avec `PAIEMENT_SIMULE_SECRET`, en-tête `X-Signature-Simulation: t=<unix>,v1=<hex>`, **fenêtre de 5 minutes** (rejeu refusé). Corps : `{"evenement_id", "reference", "transaction_id", "statut": "succes|echec|en_attente", "montant", "devise"}` (montant entier et devise obligatoires pour un succès). En dev, le secret vaut `dev-simulation-anitche` (public, repris par Postman) ; `prod.py` refuse ce fournisseur.

**État « côté fournisseur » (réconciliation).** Le dernier état signé reçu pour un paiement devient l'état que le fournisseur simulé renvoie ensuite. Il est gardé dans le cache (Redis en dev, partagé par Django et Celery ; 3 jours). Sans état connu, la transaction est « en attente ». Pour simuler un webhook perdu ou une panne en dev :

```bash
docker compose -f infra/docker-compose.yml exec backend-django python manage.py simuler_etat_paiement PAY-2026-… succes      # ou echec, en_attente
docker compose -f infra/docker-compose.yml exec backend-django python manage.py simuler_etat_paiement PAY-2026-… injoignable  # ErreurFournisseur
docker compose -f infra/docker-compose.yml exec backend-django python manage.py simuler_etat_paiement PAY-2026-… introuvable  # TransactionIntrouvable (404 du fournisseur)
```

Ensuite, la tâche `reconcilier_paiements` (ou l'expiration de la commande) applique cet état. La commande est refusée pour un paiement ouvert chez un autre fournisseur. Les tests utilisent `fournisseurs.simule.definir_etat_distant`.

La même commande accepte une référence `REV-…` (état du transfert, montant par défaut : `montant_a_verser`), appliqué ensuite par `reconcilier_reversements`. Le comportement du prochain transfert simulé se choisit par `fournisseurs.simule.definir_comportement_transfert(reference, …)` : `DELAI_APRES_ENVOI` (transfert créé chez le fournisseur puis délai dépassé : issue incertaine), `REFUS_CERTAIN` (`TransfertRefuse`), `PANNE_AVANT_ENVOI` (`ErreurFournisseur`, rien créé). Sans comportement choisi, le transfert réussit et son état distant est enregistré.

**Aide de développement : `POST /api/paiements/simulation/<reference>/`.** La page `/paiement/simulation/<reference>` du portail (vers laquelle `FournisseurSimule.initier` redirige) et les tests E2E confirment ou font échouer un paiement simulé avec le **jeton du payeur**, sans jamais détenir le secret HMAC.

```text
POST /api/paiements/simulation/PAY-2026-3F9A1C0B7E/
Authorization: Bearer <access du client>
{"statut": "succes"}            # ou "echec"
200 Paiement (statut « valide » ou « echoue »)
400 statut absent ou autre que succes / echec   {errors: {statut: [...]}}
401 jeton absent ou invalide
404 route absente (production), PAIEMENT_FOURNISSEUR ≠ simule, paiement d'un autre fournisseur,
    d'un autre compte (administration comprise) ou inconnu
409 paiement plus « en_attente »
429 limite « paiements »
```

- **Même chemin que la commande** `simuler_etat_paiement` suivie de la réconciliation : `definir_etat_distant(reference, statut, montant du paiement, devise)` puis `services.reconcilier_paiement` (montant et devise contrôlés, transition sous verrou, signal `paiement_valide`, notifications). Aucune logique métier propre à la vue.
- **Différence avec un vrai webhook** : rien n'est écrit dans `JournalWebhook`. Le chemin webhook reste couvert par les tests et par la collection Postman.
- Le statut est vérifié **avant** de fixer l'état distant : un paiement déjà réglé garde l'état confirmé (409, état distant inchangé).
- **Trois fermetures** : (1) la route n'est montée que si `PAIEMENT_SIMULATION_API_ACTIVE` (`True` dans `dev.py` et `test.py`, `False` dans `base.py`, **figé à `False` dans `prod.py`**, jamais lu dans l'environnement) ; (2) 404 si `PAIEMENT_FOURNISSEUR` ou le fournisseur du paiement n'est pas `simule` (`prod.py` refuse déjà `simule`) ; (3) seul le payeur agit sur son paiement.
- Hors du schéma OpenAPI (`extend_schema(exclude=True)`, comme les webhooks) : le frontend écrit cet appel à la main, dans la page de dev uniquement.

### Liste fermée de production (`prod.py`)

`config/settings/prod.py` refuse de démarrer (`ImproperlyConfigured`, Django et Celery) hors de cette liste :

| Règle | Refus |
|---|---|
| `PAIEMENT_FOURNISSEURS_AUTORISES = ('geniuspay',)` | `simule`, `cinetpay`, valeur inconnue ou absente (le défaut de `base.py` est `simule`), casse différente |
| `geniuspay` | `GENIUSPAY_API_KEY`, `GENIUSPAY_API_SECRET` ou `GENIUSPAY_WEBHOOK_SECRET` manquant ou vide ; `GENIUSPAY_ENVIRONNEMENT` autre que `live` ou `sandbox` ; clés non cohérentes avec l'environnement (`live` ⇒ `GENIUSPAY_API_KEY` en `sk_live_…` et `GENIUSPAY_API_SECRET` en `ss_live_…`, `sandbox` ⇒ `sk_sandbox_…` et `ss_sandbox_…`) |
| `ANITCHE_STAGING_SANDBOX_PAIEMENT` | toute valeur autre que exactement `true` ou `false` (ou absente), vide comprise (`True`, `1`, `yes`… refusés) ; ne concerne que GeniusPay |

`infra/scripts/check_prod_env.sh` applique les mêmes règles avant le déploiement, et la CI de la pile de production les rejoue (`infra/ci/verifier_environnement.sh`, `config/tests_settings_prod.py`). Le mode staging de GeniusPay est décrit au § 9.

### Adaptateur GeniusPay

`fournisseurs/geniuspay.py`, **encaissement seulement** (API marchand v1, `https://geniuspay.ci/api/v1/merchant` ; l'ancienne adresse `pay.genius.ci` ne répondait plus le 14 octobre) :

| Étape | API |
|---|---|
| Authentification | en-têtes `X-API-Key` (`GENIUSPAY_API_KEY`) et `X-API-Secret` (`GENIUSPAY_API_SECRET`) à chaque appel |
| Initiation | `POST /payments` : la référence GeniusPay est stockée dans `Paiement.transaction_id_externe`, la référence ANITCHE part dans `metadata`. **Aucune URL de notification dans la requête** : elle se déclare dans le tableau de bord GeniusPay (`https://anitche.com/api/paiements/webhook/geniuspay/`) |
| Notification | signée : HMAC-SHA256 de `"<horodatage>.<corps brut>"` avec `GENIUSPAY_WEBHOOK_SECRET`, en-têtes `X-Webhook-Signature` et `X-Webhook-Timestamp`, tolérance de 5 minutes, comparaison à temps constant. Le JSON n'est lu qu'après la signature. Son statut, son montant et sa devise ne sont **jamais** utilisés. Événements `cashout.*`, inconnus et `webhook.test` : acquittés par 200 sans effet |
| Vérification | `GET /payments/{reference}`, systématique ; seule sa réponse fait foi (statut, `amount` — jamais `net_amount` ni `fees` —, `currency`, référence, environnement et `metadata` recoupés) |
| Environnement | celui de la notification et de chaque réponse doit être `GENIUSPAY_ENVIRONNEMENT` (`sandbox` ou `live`) |
| Transfert | non implémenté (`OperationNonSupportee`) |

Inactif par défaut : sans clés, sans secret de webhook ou sans environnement, rien n'est ouvert, vérifié ni accepté. **Confirmé par l'essai en sandbox** (`docs/ESSAI_GENIUSPAY_SANDBOX.md` § 7) : signature hexadécimale (64 caractères), secret `whsec_…` utilisé tel quel comme clé HMAC, `metadata` renvoyée par le `GET`, URL de paiement dans `checkout_url`, `environment` dans `data` (et non à la racine du corps), `webhook.test` refusé à la création d'un webhook (les six événements `payment.*` sont acceptés). L'essai se rejoue avec la commande de développement `essai_geniuspay` (`creer-webhook`, `payer`, `etat`), pas à pas dans le même runbook ; **reste à faire en `live`** : confirmer les préfixes des clés et le secret du webhook de production.

### Ajouter un fournisseur (PayDunya en réserve)

1. Créer `fournisseurs/paydunya.py` : une classe `FournisseurPayDunya(FournisseurPaiement)` avec `code = "paydunya"` et les méthodes de l'interface (sa signature de notification dans `lire_notification` / `authentifier_notification`, son API de vérification dans `verifier_transaction`).
2. L'ajouter à `FOURNISSEURS` (`fournisseurs/__init__.py`).
3. Ajouter ses clés dans `base.py` (depuis l'environnement, sans valeur par défaut), `prod.py` (exigées quand il est actif), `docker-compose.prod.yml`, `check_prod_env.sh`, `infra/.env.example`.
4. Tests : copier `GeniusPayTests` (faux serveur branché sur `requests`, au format de l'API du fournisseur).
5. Basculer : `PAIEMENT_FOURNISSEUR=paydunya`. Les paiements déjà ouverts chez l'ancien fournisseur restent notifiés sur son URL (`Paiement.fournisseur`).

Rien d'autre ne change : commandes, remboursements, reversements et frais ignorent le fournisseur.

## 9. Gestion des clés de paiement

| Variable | Rôle |
|---|---|
| `PAIEMENT_FOURNISSEUR` | `geniuspay` en production ; toute autre valeur, `simule` compris, est refusée |
| `GENIUSPAY_API_KEY`, `GENIUSPAY_API_SECRET` | Clés du compte marchand GeniusPay (clé publique `sk_live_…` et clé secrète `ss_live_…` en production ; préfixes relevés sur le tableau de bord GeniusPay, dont la documentation annonce `pk_…` / `sk_…` ; ceux de `live` restent à confirmer) ; exigées seulement si `PAIEMENT_FOURNISSEUR=geniuspay` |
| `GENIUSPAY_WEBHOOK_SECRET` | Secret de signature des webhooks (`whsec_live_…` ou `whsec_sandbox_…` d'après la documentation ; préfixe non contrôlé) ; exigé avec `geniuspay` |
| `GENIUSPAY_ENVIRONNEMENT` | `live` en production, `sandbox` en staging ; exigé avec `geniuspay` |
| `ANITCHE_STAGING_SANDBOX_PAIEMENT` | Staging seulement : exactement `true` ou `false`, ou absente (= `false`) |
| `GENIUSPAY_API_URL`, `GENIUSPAY_TIMEOUT` | Facultatifs (`https://geniuspay.ci/api/v1/merchant` ; 10 s) ; non transmis par `docker-compose.prod.yml` |
| `BACKEND_BASE_URL` | Origine HTTPS publique de Django, sans chemin ni « / » final : `https://anitche.com` en production (URL de notification) |

- **Jamais dans le code ni dans le dépôt.** Transmises comme `FIELD_ENCRYPTION_KEYS` : `infra/.env` (hors dépôt) → `docker-compose.prod.yml` (Django, worker et beat Celery) ; `infra/scripts/check_prod_env.sh` bloque le déploiement si elles manquent (celles du seul fournisseur actif), si le fournisseur n'est pas dans la liste (`simule` compris), si la clé est de sandbox ou si `BACKEND_BASE_URL` n'est pas en HTTPS ; `prod.py` refuse de démarrer dans les mêmes cas, et aussi si l'hôte de `BACKEND_BASE_URL` n'est pas dans `ALLOWED_HOSTS` (Django rejetterait chaque notification en 400) ou si elle contient un chemin.
- **URL de notification en production** : `https://anitche.com/api/paiements/webhook/geniuspay/` (déclarée dans le tableau de bord GeniusPay ou par `GET/PUT/DELETE /webhooks`) ; la route de transfert `.../webhook/geniuspay/transfert/` répond 401 tant que GeniusPay n'implémente pas les transferts. Même domaine et même certificat que le site : nginx (`location /api/` de `infra/nginx/nginx.conf`) transmet le chemin complet à Django, qui l'adresse à `WebhookPaiementView` ou `WebhookTransfertView`. Aucun sous-domaine `api.`.
- Jamais journalisées (seuls les codes d'erreur du fournisseur le sont) ; le jeton Bearer vit dans le cache Redis (clé dérivée d'une empreinte de la clé API).
- **Rotation** : générer un nouveau couple dans le tableau de bord du fournisseur (GeniusPay), mettre à jour `infra/.env`, redémarrer Django et Celery ; GeniusPay n'a pas de jeton en cache, ses clés partent à chaque appel.
- En dev : laisser `PAIEMENT_FOURNISSEUR` vide (simulé). Pour essayer la sandbox : `PAIEMENT_FOURNISSEUR=geniuspay`, `GENIUSPAY_ENVIRONNEMENT=sandbox`, clés `sk_sandbox_…` / `ss_sandbox_…`, et un `BACKEND_BASE_URL` joignable par GeniusPay (tunnel) ; pas à pas : `docs/ESSAI_GENIUSPAY_SANDBOX.md`.

### GeniusPay et mode staging

En production réelle, GeniusPay n'accepte ni clé ni environnement de sandbox. Le staging, qui teste en sandbox, l'autorise par **une seule variable explicite**, `ANITCHE_STAGING_SANDBOX_PAIEMENT`, qui ne s'applique **qu'à GeniusPay**.

| `ANITCHE_STAGING_SANDBOX_PAIEMENT` | `GENIUSPAY_ENVIRONNEMENT` | Clés | Résultat |
|---|---|---|---|
| absente ou `false` | `live` | `sk_live_…` / `ss_live_…` | accepté (production) |
| absente ou `false` | `sandbox` | quelles qu'elles soient | refusé (sandbox réservé au staging) |
| absente ou `false` | `live` | une clé de sandbox ou sans préfixe attendu | refusé |
| `true` | `sandbox` | `sk_sandbox_…` / `ss_sandbox_…` | accepté (staging) |
| `true` | `live` | quelles qu'elles soient | refusé (jamais combiné à des clés live) |
| `true` | `sandbox` | une clé publique `sk_live_…` ou une clé secrète `ss_live_…` | refusé |
| `True`, `1`, `yes`, vide… | | | refusé : valeur exacte `true` ou `false` seulement |

- **Absente** : `docker-compose.prod.yml` transmet `false` (`${ANITCHE_STAGING_SANDBOX_PAIEMENT-false}`) ; **présente mais vide** : la valeur vide est transmise telle quelle et `prod.py` refuse de démarrer, jamais de conversion tolérante. Dans `infra/.env.example` elle est commentée.
- Les variables `GENIUSPAY_*` et la variable de staging sont transmises à Django, au worker et à beat.
- `check_prod_env.sh` exige les variables `GENIUSPAY_*` et applique les mêmes cas que `prod.py` ; `infra/ci/verifier_environnement.sh` le rejoue sur chaque ligne du tableau.
- La CI de la pile de production tourne avec `PAIEMENT_FOURNISSEUR=geniuspay` et des valeurs factices au bon format (`sk_live_…`, `ss_live_…`, `GENIUSPAY_ENVIRONNEMENT=live`) ; `infra/ci/verifier_environnement.sh` rejoue aussi le refus de `cinetpay`. Elle vérifie en plus que `POST /api/paiements/webhook/geniuspay/` et `.../geniuspay/transfert/` sans signature, à travers nginx, répondent **401** « Notification non authentique » (`infra/ci/smoke_prod.sh`).

## 10. Impact frontend

1. **Paiement en ligne uniquement.** Retirer l'option « payer à la livraison » : `methode: "espece_livraison"` → **400** (`errors.methode`). Proposer Wave, Orange Money, MTN MoMo, Moov Money, carte bancaire.
2. **Redirection.** Après `POST /api/paiements/initier/` (**201**), rediriger vers `url_paiement` (page du fournisseur ; en dev, `…/paiement/simulation/<reference>` : page du portail qui appelle `POST /api/paiements/simulation/<reference>/`, § 8). Le portail client envoie **toujours** `?perimetre=achats` sur `GET /api/paiements/` et `GET /api/paiements/<id>/` (un administrateur qui achète n'y voit alors que les siens). Au retour sur `/paiement/retour?reference=…`, retrouver le paiement par `GET /api/paiements/?reference=<référence>` (le `sessionStorage` est vide si le fournisseur rouvre un autre onglet), puis **ne jamais conclure du retour navigateur** : interroger `GET /api/paiements/<id>/` jusqu'à `statut` = `valide` ou `echoue` (la notification serveur fait foi).
3. **Réponse allégée.** `metadata` et `transaction_id_externe` ne sont plus renvoyés au client. Nouveaux champs : `commandes` (liste d'identifiants), `remboursements`.
4. **Changer de moyen.** Nouveau `POST /api/paiements/<id>/annuler/` puis nouvelle initiation. Sans annulation, une seconde initiation renvoie **400** « Un paiement est déjà en cours… ».
5. **Erreurs à prévoir.** **502** « service de paiement momentanément indisponible » (proposer de réessayer) ; **429** au-delà de 20 initiations/annulations par heure ; **400** montant hors limites (plus de 2 500 000 FCFA).
6. **Plus de statut `a_rembourser`.** Afficher `remboursements[].statut` (`a_traiter`, `effectue`, `refuse`) sur la commande ou le paiement.
7. **Espace vendeur.** Nouvel écran « Mes reversements » : `GET /api/paiements/vendeur/reversements/` (ce qui a été vendu, frais déduits, net, date de disponibilité) et le résumé `…/resume/`. **Commission 14 % (septembre 2026)** : afficher les frais de chaque ligne tels que l'API les renvoie (`lignes[].taux_commission`, `frais_fixe_unitaire` : 100 ou 200 FCFA selon le prix de l'article, `montant_commission`, `montant_frais_fixes`), avec la mention « TVA incluse » ; ne jamais les recalculer côté interface (une commande passée avant le changement affiche 12 % et 200 FCFA). Détail : [`GUIDE_FRONTEND.md`](./GUIDE_FRONTEND.md) § 13.
8. **Back-office.** Écrans remboursements à traiter, reversements disponibles (versement manuel ou transfert), barèmes de frais (§ 4), avec les deux champs facultatifs `seuil_petit_article` et `frais_fixe_petit_article` (à remplir ensemble ou pas du tout). Barème commencé : formulaire en lecture seule, avec pour seule action « Clôturer » (`PATCH` de `date_fin`) ; pas de bouton « Supprimer » (409). Création et reprogrammation : sélecteur de `date_debut` sans date passée (« maintenant » ou plus tard). Sur un 400 ou un 409, se fier à `errors.code[0]` (`"bareme_deja_applique"` ou `"date_debut_passee"`), pas au texte.
9. **KYC.** Retirer `compte_bancaire` du formulaire (ignoré s'il est envoyé).
10. **Format d'erreur unifié (septembre 2026).** Le **502** (fournisseur indisponible), les **409** (annulation d'un paiement qui n'est plus en attente, remboursement ou reversement déjà traité) et le **404** du résumé des reversements suivent le format commun `{success: false, status_code, detail, errors}`, `errors` toujours un objet clé → liste de messages (`{}` si aucun champ n'est en cause). **Exception** : les notifications des fournisseurs (`webhook/<fournisseur>/`, `webhook/<fournisseur>/transfert/`) ne sont pas appelées par le frontend et répondent au format attendu par le fournisseur : `{"message": …}` en 200, `{"erreur": …}` sinon (les 429 de leur limite de débit suivent le format commun).

## 11. Sécurité — risques couverts

| Risque | Protection | Test |
|---|---|---|
| Même commande payée seule puis via son groupe | Refus si une commande est couverte par un paiement actif | `test_commande_payee_seule_puis_via_son_groupe_refusee` |
| Double encaissement sans remboursement | `Remboursement` `paiement_en_double` | `test_second_paiement_d_une_commande_deja_payee_rembourse` |
| Initiations simultanées | Commandes verrouillées à l'initiation | `test_initiations_concurrentes_un_seul_paiement` (Postgres, concurrence) |
| Webhook d'un autre fournisseur accepté | Paiement cherché avec son `fournisseur` | `test_notification_d_un_autre_fournisseur_refusee` |
| Devise non contrôlée | Montant **et** devise recontrôlés | `test_devise_ou_montant_differents_refuses` |
| Notification réécrivant les commandes payées | Relation `commandes` ; notifications jamais fusionnées dans `metadata` | `test_une_notification_ne_change_jamais_les_commandes_payees` |
| Deux succès simultanés validés deux fois | Validation sous verrou, idempotente | `test_succes_concurrents_une_seule_validation` (Postgres, concurrence) |
| Annulation partielle d'un groupe : tout « à rembourser » | Remboursement par commande, paiement toujours validé | `test_annulation_partielle_d_un_groupe_paye` |
| `metadata` exposée au client | Sérialiseur client explicite | `test_reponse_client_sans_donnees_internes` |
| Succès après remboursement revalidé | Transitions contrôlées, `valide` final | `test_paiement_valide_jamais_revalide_ni_echoue` |
| Webhooks bloqués par la limite anonyme | Limite dédiée par IP | `test_notifications_non_soumises_a_la_limite_anonyme` |
| Initiation sans limite dédiée | Scope `paiements` | `test_initiation_limitee` |
| Aide de simulation exposée en production | Route non montée (réglage figé à `False` dans `prod.py`), 404 hors fournisseur simulé, payeur seulement | `test_simulation_jamais_montee_en_production`, `test_simulation_figee_a_false_dans_prod`, `SimulationPaiementTests` |
| Paiement d'un autre compte retrouvé par sa référence | Filtre `?reference=` appliqué après `paiements_visibles` | `test_paiement_d_un_autre_compte_jamais_renvoye` |
| Un administrateur qui achète voit tous les paiements (et leurs données internes) dans son portail client | `?perimetre=achats` restreint à `client=user` et à la représentation client ; il ne fait que restreindre | `PerimetreAchatsTests` |
| Interblocage webhook de succès contre expiration (Paiement → Commande d'un côté, Commande → Paiement de l'autre) : webhook en 500, argent encaissé, paiement et commande annulés, aucun remboursement | Ordre unique Commandes → Paiement (§ 3) ; l'expiration rattrape `OperationalError` commande par commande | `ReconciliationConcurrenceTests.test_webhook_de_succes_…` (Postgres, vraie route webhook, deux ordres d'arrivée) ; `commandes` : `test_interblocage_sur_une_commande_n_arrete_pas_les_suivantes` |
| Webhook perdu : client débité, sans commande ni remboursement | Vérification avant expiration et tâche planifiée (§ 8, « Réconciliation ») | `ReconciliationAvantExpirationTests`, `ReconciliationPlanifieeTests`, `ReconciliationConcurrenceTests` |
| Transfert parti mais délai dépassé : reversement remis `disponible`, vendeur payé deux fois | Seuls les refus certains rendent `disponible` ; toute autre erreur laisse `en_cours` (§ 7) | `TransfertIncertainTests` |
| Reversement bloqué `en_cours` pour toujours (notification perdue) | Réconciliation planifiée, alerte de blocage, résolution manuelle (§ 8, « Réconciliation des reversements ») | `ReconciliationReversementTests`, `ResolutionManuelleTests`, `ReconciliationGeniusPayTests` |
| Succès tardif sur un reversement déjà versé à la main ou rendu disponible | Aucune transition, alerte « double paiement possible » | `TransfertIncertainTests` |
| Réconciliation et notification simultanées : versé deux fois, vendeur notifié deux fois | Application sous verrou, statut relu | `ReconciliationReversementConcurrenceTests` (Postgres, concurrence) |

Également : `Paiement.client` en PROTECT ; fausses URL de paiement supprimées (plus rien de factice en production) ; `is_staff` sans pouvoir (API et Django admin en lecture seule) ; montant toujours serveur ; IDOR (initier, consulter, annuler) testés ; barème déjà appliqué ni modifié (sauf sa clôture) ni supprimé, et aucun barème créé ou reprogrammé dans le passé, quel que soit le chemin de l'API (§ 4).

## 12. Limites de débit et performance

| Scope | Taux | Endpoints | Clé |
|---|---|---|---|
| `paiements` | 20/h | `initier/`, `<id>/annuler/`, `simulation/<reference>/` (dev) | utilisateur |
| `webhook_paiement` | 3000/h | `webhook/…` | IP |

- Listes sans N+1 : 4 requêtes pour la liste des paiements, 3 pour les reversements vendeur, quel que soit le nombre (testé).
- Aucun appel réseau vers le fournisseur n'est fait sous verrou ou dans une transaction ; délai d'attente de 10 s. Cela vaut aussi pour la réconciliation : au plus `PAIEMENT_RECONCILIATION_LOT` appels toutes les 10 minutes, plus un appel par commande expirée qui a un paiement en attente, et plus aucun appel dans l'exécution dès que le fournisseur est injoignable.
- Le traitement d'une notification fait un appel de vérification synchrone (le fournisseur attend une réponse rapide) : à passer en tâche Celery si les mesures le justifient (§ 15).

## 13. Migrations

- **paiements 0003** : `Paiement.commandes`, `fournisseur` (existants : `simule`), `hash_jeton_notification`, `client` en PROTECT, statuts réduits ; modèles `Remboursement`, `BaremeFrais`, `Reversement`, `AjustementVendeur` ; journal `recu`.
- **paiements 0004** (données) : reprise de `metadata.commandes_couvertes` dans la relation ; `a_rembourser` → validé + `Remboursement` ; paiements `espece_livraison` encore actifs → annulés ; clés internes retirées de `metadata` ; barème 12 % + 200 FCFA créé (clôturé par 0008).
- **commandes 0006** : frais figés sur `CommandeItem` (ventes passées : aucun frais, net = prix de la ligne).
- **utilisateurs 0009** : suppression de `compte_bancaire`.
- **paiements 0006** (frais de livraison) : `Reversement.montant_livraison` (0 pour l'existant) ; `AjustementVendeur.nature` (`retour` pour l'existant) et `commande`, contrainte « un ajustement de chaque nature par commande » (hors `retour`).
- **paiements 0007** : `BaremeFrais.seuil_petit_article` et `frais_fixe_petit_article` (vides pour l'existant : comportement inchangé), contrainte `bareme_frais_petit_article_complet_ou_absent` ; textes d'aide « TVA incluse ».
- **paiements 0008** (données, 28/09/2026) : le barème de la plateforme en vigueur est clôturé (`date_fin` = maintenant, rien d'autre) et le barème **14 % + 100 / 200 FCFA (seuil 3 000 FCFA), TVA incluse** créé au même instant, avec un identifiant fixe (`3f0c1a52-7d4e-4b8a-9c14-0e5b2d6a8f71`) : rejouée, elle ne fait rien. Barèmes des boutiques non touchés. Retour arrière : le barème 14 % est supprimé (aucune ligne n'y fait référence, les frais sont copiés dans `CommandeItem`) et le barème clôturé retrouve une `date_fin` vide. Limite : si ce barème avait une `date_fin` future avant la migration, le retour arrière la vide (aucun cas en base de dev).

- **paiements 0009** (octobre 2026) : `Paiement.date_derniere_reconciliation`, nullable, vide pour l'existant. Ajout de colonne sans valeur par défaut ni réécriture de table. Retour arrière : la colonne est supprimée (on perd seulement l'ordre de rotation de la réconciliation).
- **paiements 0010** : `Reversement.date_debut_transfert`, `date_derniere_reconciliation`, `date_derniere_alerte`, nullables. Données : les reversements déjà `en_cours` reçoivent `date_debut_transfert` = `date_mise_a_jour` (la réconciliation les prend donc tout de suite, l'alerte de blocage aussi s'ils sont anciens) ; les autres restent vides. Retour arrière : colonnes supprimées.
- **notifications 0004** : texte d'aide de `lien_redirection` (route `/administration/reversements`), sans SQL.

Base de dev (28/09/2026, migrée, ramenée à 0006, puis migrée de nouveau) : 1 barème de plateforme 12 % + 200 FCFA clôturé, 1 barème 14 % en vigueur, aucun barème de boutique ; les 28 articles déjà vendus gardent 12 % + 200 FCFA (40 680 FCFA de commission, 10 200 de frais fixes), les 18 reversements sont inchangés.

Base de dev (26/09/2026) : 6 paiements, tous `annule` après migration (5 `espece_livraison`, 1 `wave`), chacun relié à sa commande ; 1 seul `compte_bancaire` en base, vide (aucune perte).

## 14. Tests

⚠️ **Toujours sur PostgreSQL** (sous SQLite, les tests de concurrence ne prouvent rien).

```bash
cd backend-django
DJANGO_SETTINGS_MODULE=config.settings.ci DB_NAME=anitche_test DB_USER=postgres DB_PASSWORD=postgres DB_HOST=localhost \
  python manage.py test apps.paiements -v 2
```

`apps/paiements/tests.py` — 243 tests. Reversements en cours (44 tests) : `FournisseurSimuleTransfertTests` (comportements du transfert simulé), `TransfertIncertainTests` (délai dépassé, 5xx, erreur non classée → `en_cours` et 200 ; refus certains → `disponible` et 409 ; relance refusée ; `ECHEC` immédiat ; succès tardif après un versement manuel ou un retour à `disponible` → alerte, même transfert → silencieux ; succès non conforme → alerte ; suivi remis à zéro), `MigrationSuiviTransfertTests` (0010 dans les deux sens), `ReconciliationReversementTests` (succès, échec, en attente, introuvable, injoignable, fournisseur inconnu, âge, lot et rotation, idempotence, tâche planifiée, `--lecture-seule`, alertes de blocage et répétition à 24 h, `simuler_etat_paiement REV-…`), `ResolutionManuelleTests` (`non-parti`, `verse`, refus : motif vide, vendeur, client, compte inactif, sans référence, hors `en_cours`), `ReconciliationGeniusPayTests` (non disponible : daté, sans appel réseau), `ReconciliationReversementConcurrenceTests` (Postgres : deux réconciliations, réconciliation contre notification, échec réconcilié puis succès notifié). Périmètre « mes achats » (3 tests, `PerimetreAchatsTests`) : un administrateur voit 2 paiements sans paramètre et le sien seul avec `perimetre=achats` (sans `fournisseur`, représentation client), la référence d'un autre compte donne 0 résultat et son détail 404, client et valeur inconnue inchangés. Liens des notifications : remboursement à traiter (`/administration/remboursements`), remboursement effectué (`/paiements/{uuid}`), reversement effectué (`/vendeur/reversements`). Recherche par référence (5 tests, `RechercheParReferenceTests`) : propriétaire et administration la retrouvent, autre client → liste vide, référence inconnue ou trop longue, sans paramètre ou vide → liste complète, nombre de requêtes inchangé (4). Aide de simulation (10 tests) : `SimulationPaiementTests` (succès → `valide`, commande confirmée, signal émis une fois, aucun `JournalWebhook` ; échec ; second appel → 409 sans réécrire l'état distant ; statut absent ou inconnu → 400 ; autre client, administration, référence inconnue → 404 ; anonyme → 401 ; `PAIEMENT_FOURNISSEUR` autre que `simule` ou paiement d'un autre fournisseur → 404 ; route absente quand le réglage est faux), `ConfigurationProductionTests` (`prod.py` fige le réglage à `False` ; avec `config.settings.prod` et `PAIEMENT_SIMULATION_API_ACTIVE=True` dans l'environnement, la route n'est pas montée). Classement des réponses du fournisseur et annulation pendant la validation (5 tests) : `ReconciliationRefusEtPanneTests` (fournisseur simulé : une transaction introuvable pour P1 n'empêche pas l'expiration de valider P2 encaissé, P1 daté et journalisé puis expiré après le délai de grâce ; une panne arrête la tâche et l'expiration sans dater P1 ni demander P2), `GeniusPayTests.test_classement_des_reponses_du_get` (404 `TRANSACTION_NOT_FOUND` → `TransactionIntrouvable` ; autres réponses, délai dépassé, connexion, TLS → `ErreurFournisseur`), `ReconciliationPlanifieeTests` (même règle pour le fournisseur simulé, état `introuvable` de `simuler_etat_paiement`), `ReconciliationConcurrenceTests.test_annulation_par_le_client_pendant_la_validation_du_paiement` (annulation par le client sur la vraie route pendant le webhook de succès : fiche de livraison `annulee`, un seul remboursement, stock restitué une fois, reversement annulé ; jamais de fiche restée `en_attente`). Réconciliation et ordre des verrous (19 tests) : avant expiration (succès → confirmée, échec → expirée, en attente → délai de grâce puis expiration, délai configurable, fournisseur injoignable appelé une fois, commande sans paiement sans appel), tâche planifiée (webhook perdu puis validation, échec, en attente, arrêt propre si injoignable, paiement annulé puis succès → un seul remboursement, sans `transaction_id_externe` ou hors fenêtre de 24 h ignoré, montant ou devise incohérents journalisés, commande `simuler_etat_paiement`, lot limité et rotation), concurrence PostgreSQL (webhook de succès sur la vraie route contre la tâche d'expiration, dans les deux ordres d'arrivée ; réconciliation et webhook simultanés ; tâche lancée deux fois en parallèle). Autres tests : initiation (montant serveur, moyens acceptés, paiement à la livraison refusé, IDOR, boutique indisponible, fournisseur injoignable, annulation et relance, `is_staff`, N+1), notifications (signature manquante, invalide, expirée, rejeu, fournisseur, montant et devise, vérification non finalisée, 503 puis rejeu, limites), remboursements (paiement après expiration, double paiement, annulation partielle, traitement admin), concurrence (initiations et succès simultanés), frais (barème par défaut 14 % TVA incluse et ancien barème clôturé, arrondi au demi-franc supérieur, plafond, barème sans seuil inchangé, seuil à 2 999 / 3 000 / 3 001 FCFA, promotion qui passe sous le seuil, quantité supérieure à 1 jusqu'au reversement, figés, offre de lancement, barème de boutique prioritaire, seuil et frais réduit ensemble en base et à l'API, coupon, 503 sans barème, non exposés, administration), migration 0008 dans les deux sens (commande d'avant gardant 12 % + 200 FCFA, barème de boutique intact, idempotence), barème déjà appliqué (`PATCH` d'un autre champ refusé avec `bareme_deja_applique` et sans effet, valeur identique acceptée, `PUT` soumis à la même règle, clôture maintenant ou plus tard, horloge du client en retard, clôture passée ou réouverture refusées, barème déjà clôturé figé, `DELETE` refusé en 409, barème programmé modifiable et supprimable, non-administrateur 403 et anonyme 401, Django admin en lecture seule), date de début (création dans le passé refusée avec `date_debut_passee`, « maintenant » ramené à l'heure du serveur, création future, sans date : commence à l'enregistrement, sans date avec `date_fin` passée : 400 et non 500, reprogrammation d'un barème programmé vers le passé refusée par `PATCH` et `PUT`, vers une autre date future ou « maintenant » acceptée), reversements (cycle de vie, 7 jours, retour ouvert/rejeté, retour remboursé avant et après versement, ajustement imputé, versement manuel, transfert immédiat, en attente puis notifié, échoué, vue vendeur), adaptateur GeniusPay (`GeniusPayTests` : initiation, signature, vérification par le `GET`, montant et devise, environnement, réconciliation, transfert non pris en charge et versement manuel), configuration de production (simulé, sandbox, clés, HTTPS). Fournisseurs en production : `config/tests_settings_prod.py` (`PaiementProductionTests`, sous-processus avec des valeurs factices : liste fermée, clés GeniusPay manquantes ou incohérentes, environnement, mode staging, valeurs non exactes de la variable, fournisseur `cinetpay` refusé, aucun secret dans l'erreur) ; contrôle shell : `infra/ci/verifier_environnement.sh`.

Postman : `postman_paiements.json` (hors dépôt, reconstruite) — mise en place automatique (vendeur, produit, clients, OTP via Mailpit), parcours nominal (checkout → paiement → notification simulée signée → commande confirmée → reversement), scénarios de sécurité (IDOR, signature manquante/fausse/expirée, montant et devise falsifiés, rejeu, autre fournisseur, paiement à la livraison), annulation et relance, vue vendeur, administration (`admin_password` à renseigner ; dont [SEC] : modifier le taux du barème en vigueur → 400 `bareme_deja_applique`, taux inchangé ensuite). L'ancienne collection est archivée dans `postman_archives/`.

## 15. Dette connue

- **Abonnement selon le volume de ventes** (modèle Jumia) : non implémenté, **0 FCFA au lancement**. Forme prévue : un barème d'abonnement mensuel par tranche de chiffre d'affaires, prélevé sur les reversements du mois (ajustement négatif). À concevoir quand le volume le justifie.
- **TVA sur la commission** : **tranché, TVA incluse** (septembre 2026) : 14 % + 100 / 200 FCFA sont des montants toutes taxes comprises, rien ne s'y ajoute (comme Jumia, qui affiche des commissions TTC). **Point ouvert** : la décomposition HT / TVA (taux de **18 % à confirmer avec un comptable**) le jour où ANITCHE émettra des factures de commission aux vendeurs. Rien n'est calculé ni stocké pour l'instant ; ce jour-là, la TVA se déduira des montants figés (TVA = TTC × taux / (100 + taux)) et la règle d'arrondi sera à fixer avec le comptable.
- **Bascule vers GeniusPay** (décision du 11 octobre 2026, § 16) : essai réel en sandbox réussi (`docs/ESSAI_GENIUSPAY_SANDBOX.md` § 7) et `PAIEMENT_FOURNISSEUR=geniuspay` par défaut. Restent : l'ouverture du compte `live` (clés et secret de webhook de production, préfixes à confirmer) et les transferts vers les vendeurs (non implémentés chez GeniusPay : versement manuel d'ici là).
- **Remboursement et transfert automatiques** : manuels au lancement ; API de remboursement et de transfert à brancher quand le fournisseur les propose. L'adaptateur de transfert devra : envoyer `reversement.reference` comme référence marchande (clé d'idempotence), lever `TransfertRefuse` pour les seuls refus certains, ne renvoyer `ECHEC` que pour un statut final explicite (pour un payout, `cancelled` et `expired` restent en attente tant que le support ne les a pas confirmés finaux), et implémenter `verifier_transfert` par cette référence (§ 8, « Réconciliation des reversements »).
- **Notification traitée de façon synchrone** : passer en tâche Celery si le temps de réponse de la vérification le justifie (mesure en sandbox).
- **Réconciliation, à confirmer en sandbox GeniusPay** : la politique de rejeu des notifications (hypothèse non vérifiée), le délai après lequel une transaction non payée passe `expired`, la limite de débit de `GET /payments/{référence}`. Selon ces mesures, ajuster `PAIEMENT_RECONCILIATION_*` (§ 8).
- **Module retours** (vu a minima) : un vendeur peut encore marquer un retour « remboursé » (seul l'argent reste entre les mains de l'administration, via `Remboursement`) ; le délai de rétractation de 7 jours n'y est pas appliqué ; à traiter avec ce module.
- **Opérateur Wave** non déductible du numéro pour un transfert : à préciser par l'admin (`operateur: "wave"`).

## 16. Justification des choix

### Choix du fournisseur : GeniusPay (décision du 11 octobre 2026)

**Décision** : GeniusPay est le fournisseur d'encaissement d'ANITCHE.

État du code :

- l'adaptateur GeniusPay existe (encaissement seulement, § 8) et `prod.py` l'autorise (§ 8, « Liste fermée de production ») ;
- **`PAIEMENT_FOURNISSEUR=geniuspay`** par défaut dans `infra/.env.example` et dans la CI : l'essai réel en sandbox est réussi (`docs/ESSAI_GENIUSPAY_SANDBOX.md` § 7) ;
- à faire ensuite, dans cet ordre : ouverture du compte `live`, transferts vers les vendeurs (versement manuel d'ici là).

Les motifs de la décision ne sont pas consignés dans ce document : à compléter par l'équipe. Les options étudiées en septembre 2026 restent ci-dessous comme référence (frais relevés sur les sites des fournisseurs et des articles qui les reprennent ; **à confirmer par devis**) :

| Option | Frais d'encaissement relevés | Remarques relevées |
|---|---|---|
| GeniusPay | 1 % + 100 XOF **en plus** des frais de la passerelle utilisée : Wave 2,5 % + 100, mobile money via Paystack 4,5 % + 100, carte 6 % + 100 | Surcouche technique qui s'appuie elle-même sur CinetPay, Paystack ou PawaPay ; service alors **en bêta sur liste d'attente**, présenté comme « service d'intégration technique » et non comme établissement de paiement |
| PayDunya | 1,5 à 2,2 % selon le volume (source secondaire) | Plus centré sur le Sénégal ; frais ivoiriens non publiés officiellement. **Gardé en réserve** : l'architecture permet de l'ajouter (§ 8) |
| Intégrations directes (Wave, Orange Money, MTN MoMo, Moov, + acquéreur carte) | Wave environ 1 % (plafonné à 5 000 FCFA) ; les autres sur contrat | 4 à 5 contrats, 4 à 5 formats de notification et de vérification à maintenir, et un acquéreur carte en plus : disproportionné au lancement (CLAUDE.md : ne pas sur-concevoir) |

Sources : [GeniusPay – tarifs](https://pay.genius.ci/pricing), [Kolonell – passerelles en Côte d'Ivoire (PayDunya)](https://kolonell.com/fr/blog/passerelle-paiement-cote-divoire-wave-orange-mtn-2026), [Kolonell – Wave marchand en Côte d'Ivoire](https://kolonell.com/fr/blog/wave-cote-ivoire-integration-marchand-abidjan-2026), [Paystack – tarifs Côte d'Ivoire](https://paystack.com/ci/pricing).

### Barème : 14 % + 100 FCFA par article jusqu'à 3 000 FCFA, 200 FCFA au-delà, TVA incluse

Décision d'équipe du 28 septembre 2026 ; elle remplace le barème de départ, 12 % + 200 FCFA par article, dont le traitement de la TVA n'avait pas été tranché.

- **Même modèle que Jumia Côte d'Ivoire**, que les vendeurs connaissent : commission en % + frais fixe **par article**, prélevés à la livraison, plus un abonnement par tranche de chiffre d'affaires (5 000 FCFA entre 100 000 et 1 499 999 FCFA, 20 000 FCFA au-delà).
- **Compétitif** : Jumia prend **15 %** dans son exemple de la catégorie mode (commissions TTC, variables selon la catégorie) plus son frais fixe et son abonnement ; ANITCHE prend **14 % TVA incluse**, un frais fixe bas et **sans abonnement**.
- **Frais fixe réduit pour les petits articles** : un frais fixe pèse d'autant plus que l'article est bon marché (200 FCFA font 10 % d'un article à 2 000 FCFA, 100 FCFA en font 5 %).
- **Couvre les coûts** (frais de GeniusPay relevés en octobre 2026, à confirmer par devis) : l'encaissement (1 % + 100 FCFA de GeniusPay, plus 2,5 % + 100 pour Wave ou 6 % + 100 pour la carte) reste sous la commission, et le versement manuel n'a pas de frais de transfert ; le frais fixe couvre la part fixe de traitement d'une commande. Sur un article à 5 000 FCFA : 900 FCFA de frais pour le vendeur (700 de commission + 200 de frais fixe), contre au plus 550 FCFA d'encaissement par carte (6 % + 100 et 1 % + 100) côté ANITCHE. Sur un article à 2 000 FCFA : 380 FCFA (280 + 100), contre au plus 340 FCFA par carte.
- **Réglable sans déploiement** (barème administrable, offre de lancement par boutique) et **figé à la vente** : les vendeurs ne découvrent jamais un taux changé après coup ; les ventes passées avant le 28/09/2026 gardent 12 % + 200 FCFA.

Sources : [Jumia VendorHub CI – frais fixes 2026](https://vendorhub.jumia.ci/fr/frais-fixes-2026/), [Jumia VendorHub CI – commissions](https://vendorhub.jumia.ci/commissions/).

### Pourquoi 7 jours de rétractation avant reversement

- C'est la **fenêtre de retour de Jumia Côte d'Ivoire** (retour accepté dans les 7 jours suivant la réception) : la référence que clients et vendeurs connaissent, et la période où les retours arrivent.
- Le délai **légal** de rétractation applicable en Côte d'Ivoire (droit de la consommation, loi n° 2013-546 sur les transactions électroniques) n'a pas pu être vérifié dans le texte : **à confirmer par un juriste**. S'il est plus long, il suffit d'augmenter le réglage, et le module retours devra l'appliquer (§ 15).
- Tant que l'argent n'est pas versé, un retour se règle **en réduisant le reversement** : aucune créance à recouvrer auprès du vendeur. Après versement, il faut un ajustement sur le prochain reversement, qui n'existe pas toujours (vendeur inactif).
- Assez court pour la trésorerie des petits vendeurs ; **réglable** (`REVERSEMENT_DELAI_RETRACTATION_JOURS`).

Sources : [Jumia CI – politique de retour et remboursement](https://www.jumia.ci/sp-politique-de-retour-remboursement/), [loi n° 2013-546 (ARTCI)](https://www.artci.ci/images/stories/pdf/lois/loi_2013_546.pdf).

### Pourquoi mobile money uniquement

- C'est le moyen dominant en Côte d'Ivoire, y compris pour les petits vendeurs, souvent **sans compte bancaire** ; le numéro est déjà exigé et vérifié au KYC.
- **Versement sur le numéro mobile money du vendeur** : manuel avec GeniusPay, automatisable par l'API de transfert d'un fournisseur qui en propose, là où un virement bancaire prend plusieurs jours.
- **Minimisation des données** : ne plus collecter de coordonnées bancaires supprime une donnée sensible (chiffrée, mais à protéger, sauvegarder et justifier) sans usage.
