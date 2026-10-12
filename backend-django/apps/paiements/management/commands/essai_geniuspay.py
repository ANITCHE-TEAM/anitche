"""Essai réel de GeniusPay en sandbox (développement), pas à pas dans
docs/ESSAI_GENIUSPAY_SANDBOX.md.

    python manage.py essai_geniuspay creer-webhook --url https://xxx.trycloudflare.com
    python manage.py essai_geniuspay payer --montant 500
    python manage.py essai_geniuspay etat --reference PAY-...

Refusée hors développement : DEBUG désactivé, settings de production,
fournisseur actif autre que geniuspay, environnement ou clés autres que ceux
de la sandbox. Aucune clé ni aucun secret n'est jamais affiché : le secret du
webhook, renvoyé une seule fois par GeniusPay, va directement dans le fichier
.env.
"""

import os
import re
import tempfile
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.http.request import validate_host
from rest_framework.test import APIClient

from apps.catalogue.models import Produit, Stock, VarianteProduit
from apps.commandes.models import Commande
from apps.paiements import services
from apps.paiements.fournisseurs.base import (
    ErreurFournisseur,
    MontantHorsLimites,
    NotificationInvalide,
    TransactionIntrouvable,
)
from apps.paiements.fournisseurs.geniuspay import PAIEMENT_MIN, FournisseurGeniusPay
from apps.paiements.models import JournalWebhook, Paiement
from apps.panier.models import Panier, PanierItem
from apps.utilisateurs.models import DocumentKYC, Role, StatutKYC, TypePieceIdentite, Utilisateur
from apps.vendeurs.models import Boutique

ENVIRONNEMENT = "sandbox"
PREFIXES_DES_CLES = (("GENIUSPAY_API_KEY", "sk_sandbox_"), ("GENIUSPAY_API_SECRET", "ss_sandbox_"))

VARIABLE_SECRET = "GENIUSPAY_WEBHOOK_SECRET"
LIGNE_SECRET = re.compile(rf"^\s*(?:export\s+)?{VARIABLE_SECRET}\s*=(.*)$")
CHEMIN_WEBHOOK = "/api/paiements/webhook/geniuspay/"
NOM_WEBHOOK = "ANITCHE essai sandbox"
#: Tous les événements de paiement de la documentation, et l'essai du webhook.
EVENEMENTS_WEBHOOK = (
    "payment.initiated", "payment.success", "payment.failed", "payment.cancelled", "payment.refunded",
    "payment.expired", "webhook.test",
)
#: Noms possibles du champ qui porte le secret (la documentation n'en donne pas l'exemple).
CHAMPS_SECRET = ("secret", "signing_secret", "webhook_secret")
PREFIXE_SECRET = "whsec_"

EMAIL_CLIENT = "client.essai-geniuspay@essai.anitche.test"
EMAIL_VENDEUR = "vendeur.essai-geniuspay@essai.anitche.test"
NOM_BOUTIQUE = "Essai GeniusPay"
NOM_PRODUIT = "Article d'essai GeniusPay"
STOCK_D_ESSAI = 100
ADRESSE = {"commune": "Cocody", "quartier": "Angré", "point_de_repere": "Essai GeniusPay", "telephone": "0700000001"}
URL_VALIDER_PANIER = "/api/commandes/valider-panier/"


def verifier_environnement_d_essai():
    """Lève CommandError hors d'un environnement de développement branché sur
    la sandbox de GeniusPay. Le nom d'un réglage peut être cité, jamais sa valeur."""
    raisons = []
    if not settings.DEBUG:
        raisons.append("DEBUG est désactivé")
    if str(getattr(settings, "SETTINGS_MODULE", "")).endswith(".prod"):
        raisons.append("les settings de production sont chargés")
    if settings.PAIEMENT_FOURNISSEUR != FournisseurGeniusPay.code:
        raisons.append("PAIEMENT_FOURNISSEUR n'est pas geniuspay")
    if settings.GENIUSPAY_ENVIRONNEMENT != ENVIRONNEMENT:
        raisons.append(f"GENIUSPAY_ENVIRONNEMENT n'est pas {ENVIRONNEMENT}")
    for nom, prefixe in PREFIXES_DES_CLES:
        if not str(getattr(settings, nom, "")).startswith(prefixe):
            raisons.append(f"{nom} ne commence pas par {prefixe}")
    if raisons:
        raise CommandError(
            "essai_geniuspay est réservée au développement en sandbox : " + ", ".join(raisons)
            + ". Rien n'a été fait."
        )


def chemin_env():
    """Fichier .env lu par les settings (backend-django/.env)."""
    return Path(settings.BASE_DIR) / ".env"


def _lire_env(chemin):
    # newline="" : les fins de ligne du fichier sont conservées telles quelles.
    with open(chemin, encoding="utf-8", newline="") as fichier:
        return fichier.read()


def secret_deja_dans_env(chemin):
    """La variable du secret a-t-elle déjà une valeur dans le fichier ?"""
    for ligne in _lire_env(chemin).splitlines():
        trouve = LIGNE_SECRET.match(ligne)
        if trouve and trouve.group(1).strip().strip("'\""):
            return True
    return False


def enregistrer_secret_dans_env(chemin, secret):
    """Écrit GENIUSPAY_WEBHOOK_SECRET=<secret> dans le fichier : la ligne
    existante est remplacée (un doublon est retiré), sinon la variable est
    ajoutée à la fin. Les autres lignes ne changent pas. Écriture dans un
    fichier voisin puis remplacement : le .env n'est jamais laissé tronqué."""
    texte = _lire_env(chemin)
    fin = "\r\n" if "\r\n" in texte else "\n"
    lignes, ecrit = [], False
    for ligne in texte.splitlines(keepends=True):
        if not LIGNE_SECRET.match(ligne.rstrip("\r\n")):
            lignes.append(ligne)
        elif not ecrit:
            lignes.append(f"{VARIABLE_SECRET}={secret}{fin}")
            ecrit = True
    if not ecrit:
        if lignes and not lignes[-1].endswith(("\n", "\r")):
            lignes[-1] += fin
        lignes.append(f"{VARIABLE_SECRET}={secret}{fin}")
    descripteur, provisoire = tempfile.mkstemp(dir=chemin.parent, prefix=".env.", suffix=".tmp")
    try:
        with open(descripteur, "w", encoding="utf-8", newline="") as fichier:
            fichier.write("".join(lignes))
        os.replace(provisoire, chemin)
    except BaseException:
        if os.path.exists(provisoire):
            os.remove(provisoire)
        raise


def _objets(data):
    """La réponse et ses objets imbriqués directs (« webhook »...)."""
    return [data, *(valeur for valeur in data.values() if isinstance(valeur, dict))]


def secret_du_webhook(data):
    """Secret de signature dans la réponse de création, ou None : un champ au
    nom connu, sinon toute valeur de la forme « whsec_… »."""
    for objet in _objets(data):
        for champ in CHAMPS_SECRET:
            if isinstance(objet.get(champ), str) and objet[champ]:
                return objet[champ]
    for objet in _objets(data):
        for valeur in objet.values():
            if isinstance(valeur, str) and valeur.startswith(PREFIXE_SECRET):
                return valeur
    return None


def identifiant_du_webhook(data):
    for objet in _objets(data):
        if isinstance(objet.get("id"), (str, int)) and not isinstance(objet.get("id"), bool):
            return objet["id"]
    return "inconnu"


def _hote_local():
    """Hôte accepté par ALLOWED_HOSTS pour l'appel interne à l'API."""
    for hote in ("localhost", "testserver", "127.0.0.1"):
        if validate_host(hote, settings.ALLOWED_HOSTS):
            return hote
    return "localhost"


class Command(BaseCommand):
    help = "Essai réel de GeniusPay en sandbox (développement) : creer-webhook, payer, etat."

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True, metavar="{creer-webhook,payer,etat}")

        webhook = actions.add_parser(
            "creer-webhook", help="Déclare le webhook chez GeniusPay et enregistre son secret dans .env.")
        webhook.add_argument("--url", required=True,
                             help="Adresse publique du serveur local (https://xxx.trycloudflare.com).")

        payer = actions.add_parser("payer", help="Commande de test puis initiation d'un paiement GeniusPay.")
        payer.add_argument("--montant", type=int, required=True, help="Montant à payer, en FCFA.")
        payer.add_argument("--email", default=EMAIL_CLIENT,
                           help="Email du client de test, transmis à GeniusPay (défaut : compte d'essai).")
        payer.add_argument("--telephone", default="",
                           help="Téléphone du client de test, transmis à GeniusPay (facultatif).")

        etat = actions.add_parser("etat", help="État d'un paiement et vérification auprès de GeniusPay.")
        etat.add_argument("--reference", required=True, help="Référence du paiement (PAY-...).")
        etat.add_argument("--reconcilier", action="store_true",
                          help="Applique en plus l'état constaté (même chemin que la réconciliation planifiée).")

    def handle(self, *args, action, **options):
        verifier_environnement_d_essai()
        if action == "creer-webhook":
            self.creer_webhook(options["url"])
        elif action == "payer":
            self.payer(options["montant"], options["email"], options["telephone"])
        else:
            self.etat(options["reference"], options["reconcilier"])

    # --- creer-webhook --------------------------------------------------------

    def creer_webhook(self, url_publique):
        url = self._url_du_webhook(url_publique)
        env = chemin_env()
        # Tout ce qui peut échouer est vérifié AVANT l'appel : le secret n'est
        # renvoyé qu'une fois, il ne doit pas être perdu après la création.
        if not env.is_file() or not os.access(env, os.W_OK) or not os.access(env.parent, os.W_OK):
            raise CommandError(f"Fichier .env introuvable ou non modifiable ({env}). Aucun webhook n'a été créé.")
        if secret_deja_dans_env(env) and not self._confirmer(
            f"{VARIABLE_SECRET} a déjà une valeur dans .env. La remplacer par le secret d'un nouveau webhook ? [o/N] "
        ):
            raise CommandError("Abandon : aucun webhook n'a été créé, .env n'a pas changé.")

        try:
            data = FournisseurGeniusPay().creer_webhook(NOM_WEBHOOK, url, EVENEMENTS_WEBHOOK)
        except ErreurFournisseur as erreur:
            raise CommandError(f"Création du webhook refusée : {erreur} .env n'a pas changé.") from None
        identifiant = identifiant_du_webhook(data)
        secret = secret_du_webhook(data)
        if secret is None or not secret.isprintable() or any(caractere.isspace() for caractere in secret):
            champs = ", ".join(sorted(str(cle) for objet in _objets(data) for cle in objet))
            raise CommandError(
                f"Webhook créé (id {identifiant}) mais secret introuvable ou inattendu dans la réponse "
                f"(champs reçus : {champs}). .env n'a pas changé : supprimez ce webhook dans le tableau de bord "
                "GeniusPay et signalez les champs reçus."
            )
        enregistrer_secret_dans_env(env, secret)

        self.stdout.write(self.style.SUCCESS("secret enregistré dans .env"))
        self.stdout.write(f"Webhook {identifiant} : {url}")
        self.stdout.write(f"Événements : {', '.join(EVENEMENTS_WEBHOOK)}")
        if VARIABLE_SECRET in os.environ:
            self.stdout.write(self.style.WARNING(
                f"{VARIABLE_SECRET} est aussi définie dans l'environnement du processus : elle l'emporte sur .env."
            ))
        self.stdout.write("Redémarrez le serveur Django pour qu'il relise .env.")

    def _url_du_webhook(self, url_publique):
        parties = urlsplit(url_publique.strip())
        if parties.scheme != "https" or not parties.hostname or parties.query or parties.fragment:
            raise CommandError("--url doit être une adresse publique en https (https://xxx.trycloudflare.com).")
        if parties.path not in ("", "/", CHEMIN_WEBHOOK):
            raise CommandError(f"--url ne doit porter que l'hôte : le chemin {CHEMIN_WEBHOOK} est ajouté.")
        if not validate_host(parties.hostname, settings.ALLOWED_HOSTS):
            raise CommandError(
                f"{parties.hostname} n'est pas dans ALLOWED_HOSTS : Django refuserait les notifications (400). "
                "Ajoutez-le à ALLOWED_HOSTS_DEV dans .env. Aucun webhook n'a été créé."
            )
        return f"https://{parties.netloc}{CHEMIN_WEBHOOK}"

    def _confirmer(self, question):
        try:
            return input(question).strip().lower() in ("o", "oui")
        except EOFError:
            return False

    # --- payer ----------------------------------------------------------------

    def payer(self, montant, email, telephone):
        if montant < PAIEMENT_MIN:
            raise CommandError(f"--montant : GeniusPay accepte des paiements à partir de {PAIEMENT_MIN} FCFA.")
        with transaction.atomic():
            client, variante = self._donnees_de_test(Decimal(montant), email, telephone)
            panier, _ = Panier.objects.get_or_create(utilisateur=client)
            panier.items.all().delete()
            PanierItem.objects.create(panier=panier, variante=variante, quantite=1)
        commande = self._valider_panier(client)
        try:
            paiement = services.initier_paiement(client, Paiement.Methode.WAVE, commande_id=commande.pk)
        except services.PaiementRefuse as refus:
            raise CommandError(f"Paiement refusé : {refus.args[0]}") from None
        except (MontantHorsLimites, ErreurFournisseur) as erreur:
            raise CommandError(f"Initiation refusée par GeniusPay : {erreur}") from None

        self.stdout.write(f"Commande            : {commande.numero_commande}")
        self.stdout.write(f"Référence ANITCHE   : {paiement.reference}")
        self.stdout.write(f"Référence GeniusPay : {paiement.transaction_id_externe}")
        self.stdout.write(f"Montant             : {int(paiement.montant)} {paiement.devise}")
        self.stdout.write(f"URL de paiement     : {paiement.url_paiement}")
        if int(paiement.montant) != montant:
            self.stdout.write(self.style.WARNING(f"Le montant à payer diffère du montant demandé ({montant})."))
        self.stdout.write(f"Ensuite : python manage.py essai_geniuspay etat --reference {paiement.reference}")

    def _donnees_de_test(self, prix, email, telephone):
        """Client, boutique (livraison offerte : le montant payé est le prix
        de l'article) et article de test, créés ou réutilisés."""
        client = Utilisateur.objects.filter(email__iexact=email).first()
        if client is None:
            client = Utilisateur.objects.create_user(
                email=email, password=None, nom="Essai", prenom="Client", email_verifie=True,
            )
        if telephone and client.telephone != telephone:
            client.telephone = telephone
            client.save(update_fields=["telephone"])
        if not client.email_verifie:
            raise CommandError(f"Le compte {email} existe et son email n'est pas vérifié : choisissez un autre --email.")

        vendeur = Utilisateur.objects.filter(email__iexact=EMAIL_VENDEUR).first()
        if vendeur is None:
            vendeur = Utilisateur.objects.create_user(
                email=EMAIL_VENDEUR, password=None, nom="Essai", prenom="Vendeur", role=Role.VENDEUR,
                statut_kyc=StatutKYC.VALIDE, email_verifie=True,
            )
        DocumentKYC.objects.get_or_create(utilisateur=vendeur, defaults={
            "type_piece": TypePieceIdentite.PASSEPORT, "piece_identite_recto": "kyc/essai-geniuspay.pdf",
            "selfie": "kyc/essai-geniuspay.png", "numero_mobile_money": "0700000000", "adresse": "Cocody",
        })
        boutique, _ = Boutique.objects.get_or_create(
            proprietaire=vendeur, defaults={"nom": NOM_BOUTIQUE, "livraison_offerte": True},
        )
        if not boutique.livraison_offerte:
            boutique.livraison_offerte = True
            boutique.save(update_fields=["livraison_offerte"])
        if not boutique.est_publiable:
            raise CommandError(f"La boutique de test « {boutique.nom} » n'est plus publiable (fermée ou suspendue).")

        produit, _ = Produit.objects.get_or_create(boutique=boutique, nom=NOM_PRODUIT, defaults={"prix_base": prix})
        variante, _ = VarianteProduit.objects.get_or_create(produit=produit, nom="Standard", defaults={"prix": prix})
        if variante.prix != prix or variante.prix_promo is not None:
            variante.prix, variante.prix_promo = prix, None
            variante.save(update_fields=["prix", "prix_promo"])
        Stock.objects.filter(variante=variante, quantite_disponible__lt=1).update(quantite_disponible=STOCK_D_ESSAI)
        return client, variante

    def _valider_panier(self, client):
        """Checkout par la vraie vue (montants, stock, frais figés)."""
        api = APIClient(HTTP_HOST=_hote_local())
        api.force_authenticate(user=client)
        reponse = api.post(URL_VALIDER_PANIER, {"adresse_livraison": ADRESSE}, format="json")
        if reponse.status_code != 201:
            detail = getattr(reponse, "data", None) or reponse.content[:300]
            raise CommandError(f"Validation du panier refusée (HTTP {reponse.status_code}) : {detail}")
        return Commande.objects.get(pk=reponse.data[0]["id"])

    # --- etat -----------------------------------------------------------------

    def etat(self, reference, reconcilier):
        paiement = Paiement.objects.filter(reference=reference).first()
        if paiement is None:
            raise CommandError(f"Paiement {reference} introuvable.")
        if paiement.fournisseur != FournisseurGeniusPay.code:
            raise CommandError(f"Paiement {reference} ouvert chez {paiement.fournisseur}, pas chez GeniusPay.")
        self._afficher(paiement)

        self.stdout.write("Vérification serveur à serveur (GET /payments/{reference}) :")
        try:
            etat = FournisseurGeniusPay().verifier_transaction(paiement)
        except (ErreurFournisseur, TransactionIntrouvable, NotificationInvalide) as erreur:
            self.stdout.write(self.style.ERROR(f"  refusée ({type(erreur).__name__}) : {erreur}"))
        else:
            self.stdout.write(f"  statut {etat.statut}, montant {etat.montant}, devise {etat.devise}")

        if reconcilier:
            issue, change = services.reconcilier_paiement(paiement)
            self.stdout.write(f"Réconciliation : issue {issue}, statut {'modifié' if change else 'inchangé'}.")
            paiement.refresh_from_db()
            self._afficher(paiement)

    def _afficher(self, paiement):
        self.stdout.write(
            f"Paiement {paiement.reference} : {paiement.statut}, {int(paiement.montant)} {paiement.devise}, "
            f"référence GeniusPay {paiement.transaction_id_externe or 'aucune'}"
        )
        for commande in paiement.commandes.order_by("pk"):
            self.stdout.write(f"  Commande {commande.numero_commande} : {commande.status}")
        notifications = JournalWebhook.objects.filter(
            fournisseur=FournisseurGeniusPay.code, payload__reference=paiement.transaction_id_externe,
        ).order_by("date_reception") if paiement.transaction_id_externe else []
        for journal in notifications:
            suite = f" ({journal.erreur})" if journal.erreur else ""
            self.stdout.write(f"  Notification {journal.payload.get('event')} : {journal.statut_traitement}{suite}")
        if not notifications:
            self.stdout.write("  Aucune notification journalisée pour ce paiement.")
