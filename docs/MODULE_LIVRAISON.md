# Module livraison — contrat et règles

> Périmètre : backend Django, `backend-django/apps/livraison/` (et ses points de contact dans `commandes`, `paiements`, `notifications`, `utilisateurs`).
> État : refonte de septembre 2026. **« Livrée » déclenche de l'argent** (délai de rétractation de 7 jours puis reversement au vendeur) : toute la conception part de là.

## 1. Ce sur quoi le module s'appuie, et qui s'appuie sur lui

| Module | Lien |
|---|---|
| `paiements` | `valider_paiement` crée la fiche (adresse reprise du checkout) ; « livrée » → `commandes.services.synchroniser_depuis_livraison` → `reversements.ouvrir_retractation` ; contestation → `suspendre_reversement`, puis `reprendre_reversement` ou `annuler_reversement` + `Remboursement` (`livraison_non_recue`) ; `reversements._reprendre` ne reprend pas un reversement dont la livraison est contestée |
| `commandes` | « expédiée » et « livrée » se répercutent sur la commande (409 si elle n'est pas prête) ; `annuler_commande` appelle `livraison.services.annuler_livraison_de` dans sa transaction ; l'abandon d'une livraison annule la commande « expédiée » (motif `livraison_echouee`) |
| `notifications` | Écoute le signal `livraison_status_change` (le client est prévenu à chaque étape, sauf « annulée », déjà couverte par l'annulation de la commande) |
| `utilisateurs` | Rôle `livreur` (nommé et retiré par l'administration) ; `telephone` du livreur montré au client pendant la tournée |
| `vendeurs` | `EstAdministrateur`, `EstVendeurValide`, `ROLES_ADMINISTRATION` (jamais `is_staff` seul) |

## 2. Architecture

```text
views.py        HTTP : payloads, queryset par rôle, codes de réponse
permissions.py  EstLivreurOuAdministrateur (accès à l'endpoint de statut)
services.py     seul point d'entrée des changements : table des transitions sous
                select_for_update, code de livraison, assignation, annulation, abandon,
                contestation, rôle livreur
serializers.py  une représentation par rôle (client, livreur, vendeur, administration)
tasks.py        envoi du code de livraison par email (Celery, après commit)
admin.py        Django admin : livreur (livreurs actifs seulement) et date estimée
```

## 3. Modèles

- **`Livraison`** : `commande` (1-1), `livreur`, `status`, `adresse_livraison` (texte d'une ligne, copié du checkout), `date_livraison_estimee`, `date_expedition`, `date_livraison`, `tentatives` (passages « en cours »), `code_hash` (vérification), `code_chiffre` (Fernet, pour le réafficher au client et l'envoyer par email), `code_essais`. Le code est **effacé** dès que la livraison est livrée ou annulée.
- **`LivraisonHistorique`** : chaque changement (statut, assignation) avec `effectue_par`, **`role_acteur`** figé au moment de l'action (`livreur`, `administration`, `client`, `systeme`) et `commentaire`.
- **`ContestationLivraison`** (1-1 avec la livraison) : `motif`, `statut` (`ouverte` → `rejetee` / `fondee`), `commentaire_resolution`, `resolue_par`, dates.

## 4. Statuts et transitions

| De | Vers | Qui | Effet |
|---|---|---|---|
| `en_attente` | `expediee` | livreur assigné | commande → `expediee` (409 si elle n'est pas en préparation) |
| `expediee` | `en_cours` | livreur assigné | tentative + 1, **nouveau code** généré, visible par le client et envoyé par email |
| `expediee` / `en_cours` | `echouee` | livreur assigné, **motif obligatoire** (`commentaire`) | aucun effet sur la commande |
| `en_cours` | `livree` | livreur assigné **+ code du client** | commande → `livree`, rétractation du reversement ouverte (7 jours) |
| `echouee` | `en_cours` | administration : nouvelle tentative | au plus `LIVRAISON_TENTATIVES_MAX` (3) passages « en cours », nouveau code |
| `echouee` | `annulee` | administration : abandon (`abandonner/`) | commande « expédiée » annulée (`livraison_echouee`), stock restitué, remboursement à traiter, reversement annulé |
| `en_attente` | `annulee` | système : commande annulée (client, administration, expiration…) | code effacé, coordonnées masquées au livreur |

Tout le reste est refusé, **administration comprise** : **400** pour une transition absente de la table (saut d'étape, retour arrière, sortie de `livree` ou `annulee`), **403** pour une étape qui ne revient pas à l'appelant (l'administration ne fait pas les étapes du livreur, le livreur ne décide pas d'une nouvelle tentative). « livrée » et « annulée » sont définitifs : une correction après livraison passe par la contestation ou les retours.

Le livreur doit, à chaque transition, être **assigné**, avoir le **rôle livreur**, être **actif** et **ne pas être le propriétaire de la boutique** de la commande (vérifié sous verrou : une donnée ancienne ou un changement de rôle par le Django admin n'ouvre aucun droit).

## 5. Endpoints — `/api/livraison/`

### Tous les comptes authentifiés (queryset par rôle ; 404 hors périmètre)

| Méthode | Chemin | Détail |
|---|---|---|
| GET | `` | Administration : toutes, filtres `?status=` et `?contestation=ouverte`. Livreur : les siennes. Tout autre compte : celles de ses commandes |
| GET | `<uuid>/` | Même règle d'accès |
| GET | `<uuid>/historique/` | Même règle d'accès ; **404** pour un tiers (avant : 200 avec une liste vide). `acteur` (rôle) remplace l'identifiant ; l'administration voit aussi `effectue_par` |
| PATCH | `<uuid>/statut/` | Livreur ou administration (`EstLivreurOuAdministrateur`, puis contrôle fin dans le service). Corps : `status`, `commentaire?` (obligatoire pour `echouee`), `code?` (obligatoire pour `livree`). **200** : la livraison (représentation de l'appelant). **400** / **403** : § 4. **409** : commande pas prête. **429** : limite `livraison_statut` |
| POST | `<uuid>/contester/` | Client de la commande uniquement (404 sinon). Corps : `motif`. **201** : la contestation. **400** : pas livrée, délai de 7 jours écoulé, déjà contestée. **429** : limite `livraison_contestation` |

### Vendeur (`EstVendeurValide` ; livraisons de sa boutique, lecture seule)

| Méthode | Chemin | Détail |
|---|---|---|
| GET | `vendeur/` | `id`, `commande`, `numero_commande`, `status`, `livreur` (`{prenom}`), `date_livraison_estimee`, `date_expedition`, `date_livraison`, `created_at`, `updated_at`. **Jamais le code**, ni l'adresse et le téléphone (déjà dans `/api/commandes/vendeur/`) |
| GET | `vendeur/<uuid>/` | Idem ; 404 pour la livraison d'une autre boutique |

### Administration (`EstAdministrateur` : rôle admin, jamais `is_staff` seul)

| Méthode | Chemin | Détail |
|---|---|---|
| POST | `<uuid>/assigner/` | Corps : `livreur_id`, `date_livraison_estimee?` (pas dans le passé). Assignation ou réassignation tant que la livraison n'est ni livrée ni annulée. **400** : pas livreur, inactif, propriétaire de la boutique, livraison terminée, livreur introuvable |
| POST | `<uuid>/abandonner/` | Corps : `commentaire` (obligatoire). Livraison échouée uniquement (**400** sinon) |
| POST | `<uuid>/contestation/resoudre/` | Corps : `decision` (`rejetee` / `fondee`), `commentaire?`. **400** si déjà traitée, **404** sans contestation |
| GET | `livreurs/` | Livreurs actifs : `id`, `prenom`, `nom`, `email`, `telephone`, `livraisons_en_cours` |
| POST | `livreurs/nommer/` | Corps : `utilisateur_id`. Client → livreur. **400** : vendeur, demande vendeur en cours ou validée, propriétaire d'une boutique, autre rôle, compte désactivé, déjà livreur |
| POST | `livreurs/<id>/retirer/` | Livreur → client, **effet immédiat**. Réponse : `utilisateur` et `livraisons_a_reassigner` (`id`, `numero_commande`, `status`) |

### Représentations

| Champ | Client | Livreur | Vendeur | Administration |
|---|---|---|---|---|
| `livreur` | `{prenom, telephone}` — téléphone **seulement pendant `en_cours`**, sinon `null` | `{prenom}` | `{prenom}` | `{id, prenom, nom}` |
| `adresse_livraison`, `adresse` (commune, quartier, point de repère, téléphone), `telephone_contact` | oui | oui, **masqués** (`""` / `null`) une fois `livree` ou `annulee` | non | oui |
| `code_livraison` | seulement pendant `en_cours` | non | non | non |
| `tentatives` | non | oui | non | oui |
| `code_essais_restants` / `code_essais` | non | pendant `en_cours` | non | `code_essais` |
| `contestation`, `date_limite_contestation` | oui (`statut`, dates) | non | non | `contestation` (avec le motif) |

`telephone_contact` et `adresse.telephone` sont le numéro choisi par le client au checkout, jamais celui de son profil.

## 6. Code de livraison

- Généré à **chaque** passage « en cours » (6 chiffres, `secrets`), il remplace le précédent et remet les essais à zéro.
- **Haché** (`make_password`) pour la vérification, **chiffré** (`EncryptedCharField`, mêmes clés que les numéros de reversement) pour être réaffiché dans l'app et envoyé par email : jamais en clair en base. Un haché seul ne peut pas être réaffiché au client, d'où les deux colonnes.
- **Email** au client, quelles que soient ses préférences de notification (sans ce code, le colis ne peut pas être remis) : tâche `envoyer_code_livraison_email`, lancée après le commit. Seul l'identifiant de la livraison passe par Redis ; la tâche relit le code et n'envoie rien si la livraison n'est plus en cours.
- **5 essais** par code. Au-delà, même le bon code est refusé : le livreur passe la livraison « échouée », et l'administration relance une tentative, avec un nouveau code. Blocage journalisé (`securite`).
- **Client injoignable** : « échouée » avec motif ; aucun passage « livrée » sans le code, même pour l'administration.

## 7. Contestation « non reçu »

1. Le client conteste une livraison **livrée**, au plus tard `date_livraison` + `REVERSEMENT_DELAI_RETRACTATION_JOURS` (7 jours), une seule fois : le reversement est **suspendu**, et chaque administrateur actif est alerté (journal `securite` + notification).
2. Tant que la contestation est ouverte, le reversement ne reprend pas, même si un retour de la même commande se clôt.
3. L'administration tranche : **rejetée**, le reversement reprend (rétractation ou disponible selon la date) ; **fondée**, le reversement est annulé et un `Remboursement` `livraison_non_recue` est créé (à traiter comme les autres, [`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md) § 6). Le client est notifié de la décision.

La commande et la livraison restent « livrées » : la contestation fondée est tracée par `ContestationLivraison` et le remboursement.

## 8. Rôle livreur

- **Devenir livreur** : un compte client est nommé par l'administration (`livreurs/nommer/`). Un vendeur, ou un compte qui a une demande vendeur en cours, ne peut pas l'être : un compte ne porte qu'un rôle, et un livreur-vendeur pourrait déclencher son propre reversement.
- **Retrait** (`livreurs/<id>/retirer/`) : le compte redevient client **immédiatement**. Il ne voit plus ses livraisons et ne peut plus agir dessus, et celles qui ne sont pas terminées sont renvoyées pour réassignation.
- **Suspension** : `is_active=False` (Django admin) coupe tout accès, le jeton est refusé (401).
- **Django admin** : le champ `livreur` ne propose que les livreurs actifs, applique la même règle (jamais le propriétaire), et l'assignation passe par le service (historique, journal). Seul le rôle admin peut modifier une fiche ; le statut n'y est jamais éditable.

## 9. Frais de livraison — décision produit en attente

**Existant : aucun.** `montant_total` = produits − remise ; les seuls « frais » du code sont ceux du vendeur (commission). Seul `VarianteProduit.poids_kg` est prévu pour un calcul futur. Options présentées : forfait client par commune ou zone figé au checkout (modèle Jumia CI), frais absorbés par ANITCHE, frais à la charge du vendeur (déduits du reversement), formule mixte. **Étape séparée, après ce module** : elle touche `panier`, `commandes` et `paiements` (le montant payé).

## 10. Impact frontend

1. **Code de livraison (client).** Afficher `code_livraison` dans le suivi quand `status` = `en_cours` (il arrive aussi par email), avec la consigne : le donner au livreur **à la remise du colis seulement**.
2. **Livreur : saisie du code.** « Livrée » envoie `{"status": "livree", "code": "123456"}`. **400** : code manquant ou faux (`detail` annonce les essais restants, `code_essais_restants` dans la fiche), puis code bloqué, il faut proposer « échouée ». « Échouée » exige un `commentaire` (motif), sinon **400**.
3. **`livreur` devient un objet** (avant : un identifiant). Client : `{prenom, telephone}` (téléphone non nul pendant `en_cours` seulement) ; vendeur et livreur : `{prenom}` ; administration : `{id, prenom, nom}`.
4. **Nouveaux champs** : `numero_commande`, `adresse` (structurée : commune, quartier, point de repère, téléphone), `date_livraison_estimee`, et pour le client `contestation` et `date_limite_contestation`. `adresse_livraison` et `telephone_contact` sont conservés.
5. **Nouveau statut `annulee`** : fiches des commandes annulées (y compris les anciennes, reprises par migration). Libellé et couleur à prévoir.
6. **Historique** : `effectue_par` est remplacé par `acteur` (`livreur`, `administration`, `systeme`, `client`) ; l'administration garde `effectue_par`. Un tiers reçoit **404** (avant : 200 avec une liste vide).
7. **Contestation (client)** : bouton « Je n'ai pas reçu mon colis » tant que `date_limite_contestation` n'est pas passée et que `contestation` est `null` → `POST <id>/contester/` (`motif`). Afficher ensuite l'état de `contestation.statut`.
8. **Livreur** : après « livrée » ou « annulée », l'adresse et le téléphone du client arrivent vides (`""` / `null`) : ne pas les mettre en cache côté app.
9. **Espace vendeur** : nouvel écran de suivi, `GET /api/livraison/vendeur/`.
10. **Back-office** : liste des livreurs, nomination et retrait (avec les livraisons à réassigner), assignation avec date estimée, nouvelle tentative (`PATCH statut/` → `en_cours` sur une livraison échouée), abandon, contestations ouvertes (`?contestation=ouverte`) et décision.
11. **Administration** : ne plus proposer de transitions libres (400 / 403, § 4).
12. **Erreurs à prévoir** : **429** au-delà de 120 changements de statut par heure et par compte, ou de 10 contestations par heure.

## 11. Sécurité — failles corrigées (diagnostic de septembre 2026)

Chaque constat a été confirmé par un test jetable sur PostgreSQL avant correction, et chacun a son test permanent dans `apps/livraison/tests.py`.

| # | Faille | Correction | Test |
|---|---|---|---|
| D1 | Le livreur seul déclarait « livrée » et déclenchait le reversement | Code de livraison du client obligatoire (haché, 5 essais) ; contestation pendant 7 jours | `CodeDeLivraisonTests`, `ContestationTests` |
| D2 | N'importe quel utilisateur assignable (Django admin), dont le vendeur de la commande | Assignation par l'API ou par le Django admin restreint : livreur actif, jamais le propriétaire ; revérifié à chaque transition | `AssignationTests` |
| D3 | Un ex-livreur gardait la main sur ses livraisons | Rôle livreur vérifié à chaque transition ; retrait par l'API | `RoleLivreurTests` |
| D4 | Transitions libres de l'administration (sortie de « livrée » après ouverture du reversement) | L'administration suit la table ; « livrée » et « annulée » définitifs | `TransitionsAdministrationTests` |
| D5 | Fiche d'une commande annulée restée « en attente », coordonnées visibles, modifiable | Statut `annulee` dans la transaction d'annulation ; migration des fiches existantes | `AnnulationTests` |
| D6 | « Échouée » sans issue (commande et reversement bloqués) | Nouvelle tentative (3 au plus) ou abandon → annulation et remboursement | `EchecTests` |
| D7 | Le vendeur ne voyait aucune livraison | Lecture seule de sa boutique, sans code | `IsolationEtSuiviTests` |
| D8 | Historique : 200 vide pour un tiers, `effectue_par` exposé au client | 404 ; rôle à la place de l'identifiant | `IsolationEtSuiviTests` |
| D9 | Suivi pauvre (id du livreur, adresse d'une ligne, aucune estimation) | Prénom, téléphone pendant la tournée, adresse structurée, date estimée | `IsolationEtSuiviTests` |
| D10 | Coordonnées du client conservées par le livreur après la livraison | Masquées une fois livrée ou annulée | `IsolationEtSuiviTests` |
| D12 | N+1 sur la liste (`commande.groupe`) | `select_related` complet, nombre de requêtes constant | `PerformanceEtLimitesTests` |
| D14 | Pas de limite dédiée | `livraison_statut` 120/h, `livraison_contestation` 10/h | `PerformanceEtLimitesTests` |
| D15 | « Livrée » et « échouée » simultanées : livraison échouée, commande livrée, reversement ouvert | Transitions sous `select_for_update` ; la seconde requête voit le nouvel état (400) | `ConcurrenceTests` |

Vérifié et correct dès le diagnostic : `is_staff` sans rôle admin n'a aucun pouvoir (API et Django admin), un livreur désactivé est refusé par le JWT (401).

## 12. Limites de débit et performance

- `livraison_statut` : 120/heure par compte (un livreur fait 4 changements par colis) ; en plus, 5 essais par code.
- `livraison_contestation` : 10/heure par compte.
- Listes paginées (20) ; `select_related("commande__groupe", "livreur", "contestation")` : nombre de requêtes constant quel que soit le nombre de livraisons (testé pour les quatre rôles).
- Ordre des verrous : fiche de livraison, puis commande, pour l'annulation comme pour les transitions (pas d'interblocage entre une annulation et une expédition simultanées).
- La vérification du code (`check_password`, PBKDF2 en production) coûte quelques dizaines de millisecondes par essai, bornée par les 5 essais et la limite de débit.

## 13. Migrations

- **livraison 0002** : `annulee`, `date_livraison_estimee`, `tentatives`, `code_hash`, `code_chiffre`, `code_essais`, `LivraisonHistorique.role_acteur`, `ContestationLivraison`.
- **livraison 0003** (données) : fiches des commandes déjà annulées (hors livrées) → `annulee`, avec une ligne d'historique `systeme`.
- **commandes 0007** : motif `livraison_echouee`.
- **paiements 0005** : motif de remboursement `livraison_non_recue`.

En dev : `docker exec anitche-backend python manage.py migrate`, puis **redémarrer le worker Celery** (`docker restart infra-celery-worker-1`) pour qu'il connaisse la tâche d'envoi du code.

## 14. Tests

⚠️ **Toujours sur PostgreSQL.** Le Python de l'hôte n'a pas les dépendances (Celery) : lancer les tests dans le conteneur.

```bash
docker exec -e DJANGO_SETTINGS_MODULE=config.settings.ci -e DB_NAME=anitche_test anitche-backend \
  python manage.py test apps.livraison -v 2
```

`apps/livraison/tests.py` — 73 tests : parcours et isolation historiques (adaptés au nouveau contrat), code de livraison (sans code, faux code, blocage, haché et chiffré, visibilité, effacement, email), assignation (date estimée, réassignation, livraison terminée, propriétaire, non-livreur ou inactif, réservée à l'administration, donnée existante, Django admin), rôle livreur (retrait immédiat, par le Django admin aussi, nomination, refus du vendeur, réservé à l'administration, compte désactivé), transitions de l'administration, échec (motif, nouvelle tentative, limite, abandon, remboursement), annulation (administration, client, annulation refusée, migration), contestation (suspension, alerte, périmètre, délai, décisions, retour clos), isolation et suivi (vendeur, historique, client, livreur, `is_staff`, Django admin), performance (N+1, limites de débit), concurrence (D15). Tests adaptés hors module : `commandes` (`MachineAEtatsTests` : les étapes passent par un livreur, avec le code) et `notifications` (transition par le service).

Suite complète : **578 tests, OK, aucun « skipped »** (26/09/2026).

Postman : `postman_livraison.json` (hors dépôt, reconstruite ; l'ancienne est archivée dans `postman_archives/`). Mise en place automatique (vendeur, produit, clients, livreur, codes via Mailpit). Parcours nominal : commande payée → préparation → nomination et assignation → expédiée → en cours → **code lu dans Mailpit, comparé à celui de l'app** → livrée. Scénarios de sécurité : IDOR, historique 404, sans code, faux code, régression par l'administration, masquage, vue vendeur, contestation, échec, nouvelle tentative, abandon et remboursement, annulation, retrait du livreur. `admin_password` à renseigner.

## 15. Dette connue

- **Frais de livraison** : décision produit en attente (§ 9).
- **SMS du code** : email seulement au lancement (Mailpit en dev) ; brancher un fournisseur SMS (client sans email consulté).
- **KYC livreur** : aucun dossier d'identité pour les livreurs, nommés par l'administration ; à prévoir avant de faire appel à des livreurs indépendants.
- **Changement de rôle par le Django admin des utilisateurs** : le champ `role` y reste libre (module `utilisateurs`). Un vendeur passé livreur par ce biais ne peut toujours pas livrer sa propre commande (contrôle à chaque transition), mais la règle « jamais un vendeur » n'est garantie que par l'API de nomination.
- **Stock restitué à l'abandon** : l'abandon réutilise `annuler_commande`, qui remet le stock en vente. C'est juste si le colis revient au vendeur (hypothèse retenue) ; un colis perdu demanderait une correction manuelle du stock.
- **Livreur qui commande pour lui-même** : un compte livreur ne voit dans `/api/livraison/` que les livraisons qui lui sont assignées, pas celles de ses propres achats (cas marginal, un compte ne porte qu'un rôle).
- **Emails de notification envoyés dans la transaction** (`notifications`) : un changement annulé après coup peut avoir déjà envoyé l'email d'étape. Hors code de livraison, qui part après le commit.
- **Preuve de livraison enrichie** (photo, géolocalisation) : non prévue ; à étudier si des contestations « non reçu » se multiplient malgré le code.
- **Retours** : une demande de retour est encore acceptée avant expédition (commande confirmée) ; traité avec le module retours.

## 16. Justification des choix

### Pourquoi l'administration assigne (et pas le vendeur ni les livreurs eux-mêmes)

- L'argent dépend de « livrée » : si le vendeur choisissait son livreur, il pourrait s'entendre avec lui pour déclarer livré un colis jamais parti. ANITCHE garde la maîtrise de sa flotte.
- Un pôle où les livreurs se servent exposerait l'adresse et le téléphone de clients à des livreurs qui ne livreront pas leur colis.
- C'est le plus simple au lancement (peu de livreurs). Un pôle ou une assignation automatique par zone restent possibles plus tard, sans toucher au contrat de statut.

### Pourquoi un code de livraison et une contestation (options 1 + 3)

- **Le code** prouve que le client était présent à la remise : ni le livreur ni le vendeur ne peuvent déclarer livré un colis qui n'a pas été remis. C'est une pratique courante des plateformes de livraison (code donné au livreur à la remise).
- **La contestation** couvre ce que le code ne couvre pas : code communiqué trop tôt ou par téléphone, colis incomplet ou non conforme au moment de la remise. Elle reprend le mécanisme de suspension des retours et ne coûte presque rien, car l'argent n'est pas encore versé pendant les 7 jours.
- **Écartée : la confirmation du client dans l'app** comme seule preuve. Un client passif bloque le vendeur, et une confirmation automatique après X heures ramène la faille de départ.
- **Haché + chiffré** : le code se vérifie par le haché ; seule la copie chiffrée permet de le réafficher au client et de l'envoyer par email sans le faire transiter en clair par la file de tâches.
- **5 essais** : même règle que les codes OTP du projet ; un code à 6 chiffres ne se devine pas en 5 essais (1 chance sur 200 000).

### Pourquoi 3 tentatives puis l'abandon

- Trois passages « en cours » (première tentative comprise) couvrent les absences ponctuelles du client sans immobiliser un colis et le paiement du client pendant des semaines. C'est l'ordre de grandeur pratiqué par les transporteurs, qui font 2 à 3 présentations avant retour à l'expéditeur.
- L'abandon est une décision de l'administration, avec motif obligatoire : il annule la commande et crée un remboursement. Ce n'est jamais automatique, parce qu'un appel au client peut encore sauver la vente.
- Réglable sans déploiement : `LIVRAISON_TENTATIVES_MAX`.

### Pourquoi « annulée » dans la transaction de l'annulation de commande

- Une seule source de vérité : une commande annulée ne peut plus avoir de livraison active, et une livraison déjà partie **bloque** l'annulation au lieu d'être oubliée.
- Même transaction, donc pas d'état intermédiaire visible, et verrou pris sur la fiche avant la commande, dans le même ordre que les transitions de livraison.

### Pourquoi masquer les coordonnées du client une fois la livraison terminée

- Principe de minimisation : le livreur n'a besoin de l'adresse et du téléphone que pour livrer. Après la livraison (ou l'annulation), les garder accessibles n'apporte rien et expose le client (démarchage, harcèlement).
- Le téléphone du livreur n'est montré au client que pendant la tournée, pour la même raison.
