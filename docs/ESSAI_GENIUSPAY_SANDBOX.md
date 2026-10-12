# Essai réel de GeniusPay en sandbox — pas à pas

Essai de bout en bout de l'adaptateur GeniusPay (`docs/MODULE_PAIEMENTS.md`, § 8) sur le poste de développement : un vrai paiement de sandbox, une vraie notification signée reçue par un tunnel, la vérification serveur à serveur, la commande payée. Aucun argent ne circule.

**Règle** : aucune clé ni aucun secret dans le dépôt, dans un message ou dans une capture. Ils ne vivent que dans `backend-django/.env` (ignoré par git). La commande `essai_geniuspay` n'en affiche jamais.

## 1. Variables de `backend-django/.env`

| Variable | Valeur attendue |
|---|---|
| `PAIEMENT_FOURNISSEUR` | `geniuspay` |
| `GENIUSPAY_ENVIRONNEMENT` | `sandbox` |
| `GENIUSPAY_API_KEY` | clé `sk_sandbox_…` du tableau de bord (en-tête `X-API-Key`) |
| `GENIUSPAY_API_SECRET` | clé `ss_sandbox_…` du tableau de bord (en-tête `X-API-Secret`) |
| `GENIUSPAY_WEBHOOK_SECRET` | **ne pas la remplir** : `creer-webhook` l'écrit |
| `ALLOWED_HOSTS_DEV` | hôte du tunnel, sans `https://` (`xxx.trycloudflare.com`), ou `.trycloudflare.com` pour accepter tous les tunnels rapides, dont l'hôte change à chaque lancement |
| `FRONTEND_BASE_URL` | facultatif, voir ci-dessous |

`ALLOWED_HOSTS_DEV` n'est lue que par `config/settings/dev.py` (plusieurs hôtes séparés par des virgules). `prod.py` ne la connaît pas.

**Retour du client** : après le paiement, GeniusPay renvoie le navigateur vers `FRONTEND_BASE_URL/paiement/retour?reference=PAY-…`. Le défaut (`http://localhost:5173`, le frontend Vite local) convient puisque le navigateur est sur le même poste. Si GeniusPay refuse une adresse de retour qui n'est pas en HTTPS (`payer` échoue alors avec `VALIDATION_ERROR`), mettre l'adresse du tunnel dans `FRONTEND_BASE_URL` : la page de retour sera une 404 de Django, sans effet sur l'essai, dont le résultat se lit avec `etat`.

Après toute modification de `.env`, **redémarrer le serveur Django** (il ne le relit pas tout seul) : `docker compose restart backend-django` depuis `infra/`.

## 2. Installer cloudflared (Windows)

```powershell
winget install --id Cloudflare.cloudflared
```

Rouvrir le terminal, puis vérifier : `cloudflared --version`. Aucun compte Cloudflare n'est nécessaire pour un tunnel rapide.

## 3. Ordre des commandes

Les commandes `manage.py` se lancent là où tourne Django : dans le conteneur (`docker compose exec backend-django python manage.py …` depuis `infra/`) ou dans l'environnement virtuel de `backend-django/`.

1. Démarrer la pile de dev (`docker compose up -d` depuis `infra/`), avec les variables du § 1 sauf l'hôte du tunnel si l'on n'utilise pas `.trycloudflare.com`.
2. Ouvrir le tunnel et le laisser tourner dans son terminal :
   ```powershell
   cloudflared tunnel --url http://localhost:8000
   ```
   Relever l'adresse `https://xxx.trycloudflare.com`.
3. Mettre l'hôte dans `ALLOWED_HOSTS_DEV` (inutile avec `.trycloudflare.com`), redémarrer Django, puis vérifier le tunnel : `https://xxx.trycloudflare.com/api/docs/` doit s'afficher.
4. Déclarer le webhook :
   ```
   python manage.py essai_geniuspay creer-webhook --url https://xxx.trycloudflare.com
   ```
   La commande déclare `https://xxx.trycloudflare.com/api/paiements/webhook/geniuspay/` pour les événements `payment.*` et `webhook.test`, écrit le secret renvoyé dans `.env` et affiche seulement « secret enregistré dans .env » et l'identifiant du webhook. Si `GENIUSPAY_WEBHOOK_SECRET` a déjà une valeur, elle demande confirmation **avant** de créer le webhook.
5. **Redémarrer Django** (nouveau secret).
6. Depuis le tableau de bord GeniusPay, envoyer l'événement d'essai (`webhook.test`) vers ce webhook et lire le journal de Django (§ 4).
7. Ouvrir un paiement :
   ```
   python manage.py essai_geniuspay payer --montant 500
   ```
   La commande crée ou réutilise un client, une boutique (livraison offerte : le montant payé est exactement `--montant`) et un article de test, passe par le vrai checkout puis par le vrai service d'initiation. Elle affiche la référence `PAY-…`, la référence GeniusPay et l'URL de paiement. Options : `--email` et `--telephone` du client transmis à GeniusPay (si l'adresse d'essai par défaut, en `.test`, est refusée).
8. Ouvrir l'URL de paiement dans le navigateur et payer avec les moyens de test de la sandbox.
9. Lire l'état :
   ```
   python manage.py essai_geniuspay etat --reference PAY-…
   ```
   Affiche le paiement, ses commandes, les notifications journalisées, puis le résultat du `GET /payments/{reference}`, sans rien modifier. `--reconcilier` applique en plus l'état constaté, par le même chemin que la réconciliation planifiée : utile si la notification n'a pas pu être acceptée (signature à corriger) et que l'on veut tout de même observer la suite.

La commande refuse de tourner si `DEBUG` est désactivé, si les settings de production sont chargés, si `PAIEMENT_FOURNISSEUR` n'est pas `geniuspay`, ou si l'environnement et les clés ne sont pas ceux de la sandbox.

Un nouveau tunnel a une nouvelle adresse : refaire les étapes 3 à 5 (un nouveau webhook, un nouveau secret) et supprimer l'ancien webhook dans le tableau de bord.

## 4. Ce que l'on doit observer

Journal de Django (`docker compose logs -f backend-django` depuis `infra/`), lignes `INFO apps.paiements…`. Elles ne contiennent ni clé, ni secret, ni signature, ni donnée du client.

| Moment | Ligne attendue |
|---|---|
| Webhook reçu | `Notification GeniusPay reçue : N octets, événement annoncé « payment.success », environnement annoncé « sandbox », signature hexadécimale (64 caractères).` |
| Signature valide | `Notification GeniusPay : signature valide (encodage hex).` |
| Forme du corps | `Notification GeniusPay payment.success : champs […] ; data […] ; amount de type Decimal ; référence « MTX-… ».` |
| GET de vérification | `GeniusPay GET /payments/MTX-… : HTTP 200.` puis `Vérification GeniusPay de MTX-… (paiement PAY-…) : statut « completed », amount de type …, devise « XOF », environnement « sandbox », metadata [reference].` |
| Commande payée | `Paiement PAY-… validé (1 commande(s) confirmée(s)).` ; `etat` affiche le paiement `valide`, la commande `confirmee` et `Notification payment.success : traite` |
| Événement d'essai | `Notification webhook.test` acquittée par 200, `Notification geniuspay ignorée : essai du webhook (webhook.test).` |

En cas de refus, la raison est sur une ligne `WARNING securite Notification geniuspay refusée : …` (signature invalide, horodatage hors tolérance, secret non configuré) ou `… incohérente pour PAY-… : …` (référence, environnement ou metadata du GET).

## 5. Points à confirmer (à noter pendant l'essai)

| Point | Où le lire | Si ce n'est pas le cas |
|---|---|---|
| Encodage de la signature : hexadécimal ? | ligne « reçue » : `signature hexadécimale (64 caractères)` ; `base64 probable (44 caractères)` ou `de forme inconnue` sinon | `ENCODAGE_SIGNATURE` dans `fournisseurs/geniuspay.py` |
| Secret `whsec_…` utilisé tel quel comme clé HMAC | la ligne « signature valide » apparaît | signature refusée alors que sa forme est hexadécimale : clé à dériver (préfixe à retirer, décodage) |
| Signature calculée sur le corps brut reçu | idem | la documentation ré-encode le JSON dans son exemple : à signaler si le corps brut ne convient pas |
| `metadata` renvoyée par le GET | ligne « Vérification » : `metadata [reference]` | `etat` affiche `refusée (NotificationInvalide) … (metadata)` : le recoupement par metadata est à revoir |
| Corps de `webhook.test` | ligne « Notification GeniusPay webhook.test : champs […] ; data […] » | noter les champs ; un `environment` absent est déjà accepté pour cet événement |
| Montant du webhook : décimal ou entier | `amount de type Decimal` (10000.00), `int` (10000) ou `str` | sans effet sur la validation (seul le GET fait foi) ; à documenter |
| Montant du GET | ligne « Vérification » : `amount de type …` | un `str` serait refusé comme montant : `_montant` à adapter |
| Champ `reference` du webhook : `MTX-…` ou autre | ligne « Forme du corps » : `référence « … »`, à comparer à la référence GeniusPay affichée par `payer` | `Notification geniuspay pour un paiement inconnu` : le champ qui porte la référence est à revoir |
| Champ du secret dans la réponse de `POST /webhooks` | `creer-webhook` réussit | elle échoue en listant les **noms** des champs reçus : les transmettre, et supprimer le webhook créé |
| Adresse de retour en `http://localhost` acceptée | `payer` réussit | voir § 1, « Retour du client » |
| Adresse email en `.test` acceptée | `payer` réussit | `--email` |

## 6. Après l'essai

- Supprimer le webhook d'essai dans le tableau de bord GeniusPay et fermer le tunnel.
- Remettre `PAIEMENT_FOURNISSEUR=simule` dans `.env` (ou retirer la ligne) et redémarrer Django : `seed_demo` et les collections Postman en dépendent.
- Les données de test restent en base (comptes `…@essai.anitche.test`, boutique « Essai GeniusPay ») et sont réutilisées au prochain essai. Une commande d'essai non payée s'annule seule à la fin du délai de paiement.
