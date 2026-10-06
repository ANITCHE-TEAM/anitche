# Module notifications — contrat et règles

> Périmètre : backend Django, `backend-django/apps/notifications/` (et ses appelants : `paiements`, `livraison`, `commandes`, `retours`, `fidelite`, `support`).
> Principe : une notification accompagne des événements d'argent et de livraison : **elle doit refléter ce qui a réellement été enregistré**, jamais plus, et ne jamais ralentir ni bloquer l'action elle-même.

## 1. Principe d'envoi

```text
action métier (transaction)
   ├── Notification in-app créée DANS la transaction  → annulée avec elle
   └── transaction.on_commit → Celery envoyer_email_notification(id)
                                 └── relit la notification, send_mail (3 essais, délai croissant)
```

- **In-app toujours** : c'est l'historique du compte (paiement, remboursement, livraison, retours). Il ne se désactive plus.
- **Email** : après le commit, par le worker Celery (Mailpit en dev). Jamais d'appel SMTP pendant la requête ni sous un verrou de base ; jamais d'email pour une action annulée. Seul l'identifiant passe par Redis.
- **Préférence** : `email_actif` (vrai par défaut). Paramètre `email_critique=True` pour un email indispensable envoyé malgré la préférence. Les emails déjà hors de ce service restent envoyés quoi qu'il arrive : code de livraison (`livraison.tasks`), codes OTP (`utilisateurs.tasks`).
- **Alertes de l'administration** (`notifier_administration`) : une notification in-app par administrateur actif, **sans email** (un email par alerte et par administrateur noyait les boîtes) ; la trace durable reste le journal `securite`.
- **Journaux** : identifiant de la notification, du destinataire et type seulement (plus d'email, de téléphone ni de contenu).

## 2. Architecture

```text
services.py   ServiceNotification.notifier_utilisateur / notifier_administration /
              marquer_toutes_lues ; alerter_stock_bas ; notifier_annulation_commande
tasks.py      envoyer_email_notification (Celery, après commit)
signals.py    paiement_valide (client + vendeurs), livraison_status_change (client)
views.py      liste, compteur, lire, tout lire, préférences — toujours filtrés par request.user
admin.py      consultation seule
```

## 3. Événements notifiés

| Événement | Destinataire(s) | Source |
|---|---|---|
| Paiement confirmé | client (+ email), vendeur de chaque commande | `notifications.signals` |
| Étape de livraison | client | `notifications.signals` |
| Code de livraison | client (email dédié, toujours) | `livraison.tasks` |
| **Commande annulée** (nouveau) | client toujours (motif, remboursement si payée) ; vendeur si la commande était payée | `commandes.services.annuler_commande` |
| **Stock bas / rupture** (nouveau) | vendeur, au franchissement de `seuil_alerte` puis à 0 | `commandes.views.ValiderPanierView` |
| Remboursement à traiter, contestation, **retour rejeté** | administration (in-app) | `paiements`, `livraison`, `retours` |
| **Nouvelle demande de retour**, actions du client sur un retour (nouveau) | vendeur | `retours` |
| Décisions sur un retour | client | `retours` |
| Points de fidélité crédités (nouveau) | client | `fidelite` |
| **Réponse ou nouveau ticket support** (nouveau) | client / équipe support | `support` |
| Reversement versé | vendeur | `paiements` |

**Stock bas** : une alerte quand une vente fait passer la quantité **au niveau du seuil ou en dessous** (avant > seuil ≥ après), une autre à la rupture (0). Les ventes suivantes sous le seuil n'en créent pas de nouvelle ; un réapprovisionnement au-dessus du seuil réarme l'alerte. Les quantités avant et après sont connues sous le verrou du checkout : aucune requête de plus quand le seuil n'est pas franchi.

## 4. Endpoints — `/api/notifications/`

| Méthode | Chemin | Détail |
|---|---|---|
| GET | `` | Les siennes, paginées (20), `?non_lues=true`, `?type=` (`commande`, `paiement`, `livraison`, `kyc`, `stock`, `support`, `systeme`) |
| GET | `compteur/` | `{"non_lues": n}` |
| PATCH | `<uuid>/lire/` | **404** pour la notification d'un autre compte |
| POST | `toutes-lues/` | `{"message", "nb_modifiees"}` |
| GET / PATCH | `preferences/` | **`{"id", "email_actif", "date_mise_a_jour"}`** (ni `in_app_actif` ni `sms_actif`) |

Représentation : `id`, `titre`, `message`, `type_notification(_display)`, `canal(_display)`, `est_lu`, `date_lecture`, `lien_redirection`, `metadata`, `date_creation`. Aucune donnée sensible : montants, numéros de commande, de retour ou de ticket ; jamais d'email, de téléphone ni de code (le code de livraison n'est que dans l'email dédié au client et dans le suivi de livraison).

## 5. Impact frontend

1. **Préférences** : ne proposer que « Recevoir les emails » (`email_actif`). `in_app_actif` et `sms_actif` disparaissent de la réponse (envoyés, ils sont ignorés).
2. **Nouveaux types à afficher** : `stock` (vendeur : stock bas, rupture ; `metadata.variante_id`, `quantite_disponible`, `seuil_alerte`), annulations de commande (`type: commande`), retours, points crédités (`systeme`), support.
3. **Paiement de plusieurs commandes** : `lien_redirection` vaut `/commandes` (jamais `/commandes/` avec un identifiant vide).
4. **Délai des emails** : ils arrivent quelques secondes après l'action (worker Celery), plus pendant la requête ; aucun effet sur les temps de réponse.
5. **Administration** : les alertes n'arrivent pas par email ; prévoir un badge sur le compteur de notifications du back-office.

## 6. Sécurité — risques couverts

Chaque risque a son test permanent dans `apps/notifications/tests.py`.

| Risque | Protection | Test |
|---|---|---|
| `send_mail` synchrone dans la requête et sous verrou (validation de paiement, livraison) ; email envoyé même si la transaction est annulée ; pannes masquées | Email après commit par Celery, réessais | `EnvoiApresCommitTests` |
| Un email par alerte et par administrateur | Alertes administration in-app | `EnvoiApresCommitTests` |
| Une préférence in-app qui efface toute trace (remboursement, livraison) ; une préférence SMS sans effet | Préférence email seule | `PreferencesTests` |
| Email, téléphone et contenu des messages dans les journaux | Identifiants seulement | `EnvoiApresCommitTests` |
| Aucune notification à l'annulation d'une commande | Client, et vendeur si payée | `AnnulationTests` |
| `seuil_alerte` sans effet | Alerte au franchissement, rupture | `StockBasTests` |
| Notification modifiable dans le Django admin | Consultation seule | — |

Également vérifié : isolation par destinataire sur la liste, le compteur, « lire » et « tout lire » (`IsolationTests`).

## 7. Limites de débit et performance

- Taux général par compte (300/heure) : lecture et marquage seulement, aucune écriture coûteuse.
- Index `(destinataire, -date_creation)` : liste et compteur d'un utilisateur quand la table grossit (plusieurs lignes par commande, sans purge).
- Plus de `get_or_create` des préférences à chaque notification : une lecture seule, et seulement si un email est envisagé.
- Alertes administration en un `bulk_create`.

## 8. Migrations

- **notifications 0002** : suppression de `in_app_actif` et `sms_actif` ; index `notif_destinataire_date`.

En dev : `docker exec anitche-backend python manage.py migrate`, puis **redémarrer le worker Celery** (nouvelle tâche `envoyer_email_notification`).

## 9. Tests

`apps/notifications/tests.py` (PostgreSQL) : API historique (préférences adaptées), signaux, puis un test par faille (§ 6), dont l'alerte de stock et l'annulation par le vrai checkout (`DonneesCycleDeVie` des tests commandes).

Postman : `postman_notifications.json` (hors dépôt, nouvelle). **Aucun compte administrateur nécessaire.** Parcours : commande payée → notification client (sans donnée sensible) et **email lu dans Mailpit** → notification vendeur → compteur, filtres → IDOR (404, absente de la liste d'un autre) → lire, tout lire → préférences (email seul) → commande annulée : notification in-app, **aucun email** (préférence respectée), vendeur non prévenu (impayée) → stock mis à 6 (seuil 5), une vente → **alerte « stock bas »** au vendeur.

## 10. Dette connue

- **SMS** : aucun fournisseur ; le code de livraison et les OTP passent par email.
- **Purge** : pas de suppression des notifications lues anciennes (à prévoir en production, par exemple 90 jours).
- **Push** : canal prévu dans le modèle, non implémenté.
- **Stock bas hors checkout** : une baisse manuelle du stock par le vendeur ne l'alerte pas (il vient de la faire).

## 11. Justification des choix

### Pourquoi Celery après le commit

- **Justesse** : un email ne part que pour ce qui a été enregistré. Envoyé pendant la requête, un « Paiement confirmé » pourrait partir pour un paiement dont la transaction échoue ensuite.
- **Performance et concurrence** : un serveur SMTP lent (secondes) prolongerait des transactions qui tiennent des verrous (validation de paiement, statut de livraison) ; sous charge, toutes les requêtes sur les mêmes lignes attendraient.
- **Fiabilité** : une panne SMTP est réessayée, et visible dans les journaux du worker, au lieu d'être avalée (`fail_silently=True`).
- C'est déjà le mécanisme utilisé pour le code de livraison et les OTP : un seul modèle dans le projet.

### Pourquoi l'in-app ne se désactive plus

Le centre de notifications est la seule trace, côté client, d'un remboursement, d'une annulation ou d'un retour. Le laisser désactiver privait le client d'informations sur son argent, sans lui faire gagner quoi que ce soit (rien n'est envoyé hors de l'application). Le choix réel porte sur les emails.

### Pourquoi l'alerte de stock au checkout

C'est le seul endroit où le stock baisse par une vente, et les quantités avant et après y sont déjà connues sous verrou : l'alerte au franchissement ne coûte aucune requête supplémentaire et ne se répète pas à chaque vente. Un signal sur `Stock` ne fonctionnerait pas : la décrémentation est un `UPDATE` conditionnel (pas de `save()`).
