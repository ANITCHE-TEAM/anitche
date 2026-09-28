# Module paiements — contrat et règles

> Périmètre : backend Django, `backend-django/apps/paiements/` (et ses points de contact dans `commandes`, `livraison`, `retours`, `fidelite`, `notifications`, `utilisateurs`).
> État : refonte de septembre 2026. **Compte marchand CinetPay pas encore ouvert** (pas de RCCM) : tout fonctionne et se teste avec le **fournisseur simulé** ; les clés CinetPay seront branchées plus tard (§ 9).

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
Client ──paie en ligne (Wave, Orange Money, MTN MoMo, Moov Money, carte)──▶ CinetPay ──▶ compte ANITCHE
                                                                              │
     commande confirmée ─▶ Reversement « en attente de livraison »           │ ANITCHE conserve les fonds
     livraison confirmée ─▶ « délai de rétractation » (7 jours)             │
     délai écoulé        ─▶ « disponible »                                   │
     versement           ─▶ « versé » : prix − commission − frais fixes ─────┴──▶ mobile money du vendeur
```

- **Aucun paiement à la livraison** : le client paie avant ; une commande non payée expire à 30 minutes (`commandes`).
- **Tous les montants sont en FCFA entiers**, calculés côté serveur ; un montant envoyé par le client est ignoré.
- Argent à rendre au client : un **`Remboursement`** par commande (§ 6). Argent dû au vendeur : un **`Reversement`** par commande (§ 7).

## 3. Modèles

- **`Paiement`** : `reference` (`PAY-<année>-<10 hex>`, 30 caractères au plus), `client` (**PROTECT**), `commandes` (**relation** : les commandes réellement payées ; remplace la liste JSON `metadata.commandes_couvertes`), `commande` / `groupe_commande` (cible demandée, contrat API conservé), `methode` (moyen choisi par le client), **`fournisseur`** (qui encaisse : `simule`, `cinetpay`), `statut`, `montant`, `devise` (`XOF`), `transaction_id_externe`, `hash_jeton_notification` (empreinte SHA-256, jamais le jeton), `url_paiement`, `adresse_livraison`, `metadata` (motifs internes, jamais exposés, jamais alimentés par une notification).
- **Transitions** (`services.py`, sous verrou de ligne) : `en_attente → valide | echoue | annule` ; `echoue → valide` ; `annule → valide` (succès tardif) ; `valide` est final. Les statuts `a_rembourser` et `rembourse` n'existent plus.
- **`JournalWebhook`** : idempotence (`fournisseur`, `evenement_id` uniques) et audit ; statuts `recu`, `traite`, `ignore` (transaction non finalisée : rejouable), `erreur` (rejouable).
- **`Remboursement`** (§ 6), **`BaremeFrais`** (§ 5), **`Reversement`** et **`AjustementVendeur`** (§ 7).

## 4. Endpoints — `/api/paiements/`

### Client (`IsAuthenticated` ; ses paiements uniquement, 404 sinon)

| Méthode | Chemin | Rôle / réponses |
|---|---|---|
| POST | `initier/` | Corps : `commande_id` **ou** `groupe_commande_id`, `methode` (`wave`, `orange_money`, `mtn_money`, `moov_money`, `carte_bancaire`). **201** : le paiement, avec `url_paiement` vers la page du fournisseur. **400** : commande d'un autre client, déjà payée ou en cours de paiement, annulée, adresse manquante, boutique indisponible (commande annulée), `espece_livraison`, montant hors limites du fournisseur. **502** : fournisseur injoignable (le paiement passe `echoue`, un nouvel essai est possible). **429** : limite `paiements` |
| GET | `` | Ses paiements (administration : tous, avec `fournisseur`, `transaction_id_externe`, `metadata`) |
| GET | `<uuid>/` | Détail |
| POST | `<uuid>/annuler/` | **Nouveau.** Abandonne un paiement `en_attente` pour en relancer un autre (autre moyen). **200** ; **409** si le paiement n'est plus en attente. Un succès tardif de l'ancien devient un remboursement dû |

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
| POST | `admin/reversements/<uuid>/transferer/` | Versement par l'**API de transfert** du fournisseur ; `operateur` facultatif (déduit du numéro : 07 Orange, 05 MTN, 01 Moov ; Wave à préciser). **409** si refus |
| GET, POST | `admin/baremes/` | Barèmes de frais (§ 5), dont `seuil_petit_article` / `frais_fixe_petit_article` (facultatifs, ensemble, sinon **400**) |
| GET, PUT, PATCH, DELETE | `admin/baremes/<uuid>/` | Selon que le barème a commencé ou non (règle ci-dessous) |

**Règle des barèmes (septembre 2026)** : un barème commencé a pu s'appliquer à des ventes, son historique ne se réécrit donc plus, et un barème ne commence jamais dans le passé (il deviendrait rétroactivement « en vigueur » sur une période déjà vendue). Changer de barème, c'est **clôturer l'actuel puis en créer un nouveau**.

| Barème | `POST` (création) / `PUT` / `PATCH` | `DELETE` |
|---|---|---|
| Nouveau ou pas encore commencé (`date_debut` future) | tout se modifie, mais **`date_debut` maintenant ou plus tard**. Plus ancienne → **400**, `errors.code = ["date_debut_passee"]`, `errors.date_debut`. `date_debut` absente : à la création, le barème commence à l'instant de l'enregistrement (comportement conservé) ; en modification, elle ne change pas | **204** |
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
- **Traitement au lancement : manuel.** L'administration est alertée (journal `securite` + notification), rembourse depuis le **tableau de bord CinetPay**, puis saisit la référence (`admin/remboursements/<id>/traiter/`). Le client voit l'état dans `remboursements` de son paiement et reçoit une notification.
- **Plus tard** : `FournisseurPaiement.rembourser()` existe dans l'interface ; les SDK CinetPay publiés (août 2026) n'exposent pas d'API de remboursement, l'adaptateur répond donc « non supporté ».
- **Retour remboursé et vendeur** : la commission des articles retournés est **rendue au vendeur**, le **frais fixe reste acquis à ANITCHE** : sa part baisse de (prix − commission) des articles retournés (§ 7).

## 7. Reversements aux vendeurs

Un **`Reversement`** par commande confirmée : `reference` (`REV-…`), `boutique`, `montant_brut`, `montant_commission`, `montant_frais_fixes`, `montant_retours`, `montant_livraison` (livraison offerte par la boutique), `montant_net`, `montant_ajustements` (≤ 0), `montant_a_verser`, `statut`, `date_livraison`, `date_disponibilite`, `date_versement`, `canal` (`mobile_money`), `numero_destinataire` (**chiffré**, figé au versement), `operateur`, `fournisseur`, `reference_externe`, `verse_par`.

| Statut | Passage |
|---|---|
| `en_attente_livraison` | Paiement validé, commande confirmée |
| `en_retractation` | Livraison confirmée : `date_disponibilite` = livraison + **7 jours** (`REVERSEMENT_DELAI_RETRACTATION_JOURS`) |
| `suspendu` | Retour ouvert ou livraison contestée « non reçu » pendant le délai (ou reversement disponible pas encore versé) ; reprend quand plus aucun retour ni contestation n'est ouvert |
| `disponible` | Délai écoulé (tâche Celery horaire `rendre_reversements_disponibles`) |
| `en_cours` | Transfert demandé au fournisseur, en attente de sa confirmation |
| `verse` | Versé (manuellement avec référence, ou transfert confirmé) ; le vendeur est notifié |
| `annule` | Commande annulée avant versement |

- **Retour remboursé avant versement** : `montant_retours` augmente, `montant_net` est recalculé.
- **Retour remboursé après versement** : un **`AjustementVendeur`** négatif est créé ; il est **déduit du prochain reversement** de la boutique (du plus ancien au plus récent, tant que le montant versé reste ≥ 0 ; le reste attend le suivant).
- **Frais de livraison** ([`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) § 9) : payés par le client, ils reviennent à ANITCHE et n'entrent **jamais** dans le reversement. **Livraison offerte** par la boutique : `montant_net` = brut − commission − frais fixes − retours − `montant_livraison`, où `montant_livraison` = min(tarif figé, net des articles) ; si le tarif dépasse le net, le reste devient un `AjustementVendeur` à la livraison (une commande annulée avant ne coûte rien). Une contestation « non reçu » fondée efface ce reste tant qu'il n'est pas déduit d'un versement.
- **`AjustementVendeur.nature`** : `retour` (retour remboursé après versement, lié au retour), `livraison_offerte` (reste non couvert), `frais_livraison_retour` (frais de livraison rendus au client pour un retour imputable au vendeur). Au plus un ajustement de chacune des deux dernières natures par commande (contrainte en base) : rejouer le traitement ne facture jamais deux fois.
- **Canal : mobile money uniquement**, sur `DocumentKYC.numero_mobile_money` du vendeur (format ivoirien, converti en +225…). En conséquence, **`compte_bancaire` n'est plus collecté** (supprimé, migration utilisateurs 0009, voir `MODULE_UTILISATEURS.md`).
- **Au lancement : versement manuel** par l'administration ; ensuite **API de transfert CinetPay** (`transferer/`), limites 100 à 1 500 000 FCFA par transfert.

## 8. Fournisseurs de paiement

### Architecture

```text
apps/paiements/fournisseurs/
  base.py      interface FournisseurPaiement + types communs (SessionPaiement, Notification, EtatTransaction, ResultatTransfert)
  simule.py    fournisseur simulé (dev, tests, Postman) — refusé en production
  cinetpay.py  adaptateur CinetPay (API REST v1)
  __init__.py  registre ; fournisseur actif = PAIEMENT_FOURNISSEUR
```

Interface commune : `initier(paiement)`, `lire_notification(request)`, `authentifier_notification(notification, objet)`, `verifier_transaction(paiement, notification)`, `rembourser(remboursement)`, `transferer(reversement, telephone, operateur)`, `lire_notification_transfert`, `verifier_transfert`.

**Règle commune** : une notification n'est qu'un signal. Chaîne obligatoire, identique pour tous les fournisseurs (`services.traiter_notification_paiement`) : lire → le paiement doit avoir été ouvert **chez ce fournisseur** → authentifier → journal d'idempotence → **redemander l'état réel au fournisseur** → contrôler montant, devise et identifiants → transition sous verrou.

### Fournisseur simulé

Aucun argent ne circule ; la chaîne de sécurité est la même. Notification signée **HMAC-SHA256** de `"<horodatage>.<corps brut>"` avec `PAIEMENT_SIMULE_SECRET`, en-tête `X-Signature-Simulation: t=<unix>,v1=<hex>`, **fenêtre de 5 minutes** (rejeu refusé). Corps : `{"evenement_id", "reference", "transaction_id", "statut": "succes|echec|en_attente", "montant", "devise"}` (montant entier et devise obligatoires pour un succès). En dev, le secret vaut `dev-simulation-anitche` (public, repris par Postman) ; `prod.py` refuse ce fournisseur.

### Adaptateur CinetPay

Suivi des **SDK officiels CinetPay** ([cinetpay-python](https://github.com/cinetpay/cinetpay-python), [cinetpay-php-sdk v3.0.1, 11/08/2026](https://github.com/cinetpay/cinetpay-php-sdk)) :

| Étape | API |
|---|---|
| Authentification | `POST /v1/oauth/login` `{api_key, api_password}` → jeton Bearer, cache 23 h, renouvelé une fois sur `EXPIRED_TOKEN` / `INVALID_TOKEN` |
| Initiation | `POST /v1/payment` : `merchant_transaction_id` = notre référence, `amount` (100 à 2 500 000 FCFA), `currency`, `payment_method` (`WAVE_CI`, `OM_CI`, `MTN_CI`, `MOOV_CI` ; aucun pour la carte), URL de retour et de notification (≤ 120 caractères) → `payment_url`, `transaction_id`, `notify_token` |
| Notification | `{notify_token, merchant_transaction_id, transaction_id}` ; `notify_token` comparé à temps constant à l'empreinte stockée ; **le statut éventuellement transmis est ignoré** |
| Vérification | `GET /v1/payment/{merchant_transaction_id}`, systématique ; identifiants recoupés ; `SUCCESS` → validé, `FAILED`/`EXPIRED`/… → échoué, `INITIATED`/`PENDING` → rien |
| Transfert | `POST /v1/transfer`, vérification `GET /v1/transfer/{id}` |
| URL | sandbox `https://api.cinetpay.net` (clé `sk_test_…`), production `https://api.cinetpay.co` (`sk_live_…`) |

⚠️ **Écart avec la demande initiale (x-token HMAC).** L'en-tête `x-token` (HMAC-SHA256 sur `cpm_site_id + cpm_trans_id + …` avec la « secret key ») appartient à l'**ancienne API CinetPay** (`site_id` / `apikey`, `api-checkout.cinetpay.com/v2`). Son site de documentation (`docs.cinetpay.com`) ne répond plus (domaine inexistant, septembre 2026) et CinetPay a réécrit ses SDK en 2026 pour la nouvelle API (« Réécriture complète de l'ancien SDK PHP basé sur les API CinetPay V1/V2 legacy »), où l'authenticité repose sur le **`notify_token` propre à chaque transaction**, suivi de la vérification par l'API. L'intention (notification authentifiée **puis** revérifiée côté serveur) est respectée. Si le compte marchand ouvert par CinetPay est encore sur l'ancienne API, il suffit d'écrire un adaptateur `cinetpay_v2` (§ 8, « Ajouter un fournisseur ») qui vérifie le `x-token` dans `lire_notification` et appelle `/v2/payment/check` dans `verifier_transaction`.

**À confirmer en sandbox dès l'ouverture du compte** : présence de `amount` / `currency` dans la réponse de vérification (s'ils y sont, ils sont recontrôlés ; sinon le montant reste celui fixé à l'initiation côté serveur), format exact de la notification de transfert, activation de la carte bancaire sur le compte.

### Ajouter un fournisseur (PayDunya en réserve)

1. Créer `fournisseurs/paydunya.py` : une classe `FournisseurPayDunya(FournisseurPaiement)` avec `code = "paydunya"` et les méthodes de l'interface (sa signature de notification dans `lire_notification` / `authentifier_notification`, son API de vérification dans `verifier_transaction`).
2. L'ajouter à `FOURNISSEURS` (`fournisseurs/__init__.py`).
3. Ajouter ses clés dans `base.py` (depuis l'environnement, sans valeur par défaut), `prod.py` (exigées quand il est actif), `docker-compose.prod.yml`, `check_prod_env.sh`, `infra/.env.example`.
4. Tests : copier `CinetPayTests` (appels HTTP simulés au format de ses SDK).
5. Basculer : `PAIEMENT_FOURNISSEUR=paydunya`. Les paiements déjà ouverts chez l'ancien fournisseur restent notifiés sur son URL (`Paiement.fournisseur`).

Rien d'autre ne change : commandes, remboursements, reversements et frais ignorent le fournisseur.

## 9. Gestion des clés CinetPay

| Variable | Rôle |
|---|---|
| `PAIEMENT_FOURNISSEUR` | `cinetpay` en production (`simule` interdit) |
| `CINETPAY_API_KEY` | Clé du compte marchand Côte d'Ivoire (`sk_live_…` en production, `sk_test_…` interdite) |
| `CINETPAY_API_PASSWORD` | Mot de passe API associé |
| `BACKEND_BASE_URL` | Adresse HTTPS publique de l'API (URL de notification) |
| `CINETPAY_API_URL`, `CINETPAY_TIMEOUT` | Facultatifs (URL déduite de la clé ; 10 s) |

- **Jamais dans le code ni dans le dépôt.** Transmises comme `FIELD_ENCRYPTION_KEYS` : `infra/.env` (hors dépôt) → `docker-compose.prod.yml` (Django, worker et beat Celery) ; `infra/scripts/check_prod_env.sh` bloque le déploiement si elles manquent, si le fournisseur est `simule`, si la clé est de sandbox ou si `BACKEND_BASE_URL` n'est pas en HTTPS ; `prod.py` refuse de démarrer dans les mêmes cas.
- Jamais journalisées (seuls les codes d'erreur CinetPay le sont) ; le jeton Bearer vit dans le cache Redis (clé dérivée d'une empreinte de la clé API).
- **Rotation** : générer un nouveau couple dans le tableau de bord CinetPay, mettre à jour `infra/.env`, redémarrer Django et Celery ; le jeton en cache expire seul (ou vider la clé `cinetpay:jeton:*`).
- En dev : laisser `PAIEMENT_FOURNISSEUR` vide (simulé). Pour essayer la sandbox : `PAIEMENT_FOURNISSEUR=cinetpay`, clé `sk_test_…`, et un `BACKEND_BASE_URL` joignable par CinetPay (tunnel).

## 10. Impact frontend

1. **Paiement en ligne uniquement.** Retirer l'option « payer à la livraison » : `methode: "espece_livraison"` → **400** (`errors.methode`). Proposer Wave, Orange Money, MTN MoMo, Moov Money, carte bancaire.
2. **Redirection.** Après `POST /api/paiements/initier/` (**201**), rediriger vers `url_paiement` (page du fournisseur ; en dev, `…/paiement/simulation/<reference>` : pas de vraie page, le paiement se confirme par une notification simulée, cf. Postman). Au retour sur `/paiement/retour?reference=…`, **ne jamais conclure du retour navigateur** : interroger `GET /api/paiements/<id>/` jusqu'à `statut` = `valide` ou `echoue` (la notification serveur fait foi).
3. **Réponse allégée.** `metadata` et `transaction_id_externe` ne sont plus renvoyés au client. Nouveaux champs : `commandes` (liste d'identifiants), `remboursements`.
4. **Changer de moyen.** Nouveau `POST /api/paiements/<id>/annuler/` puis nouvelle initiation. Sans annulation, une seconde initiation renvoie **400** « Un paiement est déjà en cours… ».
5. **Erreurs à prévoir.** **502** « service de paiement momentanément indisponible » (proposer de réessayer) ; **429** au-delà de 20 initiations/annulations par heure ; **400** montant hors limites (plus de 2 500 000 FCFA).
6. **Plus de statut `a_rembourser`.** Afficher `remboursements[].statut` (`a_traiter`, `effectue`, `refuse`) sur la commande ou le paiement.
7. **Espace vendeur.** Nouvel écran « Mes reversements » : `GET /api/paiements/vendeur/reversements/` (ce qui a été vendu, frais déduits, net, date de disponibilité) et le résumé `…/resume/`. **Commission 14 % (septembre 2026)** : afficher les frais de chaque ligne tels que l'API les renvoie (`lignes[].taux_commission`, `frais_fixe_unitaire` : 100 ou 200 FCFA selon le prix de l'article, `montant_commission`, `montant_frais_fixes`), avec la mention « TVA incluse » ; ne jamais les recalculer côté interface (une commande passée avant le changement affiche 12 % et 200 FCFA). Détail : [`GUIDE_FRONTEND.md`](./GUIDE_FRONTEND.md) § 13.
8. **Back-office.** Écrans remboursements à traiter, reversements disponibles (versement manuel ou transfert), barèmes de frais (§ 4), avec les deux champs facultatifs `seuil_petit_article` et `frais_fixe_petit_article` (à remplir ensemble ou pas du tout). Barème commencé : formulaire en lecture seule, avec pour seule action « Clôturer » (`PATCH` de `date_fin`) ; pas de bouton « Supprimer » (409). Création et reprogrammation : sélecteur de `date_debut` sans date passée (« maintenant » ou plus tard). Sur un 400 ou un 409, se fier à `errors.code[0]` (`"bareme_deja_applique"` ou `"date_debut_passee"`), pas au texte.
9. **KYC.** Retirer `compte_bancaire` du formulaire (ignoré s'il est envoyé).
10. **Format d'erreur unifié (septembre 2026).** Le **502** (fournisseur indisponible), les **409** (annulation d'un paiement qui n'est plus en attente, remboursement ou reversement déjà traité) et le **404** du résumé des reversements portaient avant un simple `{"detail": …}` : ils suivent désormais le format commun `{success: false, status_code, detail, errors}`, `errors` toujours un objet clé → liste de messages (`{}` si aucun champ n'est en cause). **Exception** : les notifications des fournisseurs (`webhook/<fournisseur>/`, `webhook/<fournisseur>/transfert/`) ne sont pas appelées par le frontend et répondent au format attendu par le fournisseur : `{"message": …}` en 200, `{"erreur": …}` sinon (les 429 de leur limite de débit suivent le format commun).

## 11. Sécurité — failles corrigées (diagnostic de septembre 2026)

| # | Faille | Correction | Test |
|---|---|---|---|
| D01 | Même commande payée seule puis via son groupe | Refus si une commande est couverte par un paiement actif | `test_D01_…` |
| D01b | Double encaissement sans remboursement | `Remboursement` `paiement_en_double` | `test_D01b_…` |
| D02 | Initiations simultanées | Commandes verrouillées à l'initiation | `test_D02_…` (Postgres, concurrence) |
| D03 | Webhook d'un autre fournisseur accepté | Paiement cherché avec son `fournisseur` | `test_D03_…` |
| D04 | Devise non contrôlée | Montant **et** devise recontrôlés | `test_D04_…` |
| D05 | Notification réécrivant les commandes payées | Relation `commandes` ; notifications jamais fusionnées dans `metadata` | `test_D05_…` |
| D06 | Deux succès simultanés validés deux fois | Validation sous verrou, idempotente | `test_D06_…` (Postgres, concurrence) |
| D08 | Annulation partielle d'un groupe : tout « à rembourser » | Remboursement par commande, paiement toujours validé | `test_D08_…` |
| D09 | `metadata` exposée au client | Sérialiseur client explicite | `test_D09_…` |
| D10 | Succès après remboursement revalidé | Transitions contrôlées, `valide` final | `test_D10_…` |
| D11 | Webhooks bloqués par la limite anonyme | Limite dédiée par IP | `test_D11_…` |
| D14 | Initiation sans limite dédiée | Scope `paiements` | `test_D14_…` |

Également : `Paiement.client` en PROTECT ; fausses URL de paiement supprimées (plus rien de factice en production) ; `is_staff` sans pouvoir (API et Django admin en lecture seule) ; montant toujours serveur ; IDOR (initier, consulter, annuler) testés ; barème déjà appliqué ni modifié (sauf sa clôture) ni supprimé, et aucun barème créé ou reprogrammé dans le passé, quel que soit le chemin de l'API (§ 4).

## 12. Limites de débit et performance

| Scope | Taux | Endpoints | Clé |
|---|---|---|---|
| `paiements` | 20/h | `initier/`, `<id>/annuler/` | utilisateur |
| `webhook_paiement` | 3000/h | `webhook/…` | IP |

- Listes sans N+1 : 4 requêtes pour la liste des paiements, 3 pour les reversements vendeur, quel que soit le nombre (testé).
- Aucun appel réseau vers le fournisseur n'est fait sous verrou ou dans une transaction ; délai d'attente de 10 s.
- Le traitement d'une notification CinetPay fait un appel de vérification synchrone (CinetPay attend une réponse en moins de 10 s) : à passer en tâche Celery si les mesures le justifient (§ 15).

## 13. Migrations

- **paiements 0003** : `Paiement.commandes`, `fournisseur` (existants : `simule`), `hash_jeton_notification`, `client` en PROTECT, statuts réduits ; modèles `Remboursement`, `BaremeFrais`, `Reversement`, `AjustementVendeur` ; journal `recu`.
- **paiements 0004** (données) : reprise de `metadata.commandes_couvertes` dans la relation ; `a_rembourser` → validé + `Remboursement` ; paiements `espece_livraison` encore actifs → annulés ; clés internes retirées de `metadata` ; barème 12 % + 200 FCFA créé (clôturé par 0008).
- **commandes 0006** : frais figés sur `CommandeItem` (ventes passées : aucun frais, net = prix de la ligne).
- **utilisateurs 0009** : suppression de `compte_bancaire`.
- **paiements 0006** (frais de livraison) : `Reversement.montant_livraison` (0 pour l'existant) ; `AjustementVendeur.nature` (`retour` pour l'existant) et `commande`, contrainte « un ajustement de chaque nature par commande » (hors `retour`).
- **paiements 0007** : `BaremeFrais.seuil_petit_article` et `frais_fixe_petit_article` (vides pour l'existant : comportement inchangé), contrainte `bareme_frais_petit_article_complet_ou_absent` ; textes d'aide « TVA incluse ».
- **paiements 0008** (données, 28/09/2026) : le barème de la plateforme en vigueur est clôturé (`date_fin` = maintenant, rien d'autre) et le barème **14 % + 100 / 200 FCFA (seuil 3 000 FCFA), TVA incluse** créé au même instant, avec un identifiant fixe (`3f0c1a52-7d4e-4b8a-9c14-0e5b2d6a8f71`) : rejouée, elle ne fait rien. Barèmes des boutiques non touchés. Retour arrière : le barème 14 % est supprimé (aucune ligne n'y fait référence, les frais sont copiés dans `CommandeItem`) et le barème clôturé retrouve une `date_fin` vide. Limite : si ce barème avait une `date_fin` future avant la migration, le retour arrière la vide (aucun cas en base de dev).

Base de dev (28/09/2026, migrée, ramenée à 0006, puis migrée de nouveau) : 1 barème de plateforme 12 % + 200 FCFA clôturé, 1 barème 14 % en vigueur, aucun barème de boutique ; les 28 articles déjà vendus gardent 12 % + 200 FCFA (40 680 FCFA de commission, 10 200 de frais fixes), les 18 reversements sont inchangés.

Base de dev (26/09/2026) : 6 paiements, tous `annule` après migration (5 `espece_livraison`, 1 `wave`), chacun relié à sa commande ; 1 seul `compte_bancaire` en base, vide (aucune perte).

## 14. Tests

⚠️ **Toujours sur PostgreSQL** (sous SQLite, les tests de concurrence ne prouvent rien).

```bash
cd backend-django
DJANGO_SETTINGS_MODULE=config.settings.ci DB_NAME=anitche_test DB_USER=postgres DB_PASSWORD=postgres DB_HOST=localhost \
  python manage.py test apps.paiements -v 2
```

`apps/paiements/tests.py` — 99 tests (septembre 2026) : initiation (montant serveur, moyens acceptés, paiement à la livraison refusé, IDOR, boutique indisponible, fournisseur injoignable, annulation et relance, `is_staff`, N+1), notifications (signature manquante, invalide, expirée, rejeu, fournisseur, montant et devise, vérification non finalisée, 503 puis rejeu, limites), remboursements (paiement après expiration, double paiement, annulation partielle, traitement admin), concurrence (D02, D06), frais (barème par défaut 14 % TVA incluse et ancien barème clôturé, arrondi au demi-franc supérieur, plafond, barème sans seuil inchangé, seuil à 2 999 / 3 000 / 3 001 FCFA, promotion qui passe sous le seuil, quantité supérieure à 1 jusqu'au reversement, figés, offre de lancement, barème de boutique prioritaire, seuil et frais réduit ensemble en base et à l'API, coupon, 503 sans barème, non exposés, administration), migration 0008 dans les deux sens (commande d'avant gardant 12 % + 200 FCFA, barème de boutique intact, idempotence), barème déjà appliqué (`PATCH` d'un autre champ refusé avec `bareme_deja_applique` et sans effet, valeur identique acceptée, `PUT` soumis à la même règle, clôture maintenant ou plus tard, horloge du client en retard, clôture passée ou réouverture refusées, barème déjà clôturé figé, `DELETE` refusé en 409, barème programmé modifiable et supprimable, non-administrateur 403 et anonyme 401, Django admin en lecture seule), date de début (création dans le passé refusée avec `date_debut_passee`, « maintenant » ramené à l'heure du serveur, création future, sans date : commence à l'enregistrement, sans date avec `date_fin` passée : 400 et non 500, reprogrammation d'un barème programmé vers le passé refusée par `PATCH` et `PUT`, vers une autre date future ou « maintenant » acceptée), reversements (cycle de vie, 7 jours, retour ouvert/rejeté, retour remboursé avant et après versement, ajustement imputé, versement manuel, transfert immédiat, en attente puis notifié, échoué, vue vendeur), adaptateur CinetPay (format d'initiation, carte, notification revérifiée, statut de la notification ignoré, identifiants et montant incohérents, jeton expiré, injoignable, limites, transfert), configuration de production (simulé, sandbox, clés, HTTPS).

Postman : `postman_paiements.json` (hors dépôt, reconstruite) — mise en place automatique (vendeur, produit, clients, OTP via Mailpit), parcours nominal (checkout → paiement → notification simulée signée → commande confirmée → reversement), scénarios de sécurité (IDOR, signature manquante/fausse/expirée, montant et devise falsifiés, rejeu, autre fournisseur, paiement à la livraison), annulation et relance, vue vendeur, administration (`admin_password` à renseigner ; dont [SEC] : modifier le taux du barème en vigueur → 400 `bareme_deja_applique`, taux inchangé ensuite). L'ancienne collection est archivée dans `postman_archives/`.

## 15. Dette connue

- **Abonnement selon le volume de ventes** (modèle Jumia) : non implémenté, **0 FCFA au lancement**. Forme prévue : un barème d'abonnement mensuel par tranche de chiffre d'affaires, prélevé sur les reversements du mois (ajustement négatif). À concevoir quand le volume le justifie.
- **TVA sur la commission** : **tranché, TVA incluse** (septembre 2026) : 14 % + 100 / 200 FCFA sont des montants toutes taxes comprises, rien ne s'y ajoute (comme Jumia, qui affiche des commissions TTC). **Point ouvert** : la décomposition HT / TVA (taux de **18 % à confirmer avec un comptable**) le jour où ANITCHE émettra des factures de commission aux vendeurs. Rien n'est calculé ni stocké pour l'instant ; ce jour-là, la TVA se déduira des montants figés (TVA = TTC × taux / (100 + taux)) et la règle d'arrondi sera à fixer avec le comptable.
- **Compte CinetPay** : à ouvrir (RCCM) ; points à confirmer en sandbox au § 8.
- **Remboursement et transfert automatiques** : manuels au lancement ; API de remboursement à brancher quand CinetPay la publie.
- **Notification traitée de façon synchrone** : passer en tâche Celery si le temps de réponse de la vérification CinetPay le justifie (mesure en sandbox).
- **Module retours** (vu a minima) : un vendeur peut encore marquer un retour « remboursé » (seul l'argent reste entre les mains de l'administration, via `Remboursement`) ; le délai de rétractation de 7 jours n'y est pas appliqué ; à traiter avec ce module.
- **Opérateur Wave** non déductible du numéro pour un transfert : à préciser par l'admin (`operateur: "wave"`).

## 16. Justification des choix

### Pourquoi CinetPay

Frais relevés en septembre 2026 (sites des fournisseurs et articles qui les reprennent ; **à confirmer par devis**) :

| Option | Frais d'encaissement relevés | Pourquoi pas / pourquoi |
|---|---|---|
| **CinetPay** | 3,5 % par défaut en Côte d'Ivoire, 1,5 à 3,5 % selon le volume ; transferts 1,5 à 2 % ; sans abonnement | **Retenu.** Acteur ivoirien établi, couvre les 5 moyens voulus (Orange, MTN, Moov, Wave, carte) avec un seul contrat, **API de transfert** pour les reversements, API de vérification, SDK officiels maintenus en 2026, sandbox |
| GeniusPay | 1 % + 100 XOF **en plus** des frais de la passerelle utilisée : Wave 2,5 % + 100, mobile money via Paystack 4,5 % + 100, carte 6 % + 100 | Surcouche technique qui s'appuie elle-même sur CinetPay, Paystack ou PawaPay : plus cher au final, un intermédiaire de plus, et service **en bêta sur liste d'attente**, qui se présente comme « service d'intégration technique » et non comme établissement de paiement |
| PayDunya | 1,5 à 2,2 % selon le volume (source secondaire) | Moins cher annoncé, mais plus centré sur le Sénégal ; frais ivoiriens non publiés officiellement. **Gardé en réserve** : l'architecture permet de l'ajouter (§ 8) |
| Intégrations directes (Wave, Orange Money, MTN MoMo, Moov, + acquéreur carte) | Wave environ 1 % (plafonné à 5 000 FCFA) ; les autres sur contrat | Frais les plus bas, mais 4 à 5 contrats, 4 à 5 formats de notification et de vérification à maintenir, et un acquéreur carte en plus : disproportionné au lancement (CLAUDE.md : ne pas sur-concevoir) |

Sources : [CinetPay – tarification](https://support.cinetpay.com/d/52-tarifications-des-paiements-entrants-et-sortants), [CinetPay – transferts de masse](https://cinetpay.com/products/mass-payout), [SDK cinetpay-php](https://github.com/cinetpay/cinetpay-php-sdk), [SDK cinetpay-python](https://github.com/cinetpay/cinetpay-python), [GeniusPay – tarifs](https://pay.genius.ci/pricing), [Kolonell – passerelles en Côte d'Ivoire (PayDunya)](https://kolonell.com/fr/blog/passerelle-paiement-cote-divoire-wave-orange-mtn-2026), [Kolonell – Wave marchand en Côte d'Ivoire](https://kolonell.com/fr/blog/wave-cote-ivoire-integration-marchand-abidjan-2026), [Paystack – tarifs Côte d'Ivoire](https://paystack.com/ci/pricing).

### Barème : 14 % + 100 FCFA par article jusqu'à 3 000 FCFA, 200 FCFA au-delà, TVA incluse

Décision d'équipe du 28 septembre 2026 ; elle remplace le barème de départ, 12 % + 200 FCFA par article, dont le traitement de la TVA n'avait pas été tranché.

- **Même modèle que Jumia Côte d'Ivoire**, que les vendeurs connaissent : commission en % + frais fixe **par article**, prélevés à la livraison, plus un abonnement par tranche de chiffre d'affaires (5 000 FCFA entre 100 000 et 1 499 999 FCFA, 20 000 FCFA au-delà).
- **Compétitif** : Jumia prend **15 %** dans son exemple de la catégorie mode (commissions TTC, variables selon la catégorie) plus son frais fixe et son abonnement ; ANITCHE prend **14 % TVA incluse**, un frais fixe bas et **sans abonnement**.
- **Frais fixe réduit pour les petits articles** : un frais fixe pèse d'autant plus que l'article est bon marché (200 FCFA font 10 % d'un article à 2 000 FCFA, 100 FCFA en font 5 %).
- **Couvre les coûts** : l'encaissement CinetPay (jusqu'à 3,5 %) et le transfert au vendeur (1,5 à 2 %) restent sous la commission ; le frais fixe couvre la part fixe de traitement d'une commande. Sur un article à 5 000 FCFA : 900 FCFA de frais pour le vendeur (700 de commission + 200 de frais fixe), contre environ 175 FCFA d'encaissement (3,5 %) et 82 FCFA de transfert (2 % de 4 100) côté ANITCHE. Sur un article à 2 000 FCFA : 380 FCFA (280 + 100), contre environ 70 et 32 FCFA.
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
- **Versement immédiat et automatisable** par l'API de transfert CinetPay (100 à 1 500 000 FCFA par transfert), là où un virement bancaire prend plusieurs jours et n'a pas d'API chez le fournisseur retenu.
- **Minimisation des données** : ne plus collecter de coordonnées bancaires supprime une donnée sensible (chiffrée, mais à protéger, sauvegarder et justifier) sans usage.
- Limite assumée : un reversement de plus de 1 500 000 FCFA devra être fractionné ou versé manuellement ; à revoir si des vendeurs à gros volume le demandent.
