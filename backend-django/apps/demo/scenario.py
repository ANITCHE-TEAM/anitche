"""Scénario de démonstration : construit les données en passant par l'API.

Chaque étape métier passe par le même chemin qu'un vrai utilisateur (vues,
serializers, permissions, services, signaux) via APIClient, avec les comptes
de démo authentifiés. Seuls trois points n'ont pas d'API et utilisent
directement le code métier :
  - la création des comptes (create_user, email déjà vérifié : pas de code
    OTP à lire) et l'attribution des rôles admin/support (back-office) ;
  - les catégories (gérées dans l'admin Django) ;
  - le crédit des points de fidélité, fait par une tâche périodique : ses
    fonctions (apps.fidelite.services) sont appelées avec une horloge
    décalée au-delà du délai de rétractation.

Les paiements passent par le fournisseur simulé : initiation par l'API, puis
notification signée (HMAC) envoyée au webhook, comme un vrai fournisseur.
"""

import io
import json
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http.request import validate_host
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFont
from rest_framework.test import APIClient

from apps.catalogue.models import Categorie
from apps.fidelite.services import crediter_gain, gains_echus
from apps.paiements.fournisseurs.simule import EN_TETE_SIGNATURE, signer
from apps.utilisateurs.models import Role, Utilisateur

from . import donnees


class ErreurScenario(Exception):
    """Un appel de l'API n'a pas donné le résultat attendu."""


# =====================================================================
# IMAGES GÉNÉRÉES (Pillow)
# =====================================================================

def _police(taille):
    try:
        return ImageFont.load_default(size=taille)
    except TypeError:  # Pillow < 10.1 : police fixe
        return ImageFont.load_default()


def image_png(texte, couleur, nom='image.png', taille=(800, 800)):
    """Image PNG unie portant un texte centré (vignettes, KYC de démo)."""
    image = Image.new('RGB', taille, couleur)
    dessin = ImageDraw.Draw(image)
    police = _police(taille[0] // 12)
    lignes = texte.split('\n')
    hauteur_ligne = taille[0] // 10
    y = (taille[1] - hauteur_ligne * len(lignes)) // 2
    for ligne in lignes:
        largeur = dessin.textlength(ligne, font=police)
        dessin.text(((taille[0] - largeur) / 2, y), ligne, fill=(255, 255, 255), font=police)
        y += hauteur_ligne
    tampon = io.BytesIO()
    image.save(tampon, format='PNG')
    return SimpleUploadedFile(nom, tampon.getvalue(), content_type='image/png')


def _texte_vignette(nom):
    """Nom du produit sur deux lignes au plus (vignette carrée)."""
    mots = nom.split()
    if len(mots) <= 2:
        return nom
    milieu = (len(mots) + 1) // 2
    return f"{' '.join(mots[:milieu])}\n{' '.join(mots[milieu:])}"


# =====================================================================
# SCÉNARIO
# =====================================================================

class ScenarioDemo:
    """Construit toutes les données de démo. Appelé par seed_demo dans une
    transaction : en cas d'échec, rien n'est enregistré."""

    def __init__(self, journal=None):
        self.api = APIClient(HTTP_HOST=self._hote_autorise())
        self.journal = journal or (lambda message: None)
        self.boutiques = {}   # clé -> {'vendeur', 'boutique_id', 'variantes': {(produit, variante): id}}

    @staticmethod
    def _hote_autorise():
        # dev : ALLOWED_HOSTS = localhost... ; tests : « testserver ».
        for hote in ('localhost', 'testserver', '127.0.0.1'):
            if validate_host(hote, settings.ALLOWED_HOSTS):
                return hote
        return 'localhost'

    # -----------------------------------------------------------------
    # Appels API
    # -----------------------------------------------------------------

    def appeler(self, utilisateur, methode, url, donnees=None, attendu=(200, 201), multipart=False, **en_tetes):
        # Relu en base à chaque appel, comme le fait l'authentification JWT :
        # rôle et statut KYC changent en cours de scénario (validation vendeur...).
        utilisateur.refresh_from_db()
        self.api.force_authenticate(user=utilisateur)
        format_corps = 'multipart' if multipart else 'json'
        reponse = getattr(self.api, methode)(url, donnees, format=format_corps, **en_tetes)
        self.api.force_authenticate(user=None)
        if reponse.status_code not in attendu:
            corps = getattr(reponse, 'data', None) or reponse.content[:500]
            raise ErreurScenario(f"{methode.upper()} {url} → HTTP {reponse.status_code} : {corps}")
        return reponse.data

    # -----------------------------------------------------------------
    # Étapes
    # -----------------------------------------------------------------

    def executer(self):
        self.creer_comptes()
        self.creer_categories()
        for definition in donnees.BOUTIQUES:
            self.ouvrir_boutique(definition)
        self.admin_nomme_livreur()
        self.creer_commandes()
        self.crediter_points_et_convertir()
        self.ouvrir_ticket_support()

    def _compte(self, infos, role=Role.CLIENT):
        return Utilisateur.objects.create_user(
            email=infos['email'],
            password=donnees.MOT_DE_PASSE_DEMO,
            nom=infos['nom'],
            prenom=infos['prenom'],
            role=role,
            email_verifie=True,
        )

    def creer_comptes(self):
        self.admin = self._compte(donnees.ADMIN, Role.ADMIN)
        self.support = self._compte(donnees.SUPPORT, Role.SUPPORT)
        self.client = self._compte(donnees.CLIENT)
        # Client pour l'instant : l'administration le nomme livreur par l'API.
        self.livreur = self._compte(donnees.LIVREUR)
        for definition in donnees.BOUTIQUES:
            self.boutiques[definition['cle']] = {'vendeur': self._compte(definition['vendeur']), 'variantes': {}}
        self.journal("Comptes créés.")

    def creer_categories(self):
        self.categories = {}
        for definition in donnees.CATEGORIES:
            categorie, _ = Categorie.objects.get_or_create(
                nom=definition['nom'],
                defaults={'description': definition['description'], 'ordre': definition['ordre']},
            )
            self.categories[definition['nom']] = categorie
        self.journal("Catégories prêtes.")

    def ouvrir_boutique(self, definition):
        etat = self.boutiques[definition['cle']]
        vendeur = etat['vendeur']
        couleur = definition['couleur']

        # Demande vendeur : dépôt du dossier KYC, puis validation par l'administration.
        self.appeler(vendeur, 'post', '/api/utilisateurs/upload-kyc/', {
            'type_piece': 'passeport',
            'piece_identite_recto': image_png('PASSEPORT\nDÉMO', couleur, 'passeport.png', (600, 400)),
            'selfie': image_png('SELFIE\nDÉMO', couleur, 'selfie.png', (400, 400)),
            'numero_mobile_money': '0707070707',
            'adresse': f"{definition['ville']}, Côte d'Ivoire",
        }, multipart=True)
        self.appeler(self.admin, 'post', f'/api/vendeurs/administration/demandes/{vendeur.pk}/valider/', {
            'commentaire': 'Dossier de démonstration.',
        })

        boutique = self.appeler(vendeur, 'post', '/api/vendeurs/ma-boutique/', {
            'nom': definition['nom'],
            'description': definition['description'],
            'ville': definition['ville'],
            'adresse': f"{definition['ville']}, Côte d'Ivoire",
            'telephone_contact': '+2250102030405',
            'email_contact': vendeur.email,
            'livraison_offerte': definition['livraison_offerte'],
            # Explicite : en multipart, DRF lit un booléen absent comme False
            # (la boutique serait créée fermée).
            'est_active': True,
            'logo': image_png(definition['nom'].replace(' & ', '\n& '), couleur, 'logo.png', (400, 400)),
        }, multipart=True)
        etat['boutique_id'] = boutique['id']

        categorie = self.categories[definition['categorie']]
        for produit in definition['produits']:
            cree = self.appeler(vendeur, 'post', '/api/catalogue/vendeur/produits/', {
                'nom': produit['nom'],
                'description': produit['description'],
                'categorie': categorie.pk,
                'prix_base': produit['variantes'][0][1],
                'est_actif': True,
            })
            for nom_variante, prix, prix_promo, stock in produit['variantes']:
                variante = self.appeler(vendeur, 'post', f"/api/catalogue/vendeur/produits/{cree['id']}/variantes/", {
                    'nom': nom_variante,
                    'prix': prix,
                    'prix_promo': prix_promo,
                    'quantite_initiale': stock,
                    'est_active': True,
                })
                etat['variantes'][(produit['nom'], nom_variante)] = variante['id']
            self.appeler(vendeur, 'post', f"/api/catalogue/vendeur/produits/{cree['id']}/images/", {
                'image': image_png(_texte_vignette(produit['nom']), couleur, 'produit.png'),
                'est_principale': True,
            }, multipart=True)
        self.journal(f"Boutique « {definition['nom']} » ouverte ({len(definition['produits'])} produits).")

    def admin_nomme_livreur(self):
        self.appeler(self.admin, 'post', '/api/livraison/livreurs/nommer/', {'utilisateur_id': self.livreur.pk})
        self.livreur.refresh_from_db()

    # -----------------------------------------------------------------
    # Commandes
    # -----------------------------------------------------------------

    def commander(self, cle_boutique, articles):
        """Panier → validation (une commande, une seule boutique). Renvoie la commande."""
        variantes = self.boutiques[cle_boutique]['variantes']
        for (produit, variante), quantite in articles:
            self.appeler(self.client, 'post', '/api/panier/panier/items/', {
                'variante': variantes[(produit, variante)], 'quantite': quantite,
            })
        commandes = self.appeler(self.client, 'post', '/api/commandes/valider-panier/', {
            'adresse_livraison': donnees.ADRESSE_LIVRAISON,
        })
        return commandes[0]

    def payer(self, commande):
        """Initiation (API) puis notification de succès signée du fournisseur simulé."""
        paiement = self.appeler(self.client, 'post', '/api/paiements/initier/', {
            'commande_id': commande['id'], 'methode': 'wave',
        })
        corps = json.dumps({
            'evenement_id': f'DEMO-{uuid.uuid4().hex}',
            'reference': paiement['reference'],
            'transaction_id': f'SIM-DEMO-{uuid.uuid4().hex[:12].upper()}',
            'statut': 'succes',
            'montant': int(Decimal(str(paiement['montant']))),
            'devise': paiement['devise'],
        })
        self.api.force_authenticate(user=None)
        reponse = self.api.post(
            '/api/paiements/webhook/simule/', corps, content_type='application/json',
            **{f"HTTP_{EN_TETE_SIGNATURE.upper().replace('-', '_')}": signer(corps, settings.PAIEMENT_SIMULE_SECRET)},
        )
        if reponse.status_code != 200:
            raise ErreurScenario(f"Notification de paiement refusée : HTTP {reponse.status_code} {reponse.content[:300]}")

    def preparer(self, cle_boutique, commande):
        vendeur = self.boutiques[cle_boutique]['vendeur']
        self.appeler(vendeur, 'post', f"/api/commandes/vendeur/{commande['id']}/preparation/")

    def livrer(self, commande):
        livraisons = self.appeler(self.client, 'get', '/api/livraison/')
        livraison_id = next(
            ligne['id'] for ligne in livraisons['results'] if str(ligne['commande']) == str(commande['id'])
        )
        self.appeler(self.admin, 'post', f'/api/livraison/{livraison_id}/assigner/', {
            'livreur_id': self.livreur.pk,
            'date_livraison_estimee': (timezone.localdate() + timedelta(days=1)).isoformat(),
        })
        url_statut = f'/api/livraison/{livraison_id}/statut/'
        self.appeler(self.livreur, 'patch', url_statut, {'status': 'expediee'})
        self.appeler(self.livreur, 'patch', url_statut, {'status': 'en_cours'})
        # Le client lit son code sur le suivi et le donne au livreur.
        code = self.appeler(self.client, 'get', f'/api/livraison/{livraison_id}/')['code_livraison']
        self.appeler(self.livreur, 'patch', url_statut, {'status': 'livree', 'code': code})

    def creer_commandes(self):
        # 1. Livrée : ouvre des points de fidélité (65 000 FCFA → 65 points).
        self.commande_livree = self.commander('tech', [(('Smartphone Tecno Spark 20', 'Noir 128 Go'), 1)])
        self.payer(self.commande_livree)
        self.preparer('tech', self.commande_livree)
        self.livrer(self.commande_livree)

        # 2. Livrée puis retour demandé par le client, approuvé par la boutique.
        commande_retour = self.commander('mode', [
            (('Chemise en wax', 'Taille M'), 1), (('Sac en pagne tissé', 'Modèle standard'), 1),
        ])
        self.payer(commande_retour)
        self.preparer('mode', commande_retour)
        self.livrer(commande_retour)
        detail = self.appeler(self.client, 'get', f"/api/commandes/{commande_retour['id']}/")
        chemise = next(article for article in detail['articles'] if article['nom_produit'] == 'Chemise en wax')
        demande = self.appeler(self.client, 'post', '/api/retours/', {
            'commande_id': commande_retour['id'],
            'motif': 'mauvaise_taille',
            'description': 'La taille M est trop juste, je souhaite un remboursement.',
            'articles': [{'commande_item_id': chemise['id'], 'quantite': 1}],
        })
        self.appeler(self.boutiques['mode']['vendeur'], 'patch', f"/api/retours/{demande['id']}/traiter/", {
            'action': 'approuver', 'reponse': 'Retour accepté : déposez le colis au point relais.',
        })

        # 3. Payée puis passée en préparation par la boutique.
        self.commande_preparation = self.commander('maison', [(('Marmite en fonte', '5 litres'), 1)])
        self.payer(self.commande_preparation)
        self.preparer('maison', self.commande_preparation)

        # 4. Payée (confirmée), en attente de préparation.
        commande_payee = self.commander('beaute', [
            (('Beurre de karité pur', '500 g'), 2), (('Savon noir', 'Pain de 200 g'), 3),
        ])
        self.payer(commande_payee)

        # 5. Annulée par le client avant paiement (stock restitué).
        commande_annulee = self.commander('maison', [(('Nappe en kita', 'Modèle standard'), 1)])
        self.appeler(self.client, 'post', f"/api/commandes/{commande_annulee['id']}/annuler/")
        self.journal("Commandes créées : livrée, livrée avec retour, en préparation, payée, annulée.")

    # -----------------------------------------------------------------
    # Fidélité et support
    # -----------------------------------------------------------------

    def crediter_points_et_convertir(self):
        """Points crédités comme le ferait la tâche périodique une fois le délai
        de rétractation écoulé (horloge décalée), limités au client de démo,
        puis conversion d'une partie des points en coupon par l'API."""
        apres_le_delai = timezone.now() + timedelta(days=settings.REVERSEMENT_DELAI_RETRACTATION_JOURS + 1)
        gains = gains_echus(maintenant=apres_le_delai).filter(compte__utilisateur=self.client)
        credites = [gain_id for gain_id in gains.values_list('pk', flat=True) if crediter_gain(gain_id)]
        if not credites:
            raise ErreurScenario("Aucun point de fidélité à créditer pour le client de démo.")
        self.coupon = self.appeler(self.client, 'post', '/api/fidelite/convertir-points/', {'option': '50_PTS_5PCT'})
        self.journal("Points de fidélité crédités et coupon créé.")

    def ouvrir_ticket_support(self):
        ticket = self.appeler(self.client, 'post', '/api/support/tickets/', {
            'subject': 'Date de livraison de ma marmite',
            'description': "Ma commande est en préparation depuis hier : quand sera-t-elle livrée ?",
            'category': 'delivery',
            'order': self.commande_preparation['id'],
        })
        self.appeler(self.support, 'post', f"/api/support/tickets/{ticket['id']}/messages/", {
            'content': 'Bonjour Awa, la boutique prépare votre colis : livraison prévue sous 48 h.',
        })
        self.journal("Ticket support ouvert, avec une réponse du support.")
