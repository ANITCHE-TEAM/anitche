import hashlib
import hmac
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import unquote

import requests
from cryptography.fernet import Fernet
from django.apps import apps as registre_apps
from django.conf import settings
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.urls import Resolver404, clear_url_caches, resolve
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle

from apps.catalogue.models import Produit, Stock, VarianteProduit
from apps.commandes.models import Commande, CommandeItem
from apps.commandes.services import annuler_commande, est_payee, expirer_commandes_impayees, synchroniser_depuis_livraison
from apps.fidelite.models import CompteFidelite, CouponReduction, GainFidelite
from apps.fidelite.services import crediter_gains_echus
from apps.livraison.models import Livraison, TarifLivraison
from apps.livraison.tests import recreer_tarifs_initiaux
from apps.notifications.models import Notification
from apps.panier.models import Panier, PanierItem
from apps.retours.models import DemandeRetour, RetourItem
from apps.utilisateurs.models import DocumentKYC, Role, StatutKYC, TypePieceIdentite, Utilisateur
from apps.vendeurs.models import Boutique

from . import reversements, services
from .fournisseurs import FOURNISSEURS, FournisseurInconnu, obtenir_fournisseur
from .fournisseurs import geniuspay
from .fournisseurs.base import (
    ECHEC,
    EN_ATTENTE,
    SUCCES,
    ErreurFournisseur,
    EtatTransaction,
    MontantHorsLimites,
    NotificationInvalide,
    OperationNonSupportee,
    TransactionIntrouvable,
    TransfertRefuse,
    hacher_jeton,
)
from .fournisseurs.geniuspay import FournisseurGeniusPay
from .fournisseurs.simule import (
    DELAI_APRES_ENVOI,
    EN_TETE_SIGNATURE,
    PANNE_AVANT_ENVOI,
    REFUS_CERTAIN,
    FournisseurSimule,
    definir_comportement_transfert,
    definir_etat_distant,
    signer,
)
from .frais import bareme_en_vigueur, calculer_frais_ligne
from .management.commands import essai_geniuspay
from .models import AjustementVendeur, BaremeFrais, JournalWebhook, Paiement, Remboursement, Reversement
from .services import reconcilier_paiement, reconcilier_paiements_en_suspens
from .signals import paiement_valide
from .tasks import reconcilier_paiements, reconcilier_reversements

URL_INITIER = "/api/paiements/initier/"
URL_VALIDER_PANIER = "/api/commandes/valider-panier/"
ADRESSE = {"commune": "Cocody", "quartier": "Angré", "point_de_repere": "Pharmacie", "telephone": "0700000001"}
# Tarif de Cocody (district d'Abidjan, migrations livraison 0004 / 0005), payé par commande.
FRAIS_ABIDJAN = Decimal("1500")
EN_TETE = "HTTP_" + EN_TETE_SIGNATURE.upper().replace("-", "_")
MIGRATION_BAREME_14 = importlib.import_module("apps.paiements.migrations.0008_bareme_plateforme_14_pourcent")


def recreer_bareme_plateforme():
    """Barème de la plateforme créé par migration (0008 : 14 % + 100 ou 200
    FCFA par article), effacé par le vidage de base d'un TransactionTestCase.
    Sans effet s'il existe encore."""
    MIGRATION_BAREME_14.creer_bareme_14(registre_apps, None)


def url_webhook(fournisseur="simule", transfert=False):
    return f"/api/paiements/webhook/{fournisseur}/" + ("transfert/" if transfert else "")


class Donnees:
    """Deux boutiques (A : 5 000 FCFA, B : 10 000 FCFA), deux clients, un
    administrateur. Commandes créées par le vrai checkout (frais figés)."""

    def creer_donnees(self):
        cache.clear()
        creer = Utilisateur.objects.create_user
        self.client1 = creer(email="c1@pay.ci", password="x", nom="Konan", prenom="Aya", email_verifie=True)
        self.client2 = creer(email="c2@pay.ci", password="x", nom="Touré", prenom="Ali", email_verifie=True)
        self.admin = creer(email="admin@pay.ci", password="x", nom="A", prenom="D", role=Role.ADMIN)
        self.vendeur1 = self.vendeur("v1@pay.ci", "0707070707")
        self.vendeur2 = self.vendeur("v2@pay.ci", "0505050505")
        self.boutique1 = Boutique.objects.create(proprietaire=self.vendeur1, nom="Pay Un")
        self.boutique2 = Boutique.objects.create(proprietaire=self.vendeur2, nom="Pay Deux")
        self.variante1 = self.variante(self.boutique1, "Produit A", Decimal("5000"))
        self.variante2 = self.variante(self.boutique2, "Produit B", Decimal("10000"))

    def vendeur(self, email, numero):
        vendeur = Utilisateur.objects.create_user(
            email=email, password="x", nom="V", prenom="End", role=Role.VENDEUR,
            statut_kyc=StatutKYC.VALIDE, email_verifie=True,
        )
        DocumentKYC.objects.create(
            utilisateur=vendeur, type_piece=TypePieceIdentite.PASSEPORT, piece_identite_recto="kyc/r.pdf",
            selfie="kyc/s.png", numero_mobile_money=numero, adresse="Cocody",
        )
        return vendeur

    def variante(self, boutique, nom, prix):
        produit = Produit.objects.create(boutique=boutique, nom=nom, prix_base=prix)
        variante = VarianteProduit.objects.create(produit=produit, nom="Standard", prix=prix)
        Stock.objects.filter(variante=variante).update(quantite_disponible=50)
        return variante

    def commander(self, *lignes, client=None, **corps):
        client = client or self.client1
        panier, _ = Panier.objects.get_or_create(utilisateur=client)
        for variante, quantite in lignes:
            PanierItem.objects.create(panier=panier, variante=variante, quantite=quantite)
        api = APIClient()
        api.force_authenticate(client)
        reponse = api.post(URL_VALIDER_PANIER, {"adresse_livraison": ADRESSE, **corps}, format="json")
        assert reponse.status_code == 201, reponse.data
        return [Commande.objects.get(pk=c["id"]) for c in reponse.data]

    def api(self, utilisateur=None):
        api = APIClient()
        if utilisateur is not None:
            api.force_authenticate(utilisateur)
        return api

    def initier(self, client=None, methode="wave", **cible):
        return self.api(client or self.client1).post(URL_INITIER, {"methode": methode, **cible}, format="json")

    def payer(self, commande=None, groupe=None, client=None, methode="wave"):
        cible = {"commande_id": str(commande.pk)} if commande else {"groupe_commande_id": str(groupe.pk)}
        reponse = self.initier(client, methode, **cible)
        assert reponse.status_code == 201, reponse.data
        return Paiement.objects.get(pk=reponse.data["id"])

    def notifier(self, reference, statut="succes", montant=None, devise="XOF", evenement_id=None,
                 fournisseur="simule", transfert=False, horodatage=None, secret=None, **autres):
        corps = {"evenement_id": evenement_id or f"evt-{uuid.uuid4().hex}", "reference": reference,
                 "transaction_id": "SIM-TX", "statut": statut, "devise": devise, **autres}
        if montant is not None:
            corps["montant"] = montant
        brut = json.dumps(corps).encode()
        signature = signer(brut, secret or settings.PAIEMENT_SIMULE_SECRET, horodatage)
        return self.api().post(url_webhook(fournisseur, transfert), data=brut, content_type="application/json",
                               **{EN_TETE: signature})

    def notifier_succes(self, paiement, **kwargs):
        return self.notifier(paiement.reference, montant=int(paiement.montant), **kwargs)


# =====================================================================
# INITIATION, IDOR, MONTANTS
# =====================================================================

class InitiationTests(Donnees, APITestCase):
    def setUp(self):
        self.creer_donnees()
        self.commande_a, self.commande_b = self.commander((self.variante1, 1), (self.variante2, 1))
        self.groupe = self.commande_a.groupe

    def test_paiement_commande_montant_calcule_par_le_serveur(self):
        r = self.initier(commande_id=str(self.commande_a.pk), montant="1")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Decimal(r.data["montant"]), Decimal("5000") + FRAIS_ABIDJAN)
        paiement = Paiement.objects.get(pk=r.data["id"])
        self.assertEqual((paiement.fournisseur, paiement.statut), ("simule", Paiement.Statut.EN_ATTENTE))
        self.assertEqual(list(paiement.commandes.all()), [self.commande_a])
        self.assertTrue(r.data["url_paiement"].endswith(paiement.reference))

    def test_paiement_du_groupe(self):
        r = self.initier(methode="orange_money", groupe_commande_id=str(self.groupe.pk))
        self.assertEqual(r.status_code, 201)
        # Deux commandes (deux boutiques) : deux colis, deux fois les frais.
        self.assertEqual(Decimal(r.data["montant"]), Decimal("15000") + 2 * FRAIS_ABIDJAN)
        self.assertEqual(set(r.data["commandes"]), {self.commande_a.pk, self.commande_b.pk})

    def test_moyens_acceptes(self):
        for methode in ("wave", "orange_money", "mtn_money", "moov_money", "carte_bancaire"):
            with self.subTest(methode):
                paiement = self.payer(self.commande_a, methode=methode)
                self.api(self.client1).post(f"/api/paiements/{paiement.pk}/annuler/")

    def test_paiement_a_la_livraison_refuse(self):
        r = self.initier(methode="espece_livraison", commande_id=str(self.commande_a.pk))
        self.assertEqual(r.status_code, 400)
        self.assertIn("livraison", str(r.data["errors"]["methode"]))
        self.assertFalse(Paiement.objects.exists())
        self.commande_a.refresh_from_db()
        self.assertEqual(self.commande_a.status, Commande.Status.CREEE)

    def test_idor_initier_la_commande_d_un_autre_client(self):
        self.assertEqual(self.initier(self.client2, commande_id=str(self.commande_a.pk)).status_code, 400)
        self.assertEqual(self.initier(self.client2, groupe_commande_id=str(self.groupe.pk)).status_code, 400)
        self.assertFalse(Paiement.objects.exists())

    def test_idor_consulter_ou_annuler_le_paiement_d_un_autre_client(self):
        paiement = self.payer(self.commande_a)
        autre = self.api(self.client2)
        self.assertEqual(autre.get(f"/api/paiements/{paiement.pk}/").status_code, 404)
        self.assertEqual(autre.post(f"/api/paiements/{paiement.pk}/annuler/").status_code, 404)
        self.assertEqual(autre.get("/api/paiements/").data["count"], 0)

    def test_commande_payee_seule_puis_via_son_groupe_refusee(self):
        self.payer(self.commande_a)
        r = self.initier(groupe_commande_id=str(self.groupe.pk))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Paiement.objects.count(), 1)

    def test_double_initiation_refusee(self):
        self.payer(self.commande_a)
        self.assertEqual(self.initier(commande_id=str(self.commande_a.pk)).status_code, 400)

    def test_annuler_puis_relancer_avec_un_autre_moyen(self):
        paiement = self.payer(self.commande_a)
        r = self.api(self.client1).post(f"/api/paiements/{paiement.pk}/annuler/")
        self.assertEqual((r.status_code, r.data["statut"]), (200, "annule"))
        self.assertEqual(self.api(self.client1).post(f"/api/paiements/{paiement.pk}/annuler/").status_code, 409)
        self.assertEqual(self.payer(self.commande_a, methode="mtn_money").methode, "mtn_money")

    def test_commande_deja_payee_ou_annulee_refusee(self):
        paiement = self.payer(self.commande_a)
        self.notifier_succes(paiement)
        self.assertEqual(self.initier(commande_id=str(self.commande_a.pk)).status_code, 400)
        annuler_commande(self.commande_b, Commande.MotifAnnulation.CLIENT)
        self.assertEqual(self.initier(commande_id=str(self.commande_b.pk)).status_code, 400)

    def test_boutique_indisponible_refus_et_commande_annulee(self):
        Boutique.objects.filter(pk=self.boutique1.pk).update(est_suspendue=True)
        self.assertEqual(self.initier(commande_id=str(self.commande_a.pk)).status_code, 400)
        self.commande_a.refresh_from_db()
        self.assertEqual(self.commande_a.status, Commande.Status.ANNULEE)
        self.assertFalse(Paiement.objects.exists())

    def test_fournisseur_injoignable_502_puis_relance_possible(self):
        with mock.patch.object(FournisseurSimule, "initier", side_effect=ErreurFournisseur("hors ligne")):
            r = self.initier(commande_id=str(self.commande_a.pk))
        self.assertEqual(r.status_code, 502)
        self.assertEqual(Paiement.objects.get().statut, Paiement.Statut.ECHOUE)
        self.assertEqual(self.initier(commande_id=str(self.commande_a.pk)).status_code, 201)

    def test_reponse_client_sans_donnees_internes(self):
        paiement = self.payer(self.commande_a)
        self.notifier_succes(paiement, marchand_interne="brut")
        donnees = self.api(self.client1).get(f"/api/paiements/{paiement.pk}/").data
        for champ in ("metadata", "transaction_id_externe", "fournisseur", "hash_jeton_notification"):
            self.assertNotIn(champ, donnees)
        admin = self.api(self.admin).get(f"/api/paiements/{paiement.pk}/").data
        self.assertEqual(admin["fournisseur"], "simule")

    def test_is_staff_sans_role_sans_pouvoir(self):
        paiement = self.payer(self.commande_a)
        staff = Utilisateur.objects.create_user(email="staff@pay.ci", password="x", nom="S", prenom="T",
                                                is_staff=True)
        api = self.api(staff)
        self.assertEqual(api.get("/api/paiements/").data["count"], 0)
        self.assertEqual(api.get(f"/api/paiements/{paiement.pk}/").status_code, 404)
        for url in ("admin/remboursements/", "admin/reversements/", "admin/baremes/"):
            self.assertEqual(api.get(f"/api/paiements/{url}").status_code, 403)

    def test_liste_admin_sans_n_plus_1(self):
        for _ in range(3):
            paiement = self.payer(self.commande_a)
            self.api(self.client1).post(f"/api/paiements/{paiement.pk}/annuler/")
        # count + page (client joint) + commandes + remboursements, quel que soit le nombre.
        with self.assertNumQueries(4):
            self.assertEqual(self.api(self.admin).get("/api/paiements/").data["count"], 3)


class RechercheParReferenceTests(Donnees, APITestCase):
    """GET /api/paiements/?reference= : la page de retour du fournisseur
    retrouve le paiement par la référence de son URL."""

    def setUp(self):
        self.creer_donnees()
        commande_a, commande_b = self.commander((self.variante1, 1), (self.variante2, 1))
        self.paiement = self.payer(commande_a)
        self.autre = self.payer(commande_b)

    def lister(self, utilisateur, **params):
        reponse = self.api(utilisateur).get("/api/paiements/", params)
        self.assertEqual(reponse.status_code, 200)
        return [ligne["id"] for ligne in reponse.data["results"]]

    def test_proprietaire_et_administration_le_retrouvent(self):
        for utilisateur in (self.client1, self.admin):
            with self.subTest(utilisateur.email):
                self.assertEqual(self.lister(utilisateur, reference=self.paiement.reference), [str(self.paiement.pk)])

    def test_paiement_d_un_autre_compte_jamais_renvoye(self):
        self.assertEqual(self.lister(self.client2, reference=self.paiement.reference), [])

    def test_reference_inconnue_ou_trop_longue(self):
        self.assertEqual(self.lister(self.client1, reference="PAY-INCONNUE"), [])
        # Tronquée à la longueur du champ : une valeur plus longue ne correspond à rien.
        self.assertEqual(self.lister(self.client1, reference=self.paiement.reference + "X" * 300), [])

    def test_sans_reference_liste_complete(self):
        attendus = {str(self.paiement.pk), str(self.autre.pk)}
        self.assertEqual(set(self.lister(self.client1)), attendus)
        self.assertEqual(set(self.lister(self.client1, reference="")), attendus)

    def test_nombre_de_requetes_inchange(self):
        api = self.api(self.client1)
        with self.assertNumQueries(4):
            api.get("/api/paiements/", {"reference": self.paiement.reference})


class PerimetreAchatsTests(Donnees, APITestCase):
    """?perimetre=achats sur la liste et le détail : un administrateur qui
    achète voit ses paiements en représentation client, rien de plus."""

    def setUp(self):
        self.creer_donnees()
        (commande,) = self.commander((self.variante1, 1))
        self.paiement_client = self.payer(commande)
        commande_admin = Commande.objects.create(boutique=self.boutique2, client=self.admin,
                                                 montant_total=Decimal("10000"))
        self.paiement_admin = Paiement.objects.create(client=self.admin, commande=commande_admin,
                                                      montant=Decimal("10000"), fournisseur="simule")
        self.paiement_admin.commandes.add(commande_admin)

    def test_liste(self):
        api = self.api(self.admin)
        self.assertEqual(api.get("/api/paiements/").data["count"], 2)
        r = api.get("/api/paiements/", {"perimetre": "achats"})
        self.assertEqual([ligne["id"] for ligne in r.data["results"]], [str(self.paiement_admin.pk)])
        self.assertNotIn("fournisseur", r.data["results"][0])  # représentation client
        r = api.get("/api/paiements/", {"perimetre": "achats", "reference": self.paiement_client.reference})
        self.assertEqual(r.data["count"], 0)

    def test_detail(self):
        api = self.api(self.admin)
        self.assertEqual(api.get(f"/api/paiements/{self.paiement_client.pk}/").status_code, 200)
        url_achat = f"/api/paiements/{self.paiement_client.pk}/"
        self.assertEqual(api.get(url_achat, {"perimetre": "achats"}).status_code, 404)
        r = api.get(f"/api/paiements/{self.paiement_admin.pk}/", {"perimetre": "achats"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("fournisseur", r.data)

    def test_client_et_valeur_inconnue_inchanges(self):
        api = self.api(self.client1)
        for params in ({}, {"perimetre": "achats"}, {"perimetre": "tout"}):
            with self.subTest(params=params):
                r = api.get("/api/paiements/", params)
                self.assertEqual([ligne["id"] for ligne in r.data["results"]], [str(self.paiement_client.pk)])
        self.assertEqual(self.api(self.admin).get("/api/paiements/", {"perimetre": "tout"}).data["count"], 2)


class SimulationPaiementTests(Donnees, APITestCase):
    """POST /api/paiements/simulation/<reference>/ : le payeur confirme ou
    fait échouer son paiement simulé, sans le secret HMAC."""

    def setUp(self):
        self.creer_donnees()
        (self.commande,) = self.commander((self.variante1, 1))
        self.paiement = self.payer(self.commande)

    def simuler(self, statut="succes", utilisateur=None, reference=None):
        return self.api(utilisateur or self.client1).post(
            f"/api/paiements/simulation/{reference or self.paiement.reference}/", {"statut": statut}, format="json",
        )

    def test_succes_valide_le_paiement_par_la_reconciliation(self):
        recus = []

        def recepteur(sender, **kwargs):
            recus.append(kwargs["paiement"].pk)

        paiement_valide.connect(recepteur)
        self.addCleanup(paiement_valide.disconnect, recepteur)
        r = self.simuler()
        self.assertEqual((r.status_code, r.data["statut"], r.data["reference"]),
                         (200, "valide", self.paiement.reference))
        self.commande.refresh_from_db()
        self.assertEqual(self.commande.status, Commande.Status.CONFIRMEE)
        self.assertEqual(recus, [self.paiement.pk])
        # Chemin de la réconciliation : aucune notification journalisée.
        self.assertFalse(JournalWebhook.objects.exists())

    def test_echec(self):
        r = self.simuler("echec")
        self.assertEqual((r.status_code, r.data["statut"]), (200, "echoue"))

    def test_second_appel_409_sans_changer_le_paiement(self):
        self.assertEqual(self.simuler().status_code, 200)
        r = self.simuler("echec")
        self.assertEqual(r.status_code, 409)
        self.paiement.refresh_from_db()
        self.assertEqual(self.paiement.statut, Paiement.Statut.VALIDE)
        # L'état distant confirmé n'est pas réécrit.
        self.assertEqual(reconcilier_paiement(self.paiement), ("succes", False))

    def test_statut_absent_ou_inconnu(self):
        for statut in ("", "en_attente", "injoignable", "valide"):
            with self.subTest(statut=statut):
                r = self.simuler(statut)
                self.assertEqual(r.status_code, 400)
                self.assertIn("statut", r.data["errors"])
        self.assertEqual(self.api(self.client1).post(
            f"/api/paiements/simulation/{self.paiement.reference}/", {}, format="json").status_code, 400)
        self.paiement.refresh_from_db()
        self.assertEqual(self.paiement.statut, Paiement.Statut.EN_ATTENTE)

    def test_paiement_d_un_autre_compte_ou_inconnu_404(self):
        self.assertEqual(self.simuler(utilisateur=self.client2).status_code, 404)
        self.assertEqual(self.simuler(utilisateur=self.admin).status_code, 404)
        self.assertEqual(self.simuler(reference="PAY-INCONNUE").status_code, 404)
        self.paiement.refresh_from_db()
        self.assertEqual(self.paiement.statut, Paiement.Statut.EN_ATTENTE)

    def test_anonyme_401(self):
        r = self.api().post(f"/api/paiements/simulation/{self.paiement.reference}/", {"statut": "succes"},
                            format="json")
        self.assertEqual(r.status_code, 401)

    def test_hors_fournisseur_simule_404(self):
        with override_settings(PAIEMENT_FOURNISSEUR="geniuspay"):
            self.assertEqual(self.simuler().status_code, 404)
        Paiement.objects.filter(pk=self.paiement.pk).update(fournisseur="geniuspay")
        self.assertEqual(self.simuler().status_code, 404)
        self.paiement.refresh_from_db()
        self.assertEqual(self.paiement.statut, Paiement.Statut.EN_ATTENTE)

    def test_route_absente_si_le_reglage_est_faux(self):
        import config.urls

        from . import urls as urls_paiements

        def recharger():
            importlib.reload(urls_paiements)
            importlib.reload(config.urls)
            clear_url_caches()

        self.addCleanup(recharger)
        with override_settings(PAIEMENT_SIMULATION_API_ACTIVE=False):
            recharger()
            self.assertEqual(self.simuler().status_code, 404)
            with self.assertRaises(Resolver404):
                resolve(f"/api/paiements/simulation/{self.paiement.reference}/")
        recharger()
        self.assertEqual(resolve(f"/api/paiements/simulation/{self.paiement.reference}/").url_name,
                         "paiement-simulation")


# =====================================================================
# NOTIFICATIONS (WEBHOOKS)
# =====================================================================

class NotificationTests(Donnees, APITestCase):
    def setUp(self):
        self.creer_donnees()
        (self.commande,) = self.commander((self.variante1, 3))
        self.paiement = self.payer(self.commande)

    def test_succes_valide_confirme_livraison_reversement_points(self):
        r = self.notifier_succes(self.paiement)
        self.assertEqual(r.status_code, 200)
        self.paiement.refresh_from_db()
        self.commande.refresh_from_db()
        self.assertEqual(self.paiement.statut, Paiement.Statut.VALIDE)
        self.assertEqual(self.commande.status, Commande.Status.CONFIRMEE)
        self.assertTrue(Livraison.objects.filter(commande=self.commande).exists())
        self.assertEqual(Reversement.objects.get(commande=self.commande).statut, Reversement.Statut.EN_ATTENTE_LIVRAISON)
        # Aucun point au paiement : ils naissent à la livraison (apps.fidelite).
        self.assertFalse(CompteFidelite.objects.filter(utilisateur=self.client1, solde_points__gt=0).exists())
        self.assertTrue(est_payee(self.commande))

    def test_signature_manquante_invalide_ou_expiree(self):
        corps = json.dumps({"evenement_id": "e", "reference": self.paiement.reference, "statut": "succes",
                            "montant": 15000, "devise": "XOF"})
        api = self.api()
        self.assertEqual(api.post(url_webhook(), corps, content_type="application/json").status_code, 401)
        self.assertEqual(api.post(url_webhook(), corps, content_type="application/json",
                                  **{EN_TETE: signer(corps, "mauvais-secret")}).status_code, 401)
        self.assertEqual(self.notifier_succes(self.paiement, horodatage=int(time.time()) - 600).status_code, 401)
        self.paiement.refresh_from_db()
        self.assertEqual(self.paiement.statut, Paiement.Statut.EN_ATTENTE)

    def test_rejeu_du_meme_evenement_traite_une_fois(self):
        self.assertEqual(self.notifier_succes(self.paiement, evenement_id="evt-1").status_code, 200)
        r = self.notifier_succes(self.paiement, evenement_id="evt-1")
        self.assertEqual((r.status_code, r.data["message"]), (200, "Événement déjà traité."))
        self.assertEqual(JournalWebhook.objects.filter(evenement_id="evt-1").count(), 1)
        # Aucun point au paiement : ils naissent à la livraison (apps.fidelite).
        self.assertFalse(CompteFidelite.objects.filter(utilisateur=self.client1, solde_points__gt=0).exists())

    def test_notification_d_un_autre_fournisseur_refusee(self):
        Paiement.objects.filter(pk=self.paiement.pk).update(fournisseur="geniuspay")
        self.assertEqual(self.notifier_succes(self.paiement).status_code, 404)
        self.paiement.refresh_from_db()
        self.assertEqual(self.paiement.statut, Paiement.Statut.EN_ATTENTE)
        self.assertEqual(self.api().post(url_webhook("inconnu"), {}, format="json").status_code, 404)

    def test_devise_ou_montant_differents_refuses(self):
        self.assertEqual(self.notifier_succes(self.paiement, devise="EUR").status_code, 400)
        self.assertEqual(self.notifier(self.paiement.reference, montant=1).status_code, 400)
        self.assertEqual(self.notifier(self.paiement.reference, montant=None).status_code, 401)  # montant absent
        self.paiement.refresh_from_db()
        self.assertEqual(self.paiement.statut, Paiement.Statut.EN_ATTENTE)
        self.assertEqual(JournalWebhook.objects.filter(statut_traitement="erreur").count(), 2)

    def test_une_notification_ne_change_jamais_les_commandes_payees(self):
        (autre,) = self.commander((self.variante1, 1), client=self.client2)
        self.notifier(self.paiement.reference, statut="echec", commandes_couvertes=[str(autre.pk)],
                      metadata={"commandes_couvertes": [str(autre.pk)]})
        self.notifier_succes(self.paiement)
        autre.refresh_from_db()
        self.assertEqual(autre.status, Commande.Status.CREEE)
        self.assertEqual(list(self.paiement.commandes.all()), [self.commande])

    def test_paiement_valide_jamais_revalide_ni_echoue(self):
        self.notifier_succes(self.paiement)
        self.notifier_succes(self.paiement)  # autre événement
        self.notifier(self.paiement.reference, statut="echec")
        self.paiement.refresh_from_db()
        self.assertEqual(self.paiement.statut, Paiement.Statut.VALIDE)
        # Aucun point au paiement : ils naissent à la livraison (apps.fidelite).
        self.assertFalse(CompteFidelite.objects.filter(utilisateur=self.client1, solde_points__gt=0).exists())
        self.assertEqual(Notification.objects.filter(destinataire=self.client1, titre="Paiement confirmé").count(), 1)

    def test_echec_puis_nouveau_paiement(self):
        self.notifier(self.paiement.reference, statut="echec")
        self.paiement.refresh_from_db()
        self.assertEqual(self.paiement.statut, Paiement.Statut.ECHOUE)
        self.assertEqual(self.initier(commande_id=str(self.commande.pk)).status_code, 201)

    def test_verification_serveur_non_finalisee_n_agit_pas(self):
        with mock.patch.object(FournisseurSimule, "verifier_transaction",
                               return_value=EtatTransaction(statut=EN_ATTENTE)):
            r = self.notifier_succes(self.paiement, evenement_id="evt-attente")
        self.assertEqual(r.status_code, 200)
        self.paiement.refresh_from_db()
        self.assertEqual(self.paiement.statut, Paiement.Statut.EN_ATTENTE)
        self.assertEqual(JournalWebhook.objects.get(evenement_id="evt-attente").statut_traitement, "ignore")
        # La même notification, rejouée une fois la transaction finalisée, est traitée.
        self.assertEqual(self.notifier_succes(self.paiement, evenement_id="evt-attente").status_code, 200)
        self.paiement.refresh_from_db()
        self.assertEqual(self.paiement.statut, Paiement.Statut.VALIDE)

    def test_fournisseur_injoignable_a_la_verification_503(self):
        with mock.patch.object(FournisseurSimule, "verifier_transaction", side_effect=ErreurFournisseur("x")):
            self.assertEqual(self.notifier_succes(self.paiement, evenement_id="evt-503").status_code, 503)
        self.assertEqual(self.notifier_succes(self.paiement, evenement_id="evt-503").status_code, 200)

    def test_notifications_non_soumises_a_la_limite_anonyme(self):
        from apps.core.tests import taux_de_production

        taux = {**taux_de_production(), "anon": "2/hour"}
        with mock.patch.object(SimpleRateThrottle, "THROTTLE_RATES", taux), \
                mock.patch.object(AnonRateThrottle, "THROTTLE_RATES", taux):
            codes = [self.notifier(self.paiement.reference, statut="en_attente").status_code for _ in range(4)]
        self.assertEqual(codes, [200] * 4)
        self.assertEqual(taux_de_production()["webhook_paiement"], "3000/hour")

    def test_initiation_limitee(self):
        from apps.core.tests import taux_de_production

        taux = {**taux_de_production(), "paiements": "2/hour"}
        cache.clear()
        with mock.patch.object(SimpleRateThrottle, "THROTTLE_RATES", taux):
            codes = [self.initier(commande_id=str(self.commande.pk)).status_code for _ in range(3)]
        self.assertEqual(codes, [400, 400, 429])
        self.assertEqual(taux_de_production()["paiements"], "20/hour")


# =====================================================================
# COHÉRENCE AVEC LES COMMANDES, REMBOURSEMENTS
# =====================================================================

class RemboursementTests(Donnees, APITestCase):
    def setUp(self):
        self.creer_donnees()
        self.commande_a, self.commande_b = self.commander((self.variante1, 1), (self.variante2, 1))
        self.groupe = self.commande_a.groupe

    def test_paiement_recu_apres_expiration(self):
        paiement = self.payer(self.commande_a)
        # Toujours « en attente » chez le fournisseur : l'expiration attend la
        # fin du délai de grâce (30 + 30 minutes), puis se fait quand même.
        Commande.objects.filter(pk=self.commande_a.pk).update(created_at=timezone.now() - timedelta(minutes=61))
        expirer_commandes_impayees()
        paiement.refresh_from_db()
        self.assertEqual(paiement.statut, Paiement.Statut.ANNULE)
        self.assertEqual(self.notifier_succes(paiement).status_code, 200)  # succès tardif
        paiement.refresh_from_db()
        self.commande_a.refresh_from_db()
        self.assertEqual(paiement.statut, Paiement.Statut.VALIDE)
        self.assertEqual(self.commande_a.status, Commande.Status.ANNULEE)  # jamais réactivée
        remboursement = Remboursement.objects.get()
        self.assertEqual((remboursement.motif, remboursement.montant), ("commande_annulee", Decimal("5000") + FRAIS_ABIDJAN))
        self.assertFalse(Livraison.objects.filter(commande=self.commande_a).exists())
        self.assertFalse(CompteFidelite.objects.filter(utilisateur=self.client1, solde_points__gt=0).exists())
        self.assertTrue(Notification.objects.filter(destinataire=self.admin, titre="Remboursement à traiter").exists())
        self.assertEqual(
            Notification.objects.get(destinataire=self.admin, titre="Remboursement à traiter").lien_redirection,
            "/administration/remboursements",
        )

    def test_second_paiement_d_une_commande_deja_payee_rembourse(self):
        premier = self.payer(self.commande_a)
        self.api(self.client1).post(f"/api/paiements/{premier.pk}/annuler/")
        second = self.payer(groupe=self.groupe)
        self.notifier_succes(second)
        self.notifier_succes(premier)  # succès tardif du paiement abandonné
        premier.refresh_from_db()
        remboursement = Remboursement.objects.get(paiement=premier)
        self.assertEqual((remboursement.motif, remboursement.commande), ("paiement_en_double", self.commande_a))
        self.assertEqual(Livraison.objects.filter(commande=self.commande_a).count(), 1)

    def test_succes_tardif_de_l_ancien_avant_le_nouveau(self):
        premier = self.payer(self.commande_a)
        self.api(self.client1).post(f"/api/paiements/{premier.pk}/annuler/")
        second = self.payer(self.commande_a, methode="moov_money")
        self.notifier_succes(premier)
        self.notifier_succes(second)
        self.commande_a.refresh_from_db()
        self.assertEqual(self.commande_a.status, Commande.Status.CONFIRMEE)
        self.assertEqual(Remboursement.objects.get().paiement, second)

    def test_annulation_partielle_d_un_groupe_paye(self):
        paiement = self.payer(groupe=self.groupe)
        self.notifier_succes(paiement)
        annuler_commande(self.commande_a, Commande.MotifAnnulation.CLIENT)
        paiement.refresh_from_db()
        self.assertEqual(paiement.statut, Paiement.Statut.VALIDE)
        self.assertTrue(est_payee(self.commande_b))
        self.commande_b.refresh_from_db()
        self.assertEqual(self.commande_b.status, Commande.Status.CONFIRMEE)
        remboursement = Remboursement.objects.get()
        self.assertEqual((remboursement.commande, remboursement.montant), (self.commande_a, Decimal("5000") + FRAIS_ABIDJAN))
        self.assertEqual(Reversement.objects.get(commande=self.commande_a).statut, Reversement.Statut.ANNULE)
        # Signalée deux fois, l'annulation ne crée rien de plus.
        from .services import traiter_paiements_apres_annulation
        traiter_paiements_apres_annulation(self.commande_a)
        self.assertEqual(Remboursement.objects.count(), 1)

    def test_traitement_admin(self):
        paiement = self.payer(self.commande_a)
        self.notifier_succes(paiement)
        annuler_commande(self.commande_a, Commande.MotifAnnulation.ADMINISTRATION)
        remboursement = Remboursement.objects.get()
        url = f"/api/paiements/admin/remboursements/{remboursement.pk}/traiter/"
        self.assertEqual(self.api(self.client1).post(url, {"decision": "effectue"}).status_code, 403)
        admin = self.api(self.admin)
        self.assertEqual(admin.get("/api/paiements/admin/remboursements/?statut=a_traiter").data["count"], 1)
        self.assertEqual(admin.post(url, {"decision": "effectue"}).status_code, 400)  # référence obligatoire
        self.assertEqual(admin.post(url, {"decision": "refuse"}).status_code, 400)  # motif obligatoire
        r = admin.post(url, {"decision": "effectue", "reference_externe": "CP-RMB-1"})
        self.assertEqual((r.status_code, r.data["statut"], r.data["traite_par"]), (200, "effectue", self.admin.pk))
        self.assertEqual(admin.post(url, {"decision": "effectue", "reference_externe": "X"}).status_code, 409)
        self.assertTrue(Notification.objects.filter(destinataire=self.client1, titre="Remboursement effectué").exists())
        self.assertEqual(
            Notification.objects.get(destinataire=self.client1, titre="Remboursement effectué").lien_redirection,
            f"/paiements/{paiement.pk}",
        )
        donnees = self.api(self.client1).get(f"/api/paiements/{paiement.pk}/").data
        self.assertEqual(donnees["remboursements"][0]["statut"], "effectue")


class ConcurrenceTests(Donnees, TransactionTestCase):
    def setUp(self):
        # Un TransactionTestCase précédent vide la base, y compris le barème
        # de frais par défaut créé par migration.
        recreer_bareme_plateforme()
        # Idem pour les tarifs de livraison (défauts et communes du district).
        recreer_tarifs_initiaux()
        self.creer_donnees()
        (self.commande,) = self.commander((self.variante1, 3))

    def en_parallele(self, fonction, nombre=2):
        barriere = threading.Barrier(nombre)
        resultats = []

        def cible(indice):
            try:
                barriere.wait()
                resultats.append(fonction(indice))
            finally:
                connection.close()

        fils = [threading.Thread(target=cible, args=(i,)) for i in range(nombre)]
        for fil in fils:
            fil.start()
        for fil in fils:
            fil.join()
        return sorted(resultats)

    def test_initiations_concurrentes_un_seul_paiement(self):
        codes = self.en_parallele(lambda i: self.initier(commande_id=str(self.commande.pk)).status_code, 4)
        self.assertEqual(codes, [201, 400, 400, 400])
        self.assertEqual(Paiement.objects.count(), 1)

    def test_succes_concurrents_une_seule_validation(self):
        paiement = self.payer(self.commande)
        codes = self.en_parallele(lambda i: self.notifier_succes(paiement, evenement_id=f"evt-c{i}").status_code, 3)
        self.assertEqual(codes, [200, 200, 200])
        # Aucun point au paiement : ils naissent à la livraison (apps.fidelite).
        self.assertFalse(CompteFidelite.objects.filter(utilisateur=self.client1, solde_points__gt=0).exists())
        self.assertEqual(Reversement.objects.count(), 1)
        self.assertEqual(Notification.objects.filter(destinataire=self.client1, titre="Paiement confirmé").count(), 1)


# =====================================================================
# RÉCONCILIATION AVEC LE FOURNISSEUR ET ORDRE DES VERROUS
# =====================================================================

def vieillir_commandes(*commandes, minutes):
    Commande.objects.filter(pk__in=[c.pk for c in commandes]).update(
        created_at=timezone.now() - timedelta(minutes=minutes))


def vieillir_paiement(paiement, minutes):
    Paiement.objects.filter(pk=paiement.pk).update(date_creation=timezone.now() - timedelta(minutes=minutes))


def compter_verifications():
    """Le vrai verifier_transaction du fournisseur simulé, appels comptés."""
    return mock.patch.object(FournisseurSimule, "verifier_transaction", autospec=True,
                             side_effect=FournisseurSimule.verifier_transaction)


def attendre_transactions_bloquees(nombre=1, delai=10):
    """Depuis la connexion du fil appelant : attend que `nombre` autres
    connexions à cette base soient bloquées sur un verrou (pg_locks)."""
    fin = time.monotonic() + delai
    with connection.cursor() as curseur:
        while time.monotonic() < fin:
            curseur.execute("SELECT pg_stat_clear_snapshot()")
            curseur.execute(
                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE NOT l.granted AND a.datname = current_database() AND l.pid <> pg_backend_pid()"
            )
            if curseur.fetchone()[0] >= nombre:
                return True
            time.sleep(0.02)
    return False


class DonneesReconciliation(Donnees):
    def preparer(self):
        self.creer_donnees()
        (self.commande,) = self.commander((self.variante1, 3))
        self.paiement = self.payer(self.commande)

    def etat_distant(self, statut, paiement=None, montant=None, devise="XOF"):
        """État de la transaction « chez le fournisseur » (simulé)."""
        paiement = paiement or self.paiement
        definir_etat_distant(paiement.reference, statut,
                             int(paiement.montant) if montant is None else montant, devise)

    def statuts(self, paiement=None, commande=None):
        paiement, commande = paiement or self.paiement, commande or self.commande
        paiement.refresh_from_db()
        commande.refresh_from_db()
        return paiement.statut, commande.status

    def stock(self):
        return Stock.objects.get(variante=self.variante1).quantite_disponible


class MontantEtDeviseConfirmesTests(DonneesReconciliation, APITestCase):
    """Contrat commun à tous les fournisseurs (fournisseurs/base.py) : un
    succès n'est appliqué que s'il porte un montant entier égal au montant
    dû et la devise du paiement. Information absente, illisible ou
    différente : rien n'est validé, par la notification comme par la
    réconciliation."""

    def setUp(self):
        self.preparer()
        self.du = int(self.paiement.montant)

    def confirmations_refusees(self):
        """libellé → (montant, devise) confirmés par le fournisseur."""
        return {
            "montant absent": (None, "XOF"),
            "devise absente": (self.du, None),
            "devise vide": (self.du, ""),
            "montant et devise absents": (None, None),
            "montant fractionnaire": (self.du + 0.5, "XOF"),
            "montant fractionnaire décimal": (Decimal(f"{self.du}.50"), "XOF"),
            "montant invalide": ("abc", "XOF"),
            "montant en texte": (str(self.du), "XOF"),
            "montant flottant": (float(self.du), "XOF"),
            "montant booléen": (True, "XOF"),
            "devise différente": (self.du, "EUR"),
            "montant inférieur": (self.du - 1, "XOF"),
            "montant supérieur": (self.du + 1, "XOF"),
        }

    def verifier_rien_valide(self):
        self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.assertFalse(Livraison.objects.filter(commande=self.commande).exists())
        self.assertFalse(Reversement.objects.filter(commande=self.commande).exists())

    def test_notification_refusee_sans_montant_et_devise_conformes(self):
        # La lecture d'une notification simulée refuse déjà un succès sans
        # montant entier : l'état vérifié est fourni ici directement, pour
        # exercer le contrôle commun à tous les fournisseurs.
        for numero, (libelle, (montant, devise)) in enumerate(self.confirmations_refusees().items()):
            with self.subTest(libelle):
                etat = EtatTransaction(statut=SUCCES, identifiant_externe="SIM-TX", montant=montant, devise=devise)
                with mock.patch.object(FournisseurSimule, "verifier_transaction", return_value=etat):
                    r = self.notifier_succes(self.paiement, evenement_id=f"evt-refus-{numero}")
                self.assertEqual(r.status_code, 400)
                self.verifier_rien_valide()
                self.assertEqual(JournalWebhook.objects.get(evenement_id=f"evt-refus-{numero}").statut_traitement,
                                 "erreur")

    def test_reconciliation_refusee_sans_montant_et_devise_conformes(self):
        for libelle, (montant, devise) in self.confirmations_refusees().items():
            with self.subTest(libelle):
                definir_etat_distant(self.paiement.reference, "succes", montant, devise)
                self.assertEqual(reconcilier_paiement(self.paiement), ("refuse", False))
                self.verifier_rien_valide()

    def test_notification_avec_montant_et_devise_conformes_valide(self):
        self.assertEqual(self.notifier_succes(self.paiement).status_code, 200)
        self.assertEqual(self.statuts(), ("valide", "confirmee"))

    def test_reconciliation_avec_montant_et_devise_conformes_valide(self):
        self.etat_distant("succes")
        self.assertEqual(reconcilier_paiement(self.paiement), ("succes", True))
        self.assertEqual(self.statuts(), ("valide", "confirmee"))


class ReconciliationAvantExpirationTests(DonneesReconciliation, APITestCase):
    def setUp(self):
        self.preparer()

    def test_succes_chez_le_fournisseur_valide_au_lieu_d_expirer(self):
        self.etat_distant("succes")
        vieillir_commandes(self.commande, minutes=31)
        with self.assertLogs("securite", "WARNING") as journaux:
            self.assertEqual(expirer_commandes_impayees(), 0)
        self.assertEqual(self.statuts(), ("valide", "confirmee"))
        self.assertIn(f"Paiement {self.paiement.reference} : en_attente → valide (source=reconciliation).",
                      journaux.output[-1])
        self.assertTrue(Livraison.objects.filter(commande=self.commande).exists())
        self.assertFalse(Remboursement.objects.exists())
        self.assertEqual(self.stock(), 47)

    def test_echec_chez_le_fournisseur_echoue_puis_expire(self):
        self.etat_distant("echec")
        vieillir_commandes(self.commande, minutes=31)
        self.assertEqual(expirer_commandes_impayees(), 1)
        self.assertEqual(self.statuts(), ("echoue", "annulee"))
        self.assertEqual(self.paiement.metadata["motif_echec"], "échec constaté par réconciliation")
        self.assertEqual(self.stock(), 50)

    def test_en_attente_expiration_repoussee_puis_faite_apres_le_delai_de_grace(self):
        # Aucun état connu du fournisseur simulé : transaction « en attente ».
        for minutes in (31, 59):
            vieillir_commandes(self.commande, minutes=minutes)
            self.assertEqual(expirer_commandes_impayees(), 0)
            self.assertEqual(self.statuts(), ("en_attente", "creee"))
        vieillir_commandes(self.commande, minutes=61)
        self.assertEqual(expirer_commandes_impayees(), 1)
        self.assertEqual(self.statuts(), ("annule", "annulee"))
        self.assertEqual(self.stock(), 50)

    @override_settings(PAIEMENT_RECONCILIATION_DELAI_GRACE_MINUTES=0)
    def test_delai_de_grace_configurable(self):
        vieillir_commandes(self.commande, minutes=31)
        self.assertEqual(expirer_commandes_impayees(), 1)
        self.assertEqual(self.statuts(), ("annule", "annulee"))

    def test_fournisseur_injoignable_un_seul_appel_puis_expiration_apres_le_delai_de_grace(self):
        (autre,) = self.commander((self.variante2, 1))
        autre_paiement = self.payer(autre)
        self.etat_distant("injoignable")
        self.etat_distant("injoignable", autre_paiement)
        vieillir_commandes(self.commande, autre, minutes=31)
        with compter_verifications() as verifier:
            self.assertEqual(expirer_commandes_impayees(), 0)
        # Fournisseur injoignable : il n'est plus rappelé dans la même exécution.
        self.assertEqual(verifier.call_count, 1)
        self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.assertEqual(self.statuts(autre_paiement, autre), ("en_attente", "creee"))
        vieillir_commandes(self.commande, autre, minutes=61)
        self.assertEqual(expirer_commandes_impayees(), 2)
        self.assertEqual(self.statuts(), ("annule", "annulee"))

    def test_commande_sans_paiement_expire_sans_appel_au_fournisseur(self):
        (autre,) = self.commander((self.variante2, 1))
        vieillir_commandes(autre, minutes=31)
        with compter_verifications() as verifier:
            self.assertEqual(expirer_commandes_impayees(), 1)
        self.assertEqual(verifier.call_count, 0)


class ReconciliationPlanifieeTests(DonneesReconciliation, APITestCase):
    def setUp(self):
        self.preparer()

    def test_webhook_perdu_puis_reconciliation_qui_valide(self):
        # Le webhook échoue (vérification impossible, 503) et n'est jamais rejoué...
        with mock.patch.object(FournisseurSimule, "verifier_transaction", side_effect=ErreurFournisseur("x")):
            self.assertEqual(self.notifier_succes(self.paiement).status_code, 503)
        self.etat_distant("succes")  # ... alors que le fournisseur a encaissé.
        self.assertEqual(reconcilier_paiements_en_suspens(), (0, 0, False))  # moins de 15 minutes
        vieillir_paiement(self.paiement, 16)
        with self.assertLogs("securite", "WARNING") as journaux:
            self.assertEqual(reconcilier_paiements(), "1 paiement(s) vérifié(s), 1 statut(s) changé(s).")
        self.assertIn(f"Paiement {self.paiement.reference} : en_attente → valide (source=reconciliation).",
                      journaux.output[-1])
        self.assertEqual(self.statuts(), ("valide", "confirmee"))
        self.assertEqual(Reversement.objects.filter(commande=self.commande).count(), 1)
        self.assertEqual(Notification.objects.filter(destinataire=self.client1, titre="Paiement confirmé").count(), 1)
        # Validé : il n'est plus vérifié.
        self.assertEqual(reconcilier_paiements_en_suspens(), (0, 0, False))

    def test_echec_constate(self):
        self.etat_distant("echec")
        vieillir_paiement(self.paiement, 16)
        self.assertEqual(reconcilier_paiements_en_suspens(), (1, 1, False))
        self.assertEqual(self.statuts(), ("echoue", "creee"))
        # Échoué sans succès chez le fournisseur : vérifié de nouveau, sans effet.
        self.assertEqual(reconcilier_paiements_en_suspens(), (1, 0, False))

    def test_en_attente_n_agit_pas_mais_date_la_verification(self):
        vieillir_paiement(self.paiement, 16)
        self.assertEqual(reconcilier_paiements_en_suspens(), (1, 0, False))
        self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.assertIsNotNone(self.paiement.date_derniere_reconciliation)

    def test_fournisseur_injoignable_arret_propre(self):
        (autre,) = self.commander((self.variante2, 1))
        autre_paiement = self.payer(autre)
        for paiement in (self.paiement, autre_paiement):
            self.etat_distant("injoignable", paiement)
            vieillir_paiement(paiement, 16)
        with compter_verifications() as verifier:
            self.assertEqual(reconcilier_paiements(),
                             "0 paiement(s) vérifié(s), 0 statut(s) changé(s). Interrompue : fournisseur injoignable.")
        self.assertEqual(verifier.call_count, 1)
        self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.paiement.refresh_from_db()
        self.assertIsNone(self.paiement.date_derniere_reconciliation)

    def test_transaction_introuvable_refusee_sans_interrompre(self):
        """Refus pour ce paiement, journalisé et daté ; les suivants sont
        vérifiés (ReconciliationRefusEtPanneTests pour l'expiration)."""
        from io import StringIO

        from django.core.management import call_command

        (autre,) = self.commander((self.variante2, 1))
        autre_paiement = self.payer(autre)
        call_command("simuler_etat_paiement", self.paiement.reference, "introuvable", stdout=StringIO())
        self.etat_distant("succes", autre_paiement)
        vieillir_paiement(self.paiement, 20)
        vieillir_paiement(autre_paiement, 16)
        with self.assertLogs("securite", "ERROR") as journaux, compter_verifications() as verifier:
            self.assertEqual(reconcilier_paiements_en_suspens(), (2, 1, False))
        self.assertEqual(verifier.call_count, 2)
        self.assertIn(f"Réconciliation de {self.paiement.reference} : transaction refusée par le fournisseur simule",
                      journaux.output[0])
        self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.assertIsNotNone(self.paiement.date_derniere_reconciliation)
        self.assertEqual(self.statuts(autre_paiement, autre), ("valide", "confirmee"))

    def test_paiement_annule_puis_succes_constate_un_seul_remboursement(self):
        vieillir_commandes(self.commande, minutes=61)
        expirer_commandes_impayees()
        self.assertEqual(self.statuts(), ("annule", "annulee"))
        self.etat_distant("succes")  # le client a finalement payé
        self.assertEqual(reconcilier_paiements_en_suspens(), (1, 1, False))
        self.assertEqual(reconcilier_paiements_en_suspens(), (0, 0, False))
        self.assertEqual(self.notifier_succes(self.paiement).status_code, 200)  # webhook tardif
        self.assertEqual(self.statuts(), ("valide", "annulee"))
        remboursement = Remboursement.objects.get()
        self.assertEqual((remboursement.motif, remboursement.paiement, remboursement.montant),
                         ("commande_annulee", self.paiement, self.commande.montant_total))
        self.assertEqual(self.stock(), 50)  # restitué une seule fois
        self.assertFalse(Livraison.objects.filter(commande=self.commande).exists())

    def test_annule_sans_transaction_externe_ou_hors_fenetre_non_verifie(self):
        self.api(self.client1).post(f"/api/paiements/{self.paiement.pk}/annuler/")
        self.etat_distant("succes")
        Paiement.objects.filter(pk=self.paiement.pk).update(transaction_id_externe="")
        with compter_verifications() as verifier:
            self.assertEqual(reconcilier_paiements_en_suspens(), (0, 0, False))
            Paiement.objects.filter(pk=self.paiement.pk).update(transaction_id_externe="SIM-X")
            vieillir_paiement(self.paiement, 25 * 60)
            self.assertEqual(reconcilier_paiements_en_suspens(), (0, 0, False))
            vieillir_paiement(self.paiement, 23 * 60)
            self.assertEqual(reconcilier_paiements_en_suspens(), (1, 1, False))
        self.assertEqual(verifier.call_count, 1)

    def test_montant_ou_devise_incoherents_refuses_et_journalises(self):
        vieillir_paiement(self.paiement, 16)
        for montant, devise in ((1, "XOF"), (None, "EUR")):
            with self.subTest(montant=montant, devise=devise):
                self.etat_distant("succes", montant=montant, devise=devise)
                with self.assertLogs("securite", "ERROR") as journaux:
                    self.assertEqual(reconcilier_paiements_en_suspens(), (1, 0, False))
                self.assertIn("rejeté (source=reconciliation)", journaux.output[0])
                self.assertEqual(self.statuts(), ("en_attente", "creee"))

    def test_commande_de_developpement_simuler_etat_paiement(self):
        from io import StringIO

        from django.core.management import CommandError, call_command

        sortie = StringIO()
        call_command("simuler_etat_paiement", self.paiement.reference, "succes", stdout=sortie)
        self.assertIn("état simulé « succes »", sortie.getvalue())
        vieillir_paiement(self.paiement, 16)
        self.assertEqual(reconcilier_paiements_en_suspens(), (1, 1, False))
        self.assertEqual(self.statuts(), ("valide", "confirmee"))
        with self.assertRaises(CommandError):
            call_command("simuler_etat_paiement", "PAY-INCONNU", "succes")
        Paiement.objects.filter(pk=self.paiement.pk).update(fournisseur="geniuspay")
        with self.assertRaises(CommandError):
            call_command("simuler_etat_paiement", self.paiement.reference, "echec")

    @override_settings(PAIEMENT_RECONCILIATION_LOT=1)
    def test_lot_limite_et_rotation(self):
        (autre,) = self.commander((self.variante2, 1))
        autre_paiement = self.payer(autre)
        vieillir_paiement(self.paiement, 20)
        vieillir_paiement(autre_paiement, 16)
        verifies = []
        for _ in range(3):
            with compter_verifications() as verifier:
                self.assertEqual(reconcilier_paiements_en_suspens(), (1, 0, False))
            verifies.append(verifier.call_args.args[1].pk)
        # Jamais vérifié d'abord, puis le moins récemment vérifié.
        self.assertEqual(verifies, [self.paiement.pk, autre_paiement.pk, self.paiement.pk])


class FilsConcurrents:
    """Pour un TransactionTestCase sur PostgreSQL : fonctions exécutées en
    même temps, chacune dans son fil et sa connexion."""

    def lancer(self, *fonctions, entre=None):
        """Exécute chaque fonction dans son fil (sa connexion). `entre` :
        appelée après le démarrage du premier fil, avant celui des autres."""
        resultats, erreurs = {}, []

        def cible(indice, fonction):
            try:
                resultats[indice] = fonction()
            except Exception as erreur:  # noqa: BLE001 — remontée au test
                erreurs.append(erreur)
            finally:
                connection.close()

        fils = [threading.Thread(target=cible, args=(i, f)) for i, f in enumerate(fonctions)]
        fils[0].start()
        if entre is not None:
            entre()
        for fil in fils[1:]:
            fil.start()
        for fil in fils:
            fil.join(timeout=30)
        self.assertFalse(any(fil.is_alive() for fil in fils), "Fil bloqué : interblocage probable.")
        self.assertEqual(erreurs, [])
        return [resultats[i] for i in range(len(fonctions))]


class ReconciliationConcurrenceTests(FilsConcurrents, DonneesReconciliation, TransactionTestCase):
    """PostgreSQL : vraies transactions concurrentes (fils d'exécution)."""

    def setUp(self):
        recreer_bareme_plateforme()
        recreer_tarifs_initiaux()
        self.preparer()

    def verifier_invariant(self):
        """Webhook de succès reçu (argent encaissé) : paiement validé, et
        commande confirmée OU remboursement créé ; stock restitué une fois
        au plus ; jamais « paiement annulé + commande annulée »."""
        paiement_statut, commande_statut = self.statuts()
        remboursements = Remboursement.objects.filter(paiement=self.paiement, commande=self.commande).count()
        self.assertEqual(paiement_statut, "valide")
        if commande_statut == "annulee":
            self.assertEqual((remboursements, self.stock()), (1, 50))
        else:
            self.assertEqual((commande_statut, remboursements, self.stock()), ("confirmee", 0, 47))
        return commande_statut

    def test_webhook_de_succes_contre_expiration_qui_tient_la_commande(self):
        """L'expiration annule la commande (verrou de ligne) puis s'arrête
        juste avant de toucher au paiement ; le webhook arrive à ce moment.
        Avec l'ordre inverse (Paiement puis Commandes), ce scénario
        produirait un interblocage, un webhook en 500 et un paiement annulé
        sans remboursement."""
        from apps.commandes.tasks import expirer_commandes_non_payees

        vieillir_commandes(self.commande, minutes=61)  # délai de grâce écoulé, fournisseur « en attente »
        import apps.paiements.services as services
        vraie = services.traiter_paiements_apres_annulation
        commande_verrouillee = threading.Event()

        def traiter_apres_annulation(commande):
            commande_verrouillee.set()
            self.assertTrue(attendre_transactions_bloquees())  # le webhook attend la commande
            return vraie(commande)

        with mock.patch.object(services, "traiter_paiements_apres_annulation", side_effect=traiter_apres_annulation):
            expiration, webhook = self.lancer(
                expirer_commandes_non_payees,
                lambda: self.notifier_succes(self.paiement).status_code,
                entre=lambda: self.assertTrue(commande_verrouillee.wait(10)),
            )
        self.assertEqual((expiration, webhook), ("1 commande(s) non payée(s) annulée(s).", 200))
        self.assertEqual(self.verifier_invariant(), "annulee")
        self.assertEqual(Remboursement.objects.get().motif, "commande_annulee")

    def test_webhook_de_succes_qui_tient_les_verrous_contre_expiration(self):
        """Le webhook tient la commande et le paiement ; l'expiration (le
        fournisseur répondait encore « en attente ») attend, puis trouve la
        commande confirmée et n'y touche pas."""
        from apps.commandes.tasks import expirer_commandes_non_payees

        vieillir_commandes(self.commande, minutes=61)
        import apps.paiements.services as services
        vrai_controle = services._controler_etat
        verrous_tenus = threading.Event()

        def controler_etat(paiement, etat):
            verrous_tenus.set()
            self.assertTrue(attendre_transactions_bloquees())  # l'expiration attend la commande
            return vrai_controle(paiement, etat)

        with mock.patch.object(services, "_controler_etat", side_effect=controler_etat), \
                mock.patch.object(services, "reconcilier_avant_expiration", return_value=True):
            webhook, expiration = self.lancer(
                lambda: self.notifier_succes(self.paiement).status_code,
                expirer_commandes_non_payees,
                entre=lambda: self.assertTrue(verrous_tenus.wait(10)),
            )
        self.assertEqual((webhook, expiration), (200, "0 commande(s) non payée(s) annulée(s)."))
        self.assertEqual(self.verifier_invariant(), "confirmee")

    def test_annulation_par_le_client_pendant_la_validation_du_paiement(self):
        """L'annulation lit la fiche de livraison (aucune encore), puis son
        UPDATE attend la commande, que le webhook verrouille pour la
        confirmer et créer la fiche. Réévalué après le commit du webhook,
        l'UPDATE annule la commande confirmée (motif client). La fiche créée
        entre-temps doit être annulée avec elle : sinon elle resterait « en
        attente » sur une commande annulée, sans action de l'API pour
        l'annuler."""
        import apps.livraison.services as services_livraison
        import apps.paiements.services as services

        vraie_lecture = services_livraison.annuler_livraison_de
        vrai_controle = services._controler_etat
        lecture_faite, verrous_tenus = threading.Event(), threading.Event()

        def annuler_livraison_de(commande, **options):
            livraison = vraie_lecture(commande, **options)
            if not lecture_faite.is_set():  # la première lecture seulement
                lecture_faite.set()
                self.assertTrue(verrous_tenus.wait(10))  # le webhook tient la commande et le paiement
            return livraison

        def controler_etat(paiement, etat):
            verrous_tenus.set()
            self.assertTrue(attendre_transactions_bloquees())  # l'UPDATE de l'annulation attend la commande
            return vrai_controle(paiement, etat)

        with mock.patch.object(services_livraison, "annuler_livraison_de", side_effect=annuler_livraison_de), \
                mock.patch.object(services, "_controler_etat", side_effect=controler_etat):
            annulation, webhook = self.lancer(
                lambda: self.api(self.client1).post(f"/api/commandes/{self.commande.pk}/annuler/").status_code,
                lambda: self.notifier_succes(self.paiement).status_code,
                entre=lambda: self.assertTrue(lecture_faite.wait(10)),
            )
        self.assertEqual((annulation, webhook), (200, 200))
        # Paiement validé, un seul remboursement, stock restitué une fois (50).
        self.assertEqual(self.verifier_invariant(), "annulee")
        self.assertEqual((self.commande.motif_annulation, Remboursement.objects.get().motif), ("client", "commande_annulee"))
        self.assertEqual(Reversement.objects.get(commande=self.commande).statut, Reversement.Statut.ANNULE)
        livraison = Livraison.objects.get(commande=self.commande)
        self.assertEqual(livraison.status, Livraison.Status.ANNULEE)
        ligne = livraison.historique.get()
        self.assertEqual((ligne.ancien_status, ligne.nouveau_status, ligne.effectue_par),
                         (Livraison.Status.EN_ATTENTE, Livraison.Status.ANNULEE, self.client1))

    def test_reconciliation_et_webhook_simultanes_un_seul_effet(self):
        self.etat_distant("succes")
        barriere = threading.Barrier(2)

        def apres(fonction):
            def executer():
                barriere.wait()
                return fonction()
            return executer

        webhook, (issue, change) = self.lancer(
            apres(lambda: self.notifier_succes(self.paiement).status_code),
            apres(lambda: reconcilier_paiement(Paiement.objects.get(pk=self.paiement.pk))),
        )
        self.assertEqual((webhook, issue), (200, "succes"))
        self.assertEqual(self.verifier_invariant(), "confirmee")
        self.assertEqual(Reversement.objects.count(), 1)
        self.assertEqual(Livraison.objects.count(), 1)
        self.assertEqual(Notification.objects.filter(destinataire=self.client1, titre="Paiement confirmé").count(), 1)

    def test_tache_lancee_deux_fois_en_parallele_idempotente(self):
        # Paiement annulé (commande expirée) mais encaissé, et un second en attente encaissé.
        vieillir_commandes(self.commande, minutes=61)
        expirer_commandes_impayees()
        self.etat_distant("succes")
        (autre,) = self.commander((self.variante2, 1))
        autre_paiement = self.payer(autre)
        self.etat_distant("succes", autre_paiement)
        vieillir_paiement(autre_paiement, 16)
        barriere = threading.Barrier(2)

        def tache():
            barriere.wait()
            return reconcilier_paiements_en_suspens()

        premier, second = self.lancer(tache, tache)
        self.assertEqual(premier[1] + second[1], 2)  # chaque changement appliqué une seule fois
        self.assertEqual(self.verifier_invariant(), "annulee")
        self.assertEqual(self.statuts(autre_paiement, autre), ("valide", "confirmee"))
        self.assertEqual(Remboursement.objects.count(), 1)
        self.assertEqual(Reversement.objects.filter(commande=autre).count(), 1)


# =====================================================================
# FRAIS VENDEUR
# =====================================================================

def frais_figes(article):
    """Frais figés d'un CommandeItem : taux, frais fixe unitaire appliqué,
    commission, frais fixes, net vendeur."""
    return (article.taux_commission, article.frais_fixe_unitaire, article.montant_commission,
            article.montant_frais_fixes, article.montant_net_vendeur)


class FraisTests(Donnees, APITestCase):
    """Barème de la plateforme en base de test : celui de la migration 0008,
    14 % + 100 FCFA par article jusqu'à 3 000 FCFA (200 FCFA au-delà)."""

    def setUp(self):
        self.creer_donnees()

    def article_commande(self, prix, quantite=1, prix_promo=None):
        """Article d'une commande passée par le vrai checkout (boutique 1)."""
        variante = self.variante(self.boutique1, f"Article à {prix}", Decimal(prix))
        if prix_promo is not None:
            VarianteProduit.objects.filter(pk=variante.pk).update(prix_promo=Decimal(prix_promo))
        (commande,) = self.commander((variante, quantite))
        return commande.article.get()

    def test_bareme_par_defaut_14_pourcent_et_100_ou_200_fcfa_tva_incluse(self):
        bareme = bareme_en_vigueur(self.boutique1)
        self.assertEqual(
            (bareme.boutique, bareme.taux_commission, bareme.seuil_petit_article, bareme.frais_fixe_petit_article,
             bareme.frais_fixe_article, bareme.date_fin),
            (None, Decimal("14"), 3000, 100, 200, None),
        )
        self.assertIn("TVA incluse", bareme.libelle)
        # L'ancien barème (migration 0004) est clôturé à l'instant où le
        # nouveau commence, sans aucune autre modification.
        ancien = BaremeFrais.objects.get(boutique=None, taux_commission=Decimal("12"))
        self.assertEqual((ancien.frais_fixe_article, ancien.seuil_petit_article, ancien.frais_fixe_petit_article),
                         (200, None, None))
        self.assertEqual(ancien.date_fin, bareme.date_debut)
        self.assertEqual(bareme_en_vigueur(self.boutique1, bareme.date_debut - timedelta(microseconds=1)), ancien)

    def test_calcul_arrondi_et_plafond(self):
        # Barème sans seuil (tous ceux d'avant la migration 0007) : un seul
        # frais fixe, calcul inchangé.
        bareme = BaremeFrais(taux_commission=Decimal("12"), frais_fixe_article=200)
        frais = calculer_frais_ligne(Decimal("1005"), 2, bareme)
        self.assertEqual(frais["frais_fixe_unitaire"], 200)
        self.assertEqual(frais["montant_commission"], Decimal("241"))  # 241,2
        self.assertEqual(frais["montant_frais_fixes"], Decimal("400"))
        self.assertEqual(frais["montant_net_vendeur"], Decimal("1369"))
        petit = calculer_frais_ligne(Decimal("150"), 1, bareme)
        self.assertEqual((petit["montant_commission"], petit["montant_frais_fixes"], petit["montant_net_vendeur"]),
                         (Decimal("18"), Decimal("132"), Decimal("0")))
        # Barème avec seuil : demi-franc arrondi au franc supérieur, frais
        # réduit plafonné au prix d'un article très bon marché.
        nouveau = BaremeFrais(taux_commission=Decimal("14"), frais_fixe_article=200, seuil_petit_article=3000,
                              frais_fixe_petit_article=100)
        self.assertEqual(calculer_frais_ligne(Decimal("2125"), 1, nouveau)["montant_commission"], Decimal("298"))  # 297,5
        tres_petit = calculer_frais_ligne(Decimal("100"), 1, nouveau)
        self.assertEqual((tres_petit["frais_fixe_unitaire"], tres_petit["montant_commission"],
                          tres_petit["montant_frais_fixes"], tres_petit["montant_net_vendeur"]),
                         (100, Decimal("14"), Decimal("86"), Decimal("0")))

    def test_cas_limites_du_seuil(self):
        # 14 % arrondi au franc ; frais fixe de 100 FCFA jusqu'à 3 000 FCFA inclus.
        cas = {
            "2999": (100, "420", "2479"),  # commission 419,86
            "3000": (100, "420", "2480"),
            "3001": (200, "420", "2381"),  # commission 420,14
        }
        for prix, (frais_fixe, commission, net) in cas.items():
            with self.subTest(prix=prix):
                article = self.article_commande(prix)
                self.assertEqual(article.prix_unitaire, Decimal(prix))
                self.assertEqual(frais_figes(article),
                                 (Decimal("14"), frais_fixe, Decimal(commission), Decimal(frais_fixe), Decimal(net)))

    def test_promo_qui_fait_passer_sous_le_seuil(self):
        # Prix 3 500, promotion 2 800 : commission et frais fixe portent sur le prix effectif.
        article = self.article_commande("3500", prix_promo="2800")
        self.assertEqual(article.prix_unitaire, Decimal("2800"))
        self.assertEqual(frais_figes(article), (Decimal("14"), 100, Decimal("392"), Decimal("100"), Decimal("2308")))

    def test_quantite_superieure_a_un(self):
        # Le seuil porte sur le prix d'un article, pas sur la ligne (3 × 2 000 = 6 000).
        article = self.article_commande("2000", quantite=3)
        self.assertEqual(frais_figes(article), (Decimal("14"), 100, Decimal("840"), Decimal("300"), Decimal("4860")))
        self.notifier_succes(self.payer(article.commande))
        reversement = Reversement.objects.get(commande=article.commande)
        self.assertEqual((reversement.montant_brut, reversement.montant_commission, reversement.montant_frais_fixes,
                          reversement.montant_net),
                         (Decimal("6000"), Decimal("840"), Decimal("300"), Decimal("4860")))

    def test_frais_figes_au_checkout_et_jamais_recalcules(self):
        (commande,) = self.commander((self.variante1, 2))
        article = commande.article.get()
        self.assertEqual(frais_figes(article), (Decimal("14"), 200, Decimal("1400"), Decimal("400"), Decimal("8200")))
        BaremeFrais.objects.update(taux_commission=Decimal("30"), frais_fixe_article=900)
        article.refresh_from_db()
        self.assertEqual(frais_figes(article), (Decimal("14"), 200, Decimal("1400"), Decimal("400"), Decimal("8200")))

    def test_offre_de_lancement_d_une_boutique(self):
        maintenant = timezone.now()
        BaremeFrais.objects.create(boutique=self.boutique1, taux_commission=Decimal("5"), frais_fixe_article=0,
                                   date_debut=maintenant - timedelta(days=1), date_fin=maintenant + timedelta(days=30))
        commande_a, commande_b = self.commander((self.variante1, 1), (self.variante2, 1))
        self.assertEqual(commande_a.article.get().montant_commission, Decimal("250"))
        self.assertEqual(commande_b.article.get().montant_commission, Decimal("1400"))
        self.assertEqual(bareme_en_vigueur(self.boutique1, maintenant + timedelta(days=31)).boutique, None)

    def test_bareme_propre_a_une_boutique_reste_prioritaire(self):
        # Offre de la boutique 1 (8 %, frais fixe unique de 50 FCFA) : elle
        # l'emporte sur le barème de la plateforme, seuil compris.
        BaremeFrais.objects.create(boutique=self.boutique1, taux_commission=Decimal("8"), frais_fixe_article=50)
        petit_a = self.variante(self.boutique1, "Petit article A", Decimal("2000"))
        petit_b = self.variante(self.boutique2, "Petit article B", Decimal("2000"))
        commandes = {commande.boutique_id: commande for commande in self.commander((petit_a, 1), (petit_b, 1))}
        self.assertEqual(frais_figes(commandes[self.boutique1.pk].article.get()),
                         (Decimal("8"), 50, Decimal("160"), Decimal("50"), Decimal("1790")))
        self.assertEqual(frais_figes(commandes[self.boutique2.pk].article.get()),
                         (Decimal("14"), 100, Decimal("280"), Decimal("100"), Decimal("1620")))

    def test_seuil_et_frais_reduit_vont_ensemble_en_base(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            BaremeFrais.objects.create(taux_commission=Decimal("10"), frais_fixe_article=200, seuil_petit_article=3000)

    def test_coupon_supporte_par_anitche(self):
        CouponReduction.objects.create(code="DIX", type_reduction="pourcentage", valeur=Decimal("10"))
        (commande,) = self.commander((self.variante1, 1), coupon_code="DIX")
        # La remise ne porte que sur les articles : les frais de livraison restent dus.
        self.assertEqual(commande.montant_total, Decimal("4500") + FRAIS_ABIDJAN)
        article = commande.article.get()
        self.assertEqual((article.montant_commission, article.montant_net_vendeur), (Decimal("700"), Decimal("4100")))

    def test_sans_bareme_le_checkout_est_refuse_proprement(self):
        BaremeFrais.objects.all().delete()
        panier, _ = Panier.objects.get_or_create(utilisateur=self.client1)
        PanierItem.objects.create(panier=panier, variante=self.variante1, quantite=1)
        r = self.api(self.client1).post(URL_VALIDER_PANIER, {"adresse_livraison": ADRESSE}, format="json")
        self.assertEqual(r.status_code, 503)
        self.assertFalse(Commande.objects.exists())

    def test_frais_jamais_exposes_au_client(self):
        (commande,) = self.commander((self.variante1, 1))
        donnees = self.api(self.client1).get(f"/api/commandes/{commande.pk}/").data
        self.assertNotIn("montant_commission", donnees["articles"][0])

    def test_administration_des_baremes(self):
        url = "/api/paiements/admin/baremes/"
        self.assertEqual(self.api(self.vendeur1).post(url, {}).status_code, 403)
        admin = self.api(self.admin)
        # Barème programmé (pas encore commencé) : il se modifie librement.
        debut = timezone.now() + timedelta(days=1)
        r = admin.post(url, {"boutique": self.boutique1.pk, "libelle": "Lancement", "taux_commission": "8.00",
                             "frais_fixe_article": 100, "date_debut": debut.isoformat()}, format="json")
        self.assertEqual((r.status_code, r.data["cree_par"]), (201, self.admin.pk))
        self.assertEqual(admin.post(url, {"taux_commission": "120", "frais_fixe_article": 1}).status_code, 400)
        self.assertEqual(admin.patch(f"{url}{r.data['id']}/", {"date_fin": "2000-01-01T00:00:00Z"},
                                     format="json").status_code, 400)
        self.assertEqual(admin.patch(f"{url}{r.data['id']}/", {"taux_commission": "9"}, format="json").status_code, 200)
        self.assertEqual(bareme_en_vigueur(self.boutique1, debut).taux_commission, Decimal("9"))
        self.assertEqual((r.data["seuil_petit_article"], r.data["frais_fixe_petit_article"]), (None, None))

    def test_administration_du_seuil_petit_article(self):
        url = "/api/paiements/admin/baremes/"
        admin = self.api(self.admin)
        debut = timezone.now() + timedelta(days=1)
        r = admin.post(url, {"boutique": self.boutique2.pk, "libelle": "Petits articles", "taux_commission": "10",
                             "frais_fixe_article": 150, "seuil_petit_article": 5000, "frais_fixe_petit_article": 50,
                             "date_debut": debut.isoformat()},
                       format="json")
        self.assertEqual((r.status_code, r.data["seuil_petit_article"], r.data["frais_fixe_petit_article"]),
                         (201, 5000, 50))
        self.assertEqual(bareme_en_vigueur(self.boutique2, debut).frais_fixe_pour(Decimal("5000")), 50)
        # L'un sans l'autre : 400 sur le champ manquant, jamais une erreur de contrainte (500).
        seul = admin.post(url, {"taux_commission": "10", "frais_fixe_article": 150, "seuil_petit_article": 5000},
                          format="json")
        self.assertEqual((seul.status_code, list(seul.data["errors"])), (400, ["frais_fixe_petit_article"]))
        detail = f"{url}{r.data['id']}/"
        vide_un = admin.patch(detail, {"frais_fixe_petit_article": None}, format="json")
        self.assertEqual((vide_un.status_code, list(vide_un.data["errors"])), (400, ["frais_fixe_petit_article"]))
        vide_tout = admin.patch(detail, {"seuil_petit_article": None, "frais_fixe_petit_article": None}, format="json")
        self.assertEqual((vide_tout.status_code, vide_tout.data["seuil_petit_article"]), (200, None))
        self.assertEqual(bareme_en_vigueur(self.boutique2, debut).frais_fixe_pour(Decimal("5000")), 150)


class BaremeDejaAppliqueTests(Donnees, APITestCase):
    """Un barème commencé ne se modifie plus (sauf sa clôture, maintenant ou
    plus tard) et ne se supprime pas, quel que soit le chemin de l'API ; un
    barème programmé reste libre."""

    URL = "/api/paiements/admin/baremes/"

    def setUp(self):
        self.creer_donnees()
        maintenant = timezone.now()
        # Offre de la boutique 1 commencée hier, sans fin ; barème de la
        # plateforme : celui de la migration 0008 (commencé lui aussi).
        self.commence = BaremeFrais.objects.create(
            boutique=self.boutique1, libelle="Offre", taux_commission=Decimal("8"), frais_fixe_article=50,
            date_debut=maintenant - timedelta(days=1),
        )
        self.plateforme = bareme_en_vigueur(self.boutique2)
        self.admin_api = self.api(self.admin)

    def detail(self, bareme):
        return f"{self.URL}{bareme.pk}/"

    def patch(self, bareme, donnees):
        return self.admin_api.patch(self.detail(bareme), donnees, format="json")

    def assertRefus(self, reponse, statut=400):
        self.assertEqual(reponse.status_code, statut, reponse.data)
        self.assertEqual((reponse.data["success"], reponse.data["status_code"]), (False, statut))
        self.assertEqual(reponse.data["errors"]["code"], ["bareme_deja_applique"])

    def test_patch_du_taux_d_un_bareme_commence_refuse(self):
        for bareme in (self.commence, self.plateforme):
            with self.subTest(bareme=str(bareme)):
                avant = BaremeFrais.objects.filter(pk=bareme.pk).values().get()
                r = self.patch(bareme, {"taux_commission": "20.00"})
                self.assertRefus(r)
                self.assertEqual(r.data["errors"]["taux_commission"], ["Non modifiable : le barème a déjà commencé."])
                self.assertEqual(BaremeFrais.objects.filter(pk=bareme.pk).values().get(), avant)
        # Autres champs, un par un, et plusieurs à la fois.
        for donnees in ({"libelle": "Autre"}, {"frais_fixe_article": 10}, {"boutique": self.boutique2.pk},
                        {"date_debut": timezone.now().isoformat()},
                        {"seuil_petit_article": 3000, "frais_fixe_petit_article": 10},
                        {"taux_commission": "1", "date_fin": (timezone.now() + timedelta(days=1)).isoformat()}):
            with self.subTest(donnees=donnees):
                self.assertRefus(self.patch(self.commence, donnees))
        self.commence.refresh_from_db()
        self.assertEqual((self.commence.taux_commission, self.commence.date_fin), (Decimal("8"), None))

    def test_valeur_identique_n_est_pas_une_modification(self):
        r = self.patch(self.commence, {"taux_commission": "8.00", "libelle": "Offre"})
        self.assertEqual(r.status_code, 200, r.data)

    def test_put_meme_regle(self):
        corps = {"boutique": self.boutique1.pk, "libelle": "Offre", "taux_commission": "8.00", "frais_fixe_article": 50}
        refuse = self.admin_api.put(self.detail(self.commence), {**corps, "frais_fixe_article": 0}, format="json")
        self.assertRefus(refuse)
        fin = timezone.now() + timedelta(days=2)
        cloture = self.admin_api.put(self.detail(self.commence), {**corps, "date_fin": fin.isoformat()}, format="json")
        self.assertEqual(cloture.status_code, 200, cloture.data)
        self.commence.refresh_from_db()
        self.assertEqual((self.commence.date_fin, self.commence.frais_fixe_article), (fin, 50))

    def test_cloture_maintenant(self):
        avant = timezone.now()
        r = self.patch(self.plateforme, {"date_fin": avant.isoformat()})
        self.assertEqual(r.status_code, 200, r.data)
        self.plateforme.refresh_from_db()
        # Ramenée à l'heure du serveur : jamais avant la réception de la requête.
        self.assertGreaterEqual(self.plateforme.date_fin, avant)
        self.assertLessEqual(self.plateforme.date_fin, timezone.now())
        self.assertEqual(self.plateforme.taux_commission, Decimal("14"))
        # Horloge du client en retard de 30 secondes : même effet.
        avant = timezone.now()
        r = self.patch(self.commence, {"date_fin": (avant - timedelta(seconds=30)).isoformat()})
        self.assertEqual(r.status_code, 200, r.data)
        self.commence.refresh_from_db()
        self.assertGreaterEqual(self.commence.date_fin, avant)

    def test_cloture_dans_le_futur_puis_avancee(self):
        fin = timezone.now() + timedelta(days=10)
        self.assertEqual(self.patch(self.commence, {"date_fin": fin.isoformat()}).status_code, 200)
        plus_tot = timezone.now() + timedelta(days=3)
        self.assertEqual(self.patch(self.commence, {"date_fin": plus_tot.isoformat()}).status_code, 200)
        self.commence.refresh_from_db()
        self.assertEqual(self.commence.date_fin, plus_tot)
        # Clôture programmée : pas de réouverture.
        self.assertRefus(self.patch(self.commence, {"date_fin": None}))

    def test_cloture_dans_le_passe_refusee(self):
        r = self.patch(self.commence, {"date_fin": (timezone.now() - timedelta(hours=1)).isoformat()})
        self.assertRefus(r)
        self.assertIn("date_fin", r.data["errors"])
        self.commence.refresh_from_db()
        self.assertIsNone(self.commence.date_fin)

    def test_bareme_deja_cloture_ne_se_modifie_plus(self):
        # L'ancien barème de la plateforme (12 %), clôturé par la migration 0008.
        ancien = BaremeFrais.objects.get(boutique=None, taux_commission=Decimal("12"))
        fin = ancien.date_fin
        for donnees in ({"date_fin": (timezone.now() + timedelta(days=1)).isoformat()}, {"date_fin": None},
                        {"taux_commission": "14.00"}):
            with self.subTest(donnees=donnees):
                self.assertRefus(self.patch(ancien, donnees))
        ancien.refresh_from_db()
        self.assertEqual((ancien.date_fin, ancien.taux_commission), (fin, Decimal("12")))

    def test_suppression_d_un_bareme_commence_refusee(self):
        for bareme in (self.commence, self.plateforme):
            with self.subTest(bareme=str(bareme)):
                self.assertRefus(self.admin_api.delete(self.detail(bareme)), 409)
                self.assertTrue(BaremeFrais.objects.filter(pk=bareme.pk).exists())

    def test_bareme_programme_modifiable_et_supprimable(self):
        futur = BaremeFrais.objects.create(
            boutique=self.boutique2, taux_commission=Decimal("10"), frais_fixe_article=100,
            date_debut=timezone.now() + timedelta(days=7),
        )
        r = self.patch(futur, {"taux_commission": "11.50", "libelle": "Rentrée", "seuil_petit_article": 3000,
                               "frais_fixe_petit_article": 50})
        self.assertEqual((r.status_code, r.data["taux_commission"], r.data["libelle"]), (200, "11.50", "Rentrée"))
        r = self.admin_api.put(self.detail(futur), {"boutique": self.boutique2.pk, "taux_commission": "12.00",
                                                    "frais_fixe_article": 150}, format="json")
        self.assertEqual((r.status_code, r.data["frais_fixe_article"]), (200, 150))
        self.assertEqual(self.admin_api.delete(self.detail(futur)).status_code, 204)
        self.assertFalse(BaremeFrais.objects.filter(pk=futur.pk).exists())

    def test_non_administrateur_toujours_403(self):
        futur = BaremeFrais.objects.create(taux_commission=Decimal("10"), frais_fixe_article=100,
                                           date_debut=timezone.now() + timedelta(days=7))
        for utilisateur in (self.vendeur1, self.client1):
            api = self.api(utilisateur)
            for bareme in (self.commence, futur):
                with self.subTest(utilisateur=utilisateur.email, bareme=str(bareme)):
                    url = self.detail(bareme)
                    self.assertEqual(api.get(url).status_code, 403)
                    self.assertEqual(api.patch(url, {"date_fin": timezone.now().isoformat()}, format="json").status_code, 403)
                    self.assertEqual(api.put(url, {}, format="json").status_code, 403)
                    self.assertEqual(api.delete(url).status_code, 403)
            self.assertEqual(api.get(self.URL).status_code, 403)
        self.assertEqual(self.api().delete(self.detail(futur)).status_code, 401)
        self.assertTrue(BaremeFrais.objects.filter(pk=futur.pk).exists())
        self.commence.refresh_from_db()
        self.assertIsNone(self.commence.date_fin)

    def test_admin_django_en_lecture_seule(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        from .admin import BaremeFraisAdmin

        requete = RequestFactory().get("/")
        requete.user = Utilisateur.objects.create_superuser(email="su@pay.ci", password="x", nom="S", prenom="U")
        modele_admin = BaremeFraisAdmin(BaremeFrais, AdminSite())
        self.assertFalse(modele_admin.has_add_permission(requete))
        self.assertFalse(modele_admin.has_change_permission(requete, self.commence))
        self.assertFalse(modele_admin.has_delete_permission(requete, self.commence))


class DateDebutBaremeTests(Donnees, APITestCase):
    """Par l'API, date_debut n'est jamais dans le passé (création,
    reprogrammation d'un barème programmé) : un barème ne devient jamais
    rétroactivement en vigueur sur une période déjà vendue."""

    URL = "/api/paiements/admin/baremes/"

    def setUp(self):
        self.creer_donnees()
        self.admin_api = self.api(self.admin)

    def creer(self, **champs):
        corps = {"boutique": self.boutique1.pk, "taux_commission": "9.00", "frais_fixe_article": 100, **champs}
        return self.admin_api.post(self.URL, corps, format="json")

    def assertDateDebutPassee(self, reponse):
        self.assertEqual(reponse.status_code, 400, reponse.data)
        self.assertEqual((reponse.data["success"], reponse.data["status_code"]), (False, 400))
        self.assertEqual(reponse.data["errors"]["code"], ["date_debut_passee"])
        self.assertIn("date_debut", reponse.data["errors"])

    def test_creation_dans_le_passe_refusee(self):
        for il_y_a in (timedelta(minutes=2), timedelta(days=30)):
            with self.subTest(il_y_a=il_y_a):
                self.assertDateDebutPassee(self.creer(date_debut=(timezone.now() - il_y_a).isoformat()))
        self.assertFalse(BaremeFrais.objects.filter(boutique=self.boutique1).exists())

    def test_creation_maintenant_ramenee_a_l_heure_du_serveur(self):
        avant = timezone.now()
        r = self.creer(date_debut=(avant - timedelta(seconds=30)).isoformat())
        self.assertEqual(r.status_code, 201, r.data)
        bareme = BaremeFrais.objects.get(pk=r.data["id"])
        self.assertGreaterEqual(bareme.date_debut, avant)
        self.assertLessEqual(bareme.date_debut, timezone.now())
        self.assertEqual(bareme_en_vigueur(self.boutique1), bareme)

    def test_creation_future(self):
        debut = timezone.now() + timedelta(days=3)
        r = self.creer(date_debut=debut.isoformat())
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(BaremeFrais.objects.get(pk=r.data["id"]).date_debut, debut)

    def test_sans_date_debut_commence_a_l_enregistrement(self):
        # Comportement conservé : date_debut = instant de l'enregistrement.
        avant = timezone.now()
        r = self.creer()
        self.assertEqual(r.status_code, 201, r.data)
        bareme = BaremeFrais.objects.get(pk=r.data["id"])
        self.assertTrue(avant <= bareme.date_debut <= timezone.now())

    def test_sans_date_debut_date_fin_passee_400(self):
        r = self.creer(date_fin=(timezone.now() - timedelta(days=1)).isoformat())
        self.assertEqual((r.status_code, list(r.data["errors"])), (400, ["date_fin"]))
        self.assertFalse(BaremeFrais.objects.filter(boutique=self.boutique1).exists())

    def test_reprogrammation_d_un_bareme_programme(self):
        futur = BaremeFrais.objects.create(boutique=self.boutique1, taux_commission=Decimal("9"),
                                           frais_fixe_article=100, date_debut=timezone.now() + timedelta(days=7))
        url = f"{self.URL}{futur.pk}/"
        debut_prevu = futur.date_debut
        # Vers le passé : refusé, par PATCH comme par PUT, et rien ne change.
        passe = (timezone.now() - timedelta(days=1)).isoformat()
        self.assertDateDebutPassee(self.admin_api.patch(url, {"date_debut": passe}, format="json"))
        corps = {"boutique": self.boutique1.pk, "taux_commission": "9.00", "frais_fixe_article": 100}
        self.assertDateDebutPassee(self.admin_api.put(url, {**corps, "date_debut": passe}, format="json"))
        futur.refresh_from_db()
        self.assertEqual(futur.date_debut, debut_prevu)
        # Vers une autre date future : accepté.
        plus_tot = timezone.now() + timedelta(days=2)
        self.assertEqual(self.admin_api.patch(url, {"date_debut": plus_tot.isoformat()}, format="json").status_code, 200)
        # Vers « maintenant » (horloge du client en retard) : ramené à l'heure
        # du serveur ; le barème commence.
        avant = timezone.now()
        r = self.admin_api.patch(url, {"date_debut": (avant - timedelta(seconds=20)).isoformat()}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        futur.refresh_from_db()
        self.assertGreaterEqual(futur.date_debut, avant)
        self.assertTrue(futur.a_commence())


class MigrationBaremeQuatorzePourcentTests(Donnees, TransactionTestCase):
    """Migration 0008 : l'ancien barème de la plateforme est clôturé, jamais
    modifié ; les ventes passées et les barèmes des boutiques ne bougent pas ;
    retour arrière propre ; idempotente."""

    AVANT = [("paiements", "0007_bareme_frais_petit_article")]

    def setUp(self):
        recreer_tarifs_initiaux()
        self.creer_donnees()

    def tearDown(self):
        self.migrer()

    def migrer(self, cible=None):
        """Sans cible : dernier état du schéma (0008 et les migrations
        suivantes). Les modèles du code (Paiement : colonne ajoutée par 0009)
        ne s'utilisent qu'à cet état ; avant 0008, seuls les barèmes et le
        checkout (frais figés) sont manipulés."""
        executeur = MigrationExecutor(connection)
        executeur.migrate(cible or executeur.loader.graph.leaf_nodes())

    def test_migration_aller_retour(self):
        # Avant 0008 : un seul barème de la plateforme, celui de la migration 0004.
        self.migrer(self.AVANT)
        BaremeFrais.objects.filter(boutique__isnull=True).delete()
        ancien = BaremeFrais.objects.create(libelle="Barème par défaut de la plateforme", taux_commission=Decimal("12.00"),
                                            frais_fixe_article=200, date_debut=timezone.now() - timedelta(days=30))
        offre = BaremeFrais.objects.create(boutique=self.boutique2, libelle="Offre de lancement",
                                           taux_commission=Decimal("5"), frais_fixe_article=0,
                                           date_debut=timezone.now() - timedelta(days=1))
        (commande_avant,) = self.commander((self.variante1, 1))
        article_avant = commande_avant.article.get()
        self.assertEqual(frais_figes(article_avant), (Decimal("12"), 200, Decimal("600"), Decimal("200"), Decimal("4200")))

        def montants(reversement):
            reversement.refresh_from_db()
            return (reversement.montant_brut, reversement.montant_commission, reversement.montant_frais_fixes,
                    reversement.montant_net)

        self.migrer()
        # Commande passée avant le changement, payée après : frais figés au checkout.
        self.notifier_succes(self.payer(commande_avant))
        reversement_avant = Reversement.objects.get(commande=commande_avant)
        self.assertEqual(montants(reversement_avant), (Decimal("5000"), Decimal("600"), Decimal("200"), Decimal("4200")))
        nouveau = BaremeFrais.objects.get(pk=MIGRATION_BAREME_14.ID_BAREME_14)
        self.assertEqual(
            (nouveau.boutique, nouveau.taux_commission, nouveau.seuil_petit_article, nouveau.frais_fixe_petit_article,
             nouveau.frais_fixe_article, nouveau.date_fin),
            (None, Decimal("14"), 3000, 100, 200, None),
        )
        self.assertIn("TVA incluse", nouveau.libelle)
        # Ancien barème clôturé à l'instant où le nouveau commence, rien d'autre.
        cloture = BaremeFrais.objects.get(pk=ancien.pk)
        self.assertEqual(
            (cloture.libelle, cloture.taux_commission, cloture.frais_fixe_article, cloture.seuil_petit_article,
             cloture.frais_fixe_petit_article, cloture.date_debut, cloture.date_fin),
            (ancien.libelle, Decimal("12"), 200, None, None, ancien.date_debut, nouveau.date_debut),
        )
        self.assertEqual(bareme_en_vigueur(self.boutique1, nouveau.date_debut - timedelta(microseconds=1)), cloture)
        self.assertEqual(bareme_en_vigueur(self.boutique1), nouveau)
        # Barème propre à une boutique : intact et toujours prioritaire.
        self.assertEqual(BaremeFrais.objects.get(pk=offre.pk).date_fin, None)
        self.assertEqual(bareme_en_vigueur(self.boutique2), offre)
        # Commande passée avant le changement : 12 % + 200 FCFA, figés.
        article_avant.refresh_from_db()
        self.assertEqual(frais_figes(article_avant), (Decimal("12"), 200, Decimal("600"), Decimal("200"), Decimal("4200")))
        self.assertEqual(montants(reversement_avant), (Decimal("5000"), Decimal("600"), Decimal("200"), Decimal("4200")))
        # Commande suivante : nouveau barème.
        (commande_apres,) = self.commander((self.variante1, 1))
        article_apres = commande_apres.article.get()
        self.assertEqual(frais_figes(article_apres), (Decimal("14"), 200, Decimal("700"), Decimal("200"), Decimal("4100")))

        # Idempotente : rejouée, elle ne crée ni ne clôture rien de plus.
        MIGRATION_BAREME_14.creer_bareme_14(registre_apps, None)
        self.assertEqual(BaremeFrais.objects.filter(boutique__isnull=True).count(), 2)
        self.assertEqual(BaremeFrais.objects.get(pk=ancien.pk).date_fin, nouveau.date_debut)

        # Retour arrière : nouveau barème supprimé, ancien rouvert ; ventes et
        # barème de la boutique intacts.
        self.migrer(self.AVANT)
        self.assertFalse(BaremeFrais.objects.filter(pk=nouveau.pk).exists())
        self.assertIsNone(BaremeFrais.objects.get(pk=ancien.pk).date_fin)
        self.assertEqual(bareme_en_vigueur(self.boutique1), ancien)
        self.assertEqual(bareme_en_vigueur(self.boutique2), offre)
        article_avant.refresh_from_db()
        article_apres.refresh_from_db()
        self.assertEqual(frais_figes(article_avant), (Decimal("12"), 200, Decimal("600"), Decimal("200"), Decimal("4200")))
        self.assertEqual(frais_figes(article_apres), (Decimal("14"), 200, Decimal("700"), Decimal("200"), Decimal("4100")))

        # De nouveau vers l'avant : un seul barème à 14 %, en vigueur.
        self.migrer()
        self.assertEqual(BaremeFrais.objects.filter(boutique__isnull=True, taux_commission=Decimal("14")).count(), 1)
        self.assertEqual(bareme_en_vigueur(self.boutique1).pk, MIGRATION_BAREME_14.ID_BAREME_14)


# =====================================================================
# REVERSEMENTS
# =====================================================================

class DonneesReversement(Donnees):
    """Commande de la boutique 1 payée : reversement en attente de livraison."""

    URL_TRANSFERER = "/api/paiements/admin/reversements/{}/transferer/"

    def setUp(self):
        self.creer_donnees()
        (self.commande,) = self.commander((self.variante1, 2))  # 10 000 : 1 400 + 400 → net 8 200
        self.notifier_succes(self.payer(self.commande))
        self.reversement = Reversement.objects.get(commande=self.commande)

    def livrer(self, commande=None, il_y_a_jours=0):
        commande = commande or self.commande
        Commande.objects.filter(pk=commande.pk).update(status=Commande.Status.EXPEDIEE)
        synchroniser_depuis_livraison(commande, "livree")
        # Date de remise du colis : sert au délai de retour (apps.retours).
        Livraison.objects.filter(commande=commande).update(
            status=Livraison.Status.LIVREE, date_livraison=timezone.now() - timedelta(days=il_y_a_jours))
        if il_y_a_jours:
            Reversement.objects.filter(commande=commande).update(
                date_disponibilite=timezone.now() - timedelta(days=il_y_a_jours - 7))
        reversements.rendre_disponibles()
        self.reversement.refresh_from_db()

    def transferer_avec(self, **resultat):
        """Transfert dont le fournisseur répond tout de suite SUCCES, avec
        les montant et devise donnés (absents s'ils ne sont pas passés)."""
        from .fournisseurs.base import ResultatTransfert

        reponse = ResultatTransfert(statut=SUCCES, identifiant_externe="T1", **resultat)
        with mock.patch.object(FournisseurSimule, "transferer", return_value=reponse):
            return self.api(self.admin).post(self.URL_TRANSFERER.format(self.reversement.pk), {"operateur": "wave"})

    def versements_notifies(self):
        return Notification.objects.filter(destinataire=self.vendeur1, titre="Reversement effectué").count()

    def alertes(self, administrateur=None):
        return Notification.objects.filter(destinataire=administrateur or self.admin, titre="Reversement à vérifier")


class ReversementTests(DonneesReversement, APITestCase):
    def retour(self, statut=DemandeRetour.Statut.DEMANDE, quantite=1):
        demande = DemandeRetour.objects.create(
            commande=self.commande, client=self.client1, boutique=self.boutique1, description="x",
            montant_remboursement=Decimal("5000") * quantite, statut=statut,
        )
        RetourItem.objects.create(demande_retour=demande, commande_item=self.commande.article.get(), quantite=quantite)
        return demande

    def test_montants_et_cycle_de_vie(self):
        self.assertEqual((self.reversement.montant_brut, self.reversement.montant_commission,
                          self.reversement.montant_frais_fixes, self.reversement.montant_net),
                         (Decimal("10000"), Decimal("1400"), Decimal("400"), Decimal("8200")))
        self.livrer()
        self.assertEqual(self.reversement.statut, Reversement.Statut.EN_RETRACTATION)
        attendu = self.reversement.date_livraison + timedelta(days=7)
        self.assertEqual(self.reversement.date_disponibilite, attendu)
        self.assertEqual(reversements.rendre_disponibles(attendu - timedelta(minutes=1)), 0)
        self.assertEqual(reversements.rendre_disponibles(attendu), 1)

    @override_settings(RETOUR_DELAI_JOURS=30)
    def test_retour_ouvert_suspend_puis_refus_definitif_reprend(self):
        from apps.retours.services import appliquer_echeances

        # Délai de retour configuré plus long que la rétractation : un retour
        # peut s'ouvrir sur un reversement déjà disponible.
        self.livrer(il_y_a_jours=8)
        self.assertEqual(self.reversement.statut, Reversement.Statut.DISPONIBLE)
        api = self.api(self.client1)
        r = api.post("/api/retours/", {"commande_id": str(self.commande.pk), "motif": "produit_defectueux",
                                        "type_resolution": "remboursement", "description": "Article cassé à la livraison",
                                        "articles": [{"commande_item_id": str(self.commande.article.get().pk),
                                                      "quantite": 1}]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.reversement.refresh_from_db()
        self.assertEqual(self.reversement.statut, Reversement.Statut.SUSPENDU)
        r = self.api(self.vendeur1).patch(f"/api/retours/{r.data['id']}/traiter/",
                                          {"action": "rejeter", "reponse": "Article intact au déballage."}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        # Refus contestable : le reversement attend la décision définitive.
        self.reversement.refresh_from_db()
        self.assertEqual(self.reversement.statut, Reversement.Statut.SUSPENDU)
        self.assertEqual(appliquer_echeances(timezone.now() + timedelta(hours=73))[1], 1)
        self.reversement.refresh_from_db()
        self.assertEqual(self.reversement.statut, Reversement.Statut.DISPONIBLE)

    def test_retour_rembourse_avant_versement(self):
        self.livrer()
        demande = self.retour(statut=DemandeRetour.Statut.RECEPTIONNE)
        reversements.suspendre_reversement(self.commande)
        r = self.api(self.vendeur1).patch(f"/api/retours/{demande.pk}/traiter/", {"action": "rembourser"}, format="json")
        self.assertEqual(r.status_code, 200)
        remboursement = Remboursement.objects.get(retour=demande)
        self.assertEqual((remboursement.motif, remboursement.statut, remboursement.montant),
                         ("retour", "a_traiter", Decimal("5000")))
        self.reversement.refresh_from_db()
        # 5 000 − 700 de commission rendue ; le frais fixe (200) reste à ANITCHE.
        self.assertEqual(self.reversement.montant_retours, Decimal("4300"))
        self.assertEqual(self.reversement.montant_net, Decimal("3900"))
        self.assertEqual(self.reversement.statut, Reversement.Statut.EN_RETRACTATION)

    def test_retour_apres_versement_ajustement_sur_le_suivant(self):
        self.livrer(il_y_a_jours=8)
        admin = self.api(self.admin)
        r = admin.post(f"/api/paiements/admin/reversements/{self.reversement.pk}/verser/", {"reference_externe": "OM-1"})
        self.assertEqual((r.status_code, r.data["statut"], Decimal(r.data["montant_a_verser"])), (200, "verse", Decimal("8200")))
        demande = self.retour(statut=DemandeRetour.Statut.RECEPTIONNE)
        self.api(self.vendeur1).patch(f"/api/retours/{demande.pk}/traiter/", {"action": "rembourser"}, format="json")
        self.assertEqual(AjustementVendeur.objects.get().montant, Decimal("-4300"))
        self.assertEqual(self.api(self.vendeur1).get("/api/paiements/vendeur/reversements/resume/").data[
            "ajustements_en_attente"], -4300)

        (suivante,) = self.commander((self.variante1, 2))
        self.notifier_succes(self.payer(suivante))
        self.livrer(suivante, il_y_a_jours=8)
        suivant = Reversement.objects.get(commande=suivante)
        r = admin.post(f"/api/paiements/admin/reversements/{suivant.pk}/verser/", {"reference_externe": "OM-2"})
        self.assertEqual((Decimal(r.data["montant_ajustements"]), Decimal(r.data["montant_a_verser"])),
                         (Decimal("-4300"), Decimal("3900")))
        self.assertEqual(AjustementVendeur.objects.get().reversement_impute, suivant)

    def test_versement_manuel_reserve_admin_et_disponible(self):
        url = f"/api/paiements/admin/reversements/{self.reversement.pk}/verser/"
        self.assertEqual(self.api(self.vendeur1).post(url, {"reference_externe": "X"}).status_code, 403)
        self.assertEqual(self.api(self.admin).post(url, {"reference_externe": "X"}).status_code, 409)  # pas livré
        self.livrer(il_y_a_jours=8)
        self.assertEqual(self.api(self.admin).post(url, {}).status_code, 409)  # référence obligatoire
        self.assertEqual(self.api(self.admin).post(url, {"reference_externe": "OM-9"}).status_code, 200)
        self.reversement.refresh_from_db()
        self.assertEqual((self.reversement.numero_destinataire, self.reversement.verse_par),
                         ("0707070707", self.admin))
        self.assertTrue(Notification.objects.filter(destinataire=self.vendeur1, titre="Reversement effectué").exists())
        self.assertEqual(
            Notification.objects.get(destinataire=self.vendeur1, titre="Reversement effectué").lien_redirection,
            "/vendeur/reversements",
        )

    def test_transfert_par_le_fournisseur(self):
        self.livrer(il_y_a_jours=8)
        with mock.patch.object(FournisseurSimule, "transferer", wraps=FournisseurSimule().transferer) as transferer:
            r = self.api(self.admin).post(f"/api/paiements/admin/reversements/{self.reversement.pk}/transferer/")
        self.assertEqual((r.status_code, r.data["statut"], r.data["operateur"]), (200, "verse", "orange_money"))
        self.assertEqual(transferer.call_args.args[1:], ("+2250707070707", "orange_money"))

    def test_transfert_confirme_par_notification(self):
        from .fournisseurs.base import ResultatTransfert

        self.livrer(il_y_a_jours=8)
        with mock.patch.object(FournisseurSimule, "transferer",
                               return_value=ResultatTransfert(statut=EN_ATTENTE, identifiant_externe="T1")):
            self.api(self.admin).post(f"/api/paiements/admin/reversements/{self.reversement.pk}/transferer/",
                                      {"operateur": "wave"})
        self.reversement.refresh_from_db()
        self.assertEqual((self.reversement.statut, self.reversement.operateur), ("en_cours", "wave"))
        r = self.notifier(self.reversement.reference, montant=8200, transfert=True)
        self.assertEqual(r.status_code, 200)
        self.reversement.refresh_from_db()
        self.assertEqual(self.reversement.statut, Reversement.Statut.VERSE)

    def test_transfert_confirme_sans_montant_et_devise_conformes_refuse(self):
        from .fournisseurs.base import ResultatTransfert

        self.livrer(il_y_a_jours=8)
        with mock.patch.object(FournisseurSimule, "transferer",
                               return_value=ResultatTransfert(statut=EN_ATTENTE, identifiant_externe="T1")):
            self.api(self.admin).post(f"/api/paiements/admin/reversements/{self.reversement.pk}/transferer/",
                                      {"operateur": "wave"})
        du = 8200
        confirmations = {
            "montant absent": (None, "XOF"),
            "devise absente": (du, None),
            "devise vide": (du, ""),
            "montant et devise absents": (None, None),
            "montant fractionnaire": (du + 0.5, "XOF"),
            "montant fractionnaire décimal": (Decimal(f"{du}.50"), "XOF"),
            "montant invalide": ("abc", "XOF"),
            "montant en texte": (str(du), "XOF"),
            "montant flottant": (float(du), "XOF"),
            "montant booléen": (True, "XOF"),
            "devise différente": (du, "EUR"),
            "montant inférieur": (du - 1, "XOF"),
            "montant supérieur": (du + 1, "XOF"),
        }
        # La lecture d'une notification simulée refuse déjà un succès sans
        # montant entier : l'état vérifié est fourni ici directement, pour
        # exercer le contrôle commun à tous les fournisseurs.
        for numero, (libelle, (montant, devise)) in enumerate(confirmations.items()):
            with self.subTest(libelle):
                etat = EtatTransaction(statut=SUCCES, identifiant_externe="T1", montant=montant, devise=devise)
                with mock.patch.object(FournisseurSimule, "verifier_transfert", return_value=etat):
                    r = self.notifier(self.reversement.reference, montant=du, transfert=True,
                                      evenement_id=f"evt-transfert-{numero}")
                self.assertEqual(r.status_code, 400)
                self.reversement.refresh_from_db()
                self.assertEqual((self.reversement.statut, self.reversement.date_versement), ("en_cours", None))
                self.assertEqual(
                    JournalWebhook.objects.get(evenement_id=f"evt-transfert-{numero}").statut_traitement, "erreur")
                self.assertFalse(Notification.objects.filter(
                    destinataire=self.vendeur1, titre="Reversement effectué").exists())
                # Même règle que le succès immédiat et la réconciliation :
                # l'argent a pu partir avec un autre montant, l'administration est alertée.
                self.assertTrue(Notification.objects.filter(
                    destinataire=self.admin, titre="Reversement à vérifier").exists())

    # --- Succès immédiat d'un transfert : même contrôle que la notification ---

    def test_succes_immediat_sans_montant_et_devise_conformes_reste_en_cours(self):
        self.livrer(il_y_a_jours=8)
        du = 8200
        confirmations = {
            "montant et devise absents": {},
            "montant absent": {"devise": "XOF"},
            "devise absente": {"montant": du},
            "devise vide": {"montant": du, "devise": ""},
            "montant fractionnaire": {"montant": du + 0.5, "devise": "XOF"},
            "montant fractionnaire décimal": {"montant": Decimal(f"{du}.50"), "devise": "XOF"},
            "montant en texte": {"montant": str(du), "devise": "XOF"},
            "montant flottant": {"montant": float(du), "devise": "XOF"},
            "montant booléen": {"montant": True, "devise": "XOF"},
            "devise différente": {"montant": du, "devise": "EUR"},
            "montant inférieur": {"montant": du - 1, "devise": "XOF"},
            "montant supérieur": {"montant": du + 1, "devise": "XOF"},
        }
        for libelle, resultat in confirmations.items():
            with self.subTest(libelle):
                Reversement.objects.filter(pk=self.reversement.pk).update(statut=Reversement.Statut.DISPONIBLE)
                with self.assertLogs("securite", "ERROR") as journaux:
                    r = self.transferer_avec(**resultat)
                # L'argent a pu partir : ni versé, ni rendu disponible.
                self.assertEqual((r.status_code, r.data["statut"]), (200, "en_cours"))
                self.reversement.refresh_from_db()
                self.assertEqual(
                    (self.reversement.statut, self.reversement.date_versement, self.reversement.montant_a_verser,
                     self.reversement.reference_externe),
                    ("en_cours", None, Decimal(du), "T1"),
                )
                self.assertIn(f"Transfert {self.reversement.reference} : succès immédiat non appliqué",
                              journaux.output[0])
                self.assertFalse(Notification.objects.filter(
                    destinataire=self.vendeur1, titre="Reversement effectué").exists())

    def test_succes_immediat_conforme_applique(self):
        self.livrer(il_y_a_jours=8)
        r = self.transferer_avec(montant=8200, devise="XOF")
        self.assertEqual((r.status_code, r.data["statut"]), (200, "verse"))
        self.reversement.refresh_from_db()
        self.assertIsNotNone(self.reversement.date_versement)
        self.assertEqual(Notification.objects.filter(
            destinataire=self.vendeur1, titre="Reversement effectué").count(), 1)

    def test_succes_immediat_refuse_puis_finalise_par_la_notification(self):
        self.livrer(il_y_a_jours=8)
        with self.assertLogs("securite", "ERROR"):
            self.transferer_avec(montant=8199, devise="XOF")
        r = self.notifier(self.reversement.reference, montant=8200, transfert=True)
        self.assertEqual(r.status_code, 200)
        self.reversement.refresh_from_db()
        self.assertEqual(self.reversement.statut, Reversement.Statut.VERSE)
        self.assertEqual(Notification.objects.filter(
            destinataire=self.vendeur1, titre="Reversement effectué").count(), 1)

    def test_fournisseur_simule_confirme_montant_et_devise_du_transfert(self):
        self.livrer(il_y_a_jours=8)
        Reversement.objects.filter(pk=self.reversement.pk).update(montant_a_verser=Decimal("8200"))
        self.reversement.refresh_from_db()
        resultat = FournisseurSimule().transferer(self.reversement, "+2250707070707", "orange_money")
        self.assertEqual((resultat.statut, resultat.montant, resultat.devise), (SUCCES, 8200, "XOF"))

    def test_vue_vendeur(self):
        self.livrer()
        (autre,) = self.commander((self.variante2, 1))
        self.notifier_succes(self.payer(autre))
        api = self.api(self.vendeur1)
        donnees = api.get("/api/paiements/vendeur/reversements/").data
        self.assertEqual(donnees["count"], 1)  # jamais ceux d'une autre boutique
        ligne = donnees["results"][0]["lignes"][0]
        self.assertEqual((ligne["taux_commission"], ligne["frais_fixe_unitaire"]), ("14.00", 200))
        self.assertEqual((ligne["montant_commission"], ligne["montant_frais_fixes"]), ("1400.00", "400.00"))
        resume = api.get("/api/paiements/vendeur/reversements/resume/").data
        self.assertEqual((resume["en_retractation"], resume["disponible"], resume["verse"]), (8200, 0, 0))
        self.assertEqual(resume["delai_retractation_jours"], 7)
        self.assertEqual(self.api(self.vendeur2).get("/api/paiements/vendeur/reversements/resume/").data[
            "en_attente_livraison"], 8400)
        self.assertEqual(self.api(self.client1).get("/api/paiements/vendeur/reversements/").status_code, 403)
        (encore,) = self.commander((self.variante1, 1))
        self.notifier_succes(self.payer(encore))
        # count + page (commande jointe) + articles : constant, sans N+1.
        with self.assertNumQueries(3):
            self.assertEqual(api.get("/api/paiements/vendeur/reversements/").data["count"], 2)


class RetourPendantLeVersementTests(DonneesReversement, APITestCase):
    """Retour remboursé alors que le reversement de la commande est en cours
    de transfert ou déjà versé : la part du vendeur ne bouge plus, la
    déduction passe par un ajustement imputé une seule fois."""

    def setUp(self):
        super().setUp()
        self.livrer(il_y_a_jours=8)

    def transfert_en_attente(self):
        from .fournisseurs.base import ResultatTransfert

        with mock.patch.object(FournisseurSimule, "transferer",
                               return_value=ResultatTransfert(statut=EN_ATTENTE, identifiant_externe="T1")):
            self.api(self.admin).post(self.URL_TRANSFERER.format(self.reversement.pk), {"operateur": "wave"})
        self.reversement.refresh_from_db()
        self.assertEqual(self.reversement.statut, Reversement.Statut.EN_COURS)

    def issue_du_transfert(self, statut, montant=None):
        from .fournisseurs.base import ResultatTransfert

        etat = ResultatTransfert(statut=statut, identifiant_externe="T1", montant=montant, devise="XOF")
        return reversements.appliquer_etat_transfert(self.reversement.pk, etat, "immediat")

    def retour_rembourse(self):
        demande = DemandeRetour.objects.create(
            commande=self.commande, client=self.client1, boutique=self.boutique1, description="x",
            montant_remboursement=Decimal("5000"), statut=DemandeRetour.Statut.RECEPTIONNE,
        )
        RetourItem.objects.create(demande_retour=demande, commande_item=self.commande.article.get(), quantite=1)
        r = self.api(self.vendeur1).patch(f"/api/retours/{demande.pk}/traiter/", {"action": "rembourser"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        return demande

    def test_transfert_en_cours_puis_echoue(self):
        anterieur = AjustementVendeur.objects.create(boutique=self.boutique1, montant=Decimal("-1000"), motif="x")
        self.transfert_en_attente()
        ajustement = AjustementVendeur.objects.get(retour=self.retour_rembourse())
        self.reversement.refresh_from_db()
        # Montant figé pendant le transfert : seule la déduction en attente.
        self.assertEqual((ajustement.montant, ajustement.reversement_impute, self.reversement.montant_net,
                          self.reversement.montant_retours, self.reversement.montant_a_verser),
                         (Decimal("-4300"), None, Decimal("8200"), Decimal("0"), Decimal("7200")))
        self.assertEqual(self.issue_du_transfert(ECHEC).reversement.statut, Reversement.Statut.DISPONIBLE)
        # Nouveau transfert du même reversement : 8 200 − 1 000 − 4 300.
        r = self.transferer_avec(montant=2900, devise="XOF")
        self.assertEqual((r.data["statut"], Decimal(r.data["montant_a_verser"])), ("verse", Decimal("2900")))
        for dette in (anterieur, ajustement):
            dette.refresh_from_db()
            self.assertEqual(dette.reversement_impute_id, self.reversement.pk)

    def test_transfert_en_cours_puis_reussi_impute_au_suivant_une_fois(self):
        self.transfert_en_attente()
        ajustement = AjustementVendeur.objects.get(retour=self.retour_rembourse())
        self.assertTrue(self.issue_du_transfert(SUCCES, 8200).change)
        ajustement.refresh_from_db()
        self.assertIsNone(ajustement.reversement_impute)
        imputes = []
        for _ in range(2):
            (suivante,) = self.commander((self.variante1, 2))
            self.notifier_succes(self.payer(suivante))
            self.livrer(suivante, il_y_a_jours=8)
            suivant = Reversement.objects.get(commande=suivante)
            r = self.api(self.admin).post(f"/api/paiements/admin/reversements/{suivant.pk}/verser/",
                                          {"reference_externe": f"OM-{suivant.pk}"})
            imputes.append(Decimal(r.data["montant_ajustements"]))
        self.assertEqual(imputes, [Decimal("-4300"), Decimal("0")])

    def test_apres_versement_un_seul_ajustement(self):
        self.api(self.admin).post(f"/api/paiements/admin/reversements/{self.reversement.pk}/verser/",
                                  {"reference_externe": "OM-1"})
        demande = self.retour_rembourse()
        reversements.appliquer_retour_rembourse(demande)
        self.assertEqual(list(AjustementVendeur.objects.values_list("montant", flat=True)), [Decimal("-4300")])

    def test_remboursement_de_retour_ne_se_refuse_pas(self):
        remboursement = Remboursement.objects.get(retour=self.retour_rembourse())
        url = f"/api/paiements/admin/remboursements/{remboursement.pk}/traiter/"
        admin = self.api(self.admin)
        r = admin.post(url, {"decision": "refuse", "commentaire": "Échec chez le fournisseur"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("ne peut pas être refusé", str(r.data))
        remboursement.refresh_from_db()
        self.assertEqual(remboursement.statut, Remboursement.Statut.A_TRAITER)
        r = admin.post(url, {"decision": "effectue", "reference_externe": "WAVE-R1", "commentaire": "Remboursé à la main"})
        self.assertEqual((r.status_code, r.data["statut"]), (200, "effectue"))


class TransfertIncertainTests(DonneesReversement, APITestCase):
    """Après l'envoi d'un transfert, seul un refus certain ou un échec
    confirmé rend le reversement disponible : toute autre erreur laisse
    l'issue incertaine, le reversement reste « en cours » (montant et
    ajustements figés) et ne peut être ni retransféré ni versé à la main."""

    def setUp(self):
        super().setUp()
        self.livrer(il_y_a_jours=8)
        # Dû par la boutique : imputé au transfert (8 200 − 1 000 = 7 200).
        self.ajustement = AjustementVendeur.objects.create(boutique=self.boutique1, montant=Decimal("-1000"),
                                                           motif="Retour remboursé après versement")

    def transferer(self):
        return self.api(self.admin).post(self.URL_TRANSFERER.format(self.reversement.pk), {"operateur": "orange_money"})

    def remettre_disponible(self):
        Reversement.objects.filter(pk=self.reversement.pk).update(
            statut=Reversement.Statut.DISPONIBLE, montant_a_verser=0, montant_ajustements=0)
        AjustementVendeur.objects.update(reversement_impute=None)

    def etat(self):
        self.reversement.refresh_from_db()
        self.ajustement.refresh_from_db()
        return (self.reversement.statut, self.reversement.montant_a_verser, self.reversement.montant_ajustements,
                self.ajustement.reversement_impute_id)

    def en_cours_fige(self):
        return ("en_cours", Decimal("7200"), Decimal("-1000"), self.reversement.pk)

    def disponible_libere(self):
        return ("disponible", Decimal("0"), Decimal("0"), None)

    def test_delai_depasse_apres_envoi_reste_en_cours(self):
        definir_comportement_transfert(self.reversement.reference, DELAI_APRES_ENVOI)
        with self.assertLogs("securite", "ERROR") as journaux:
            r = self.transferer()
        self.assertEqual((r.status_code, r.data["statut"]), (200, "en_cours"))
        self.assertEqual(self.etat(), self.en_cours_fige())
        self.assertIsNotNone(self.reversement.date_debut_transfert)
        self.assertIsNotNone(r.data["date_debut_transfert"])  # vu par l'administration
        self.assertIn(f"Transfert {self.reversement.reference} : issue incertaine", journaux.output[0])
        self.assertEqual(self.versements_notifies(), 0)
        # Le transfert est bien parti chez le fournisseur.
        self.assertEqual(FournisseurSimule().verifier_transfert(self.reversement).statut, SUCCES)

    def test_erreur_fournisseur_non_certifiee_reste_en_cours(self):
        class ErreurInconnue(ErreurFournisseur):
            pass

        erreurs = {
            "générique": ErreurFournisseur("solde"),
            "sous-classe inconnue": ErreurInconnue("nouvelle erreur d'un adaptateur"),
            "délai dépassé": ErreurFournisseur("GeniusPay injoignable : timeout"),
            "503": ErreurFournisseur("GeniusPay : HTTP 503"),
            "réponse illisible": ErreurFournisseur("GeniusPay : réponse illisible"),
        }
        for libelle, erreur in erreurs.items():
            with self.subTest(libelle):
                self.remettre_disponible()
                with mock.patch.object(FournisseurSimule, "transferer", side_effect=erreur), \
                        self.assertLogs("securite", "ERROR"):
                    r = self.transferer()
                self.assertEqual((r.status_code, r.data["statut"]), (200, "en_cours"))
                self.assertEqual(self.etat(), self.en_cours_fige())

    def test_refus_certain_rend_disponible_et_libere_les_ajustements(self):
        for libelle in ("TransfertRefuse", "MontantHorsLimites"):
            with self.subTest(libelle):
                self.remettre_disponible()
                if libelle == "TransfertRefuse":
                    definir_comportement_transfert(self.reversement.reference, REFUS_CERTAIN)
                    r = self.transferer()
                else:
                    with mock.patch.object(FournisseurSimule, "transferer",
                                           side_effect=MontantHorsLimites("Montant hors limites")):
                        r = self.transferer()
                self.assertEqual(r.status_code, 409)
                self.assertIn("Transfert refusé", str(r.data))
                self.assertEqual(self.etat(), self.disponible_libere())

    def test_operation_non_supportee_rend_disponible(self):
        with mock.patch.object(FournisseurSimule, "transferer",
                               side_effect=OperationNonSupportee("Transfert par API non disponible")):
            r = self.transferer()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.etat(), self.disponible_libere())

    def test_relance_refusee_tant_que_en_cours(self):
        definir_comportement_transfert(self.reversement.reference, DELAI_APRES_ENVOI)
        with mock.patch.object(FournisseurSimule, "transferer", autospec=True,
                               side_effect=FournisseurSimule.transferer) as transferer, \
                self.assertLogs("securite", "ERROR"):
            self.assertEqual(self.transferer().status_code, 200)
            self.assertEqual(self.transferer().status_code, 409)
            r = self.api(self.admin).post(f"/api/paiements/admin/reversements/{self.reversement.pk}/verser/",
                                          {"reference_externe": "OM-1"})
            self.assertEqual(r.status_code, 409)
        self.assertEqual(transferer.call_count, 1)
        self.assertEqual(self.etat(), self.en_cours_fige())

    def test_nouveau_transfert_remet_le_suivi_a_zero(self):
        ancien = timezone.now() - timedelta(days=3)
        Reversement.objects.filter(pk=self.reversement.pk).update(
            date_debut_transfert=ancien, date_derniere_reconciliation=ancien, date_derniere_alerte=ancien)
        definir_comportement_transfert(self.reversement.reference, DELAI_APRES_ENVOI)
        avant = timezone.now()
        with self.assertLogs("securite", "ERROR"):
            self.transferer()
        self.reversement.refresh_from_db()
        self.assertGreaterEqual(self.reversement.date_debut_transfert, avant)
        self.assertEqual((self.reversement.date_derniere_reconciliation, self.reversement.date_derniere_alerte),
                         (None, None))

    def test_rendre_disponibles_ne_touche_que_en_retractation(self):
        for statut in ("en_cours", "verse", "suspendu", "annule", "en_attente_livraison"):
            with self.subTest(statut):
                Reversement.objects.filter(pk=self.reversement.pk).update(
                    statut=statut, date_disponibilite=timezone.now() - timedelta(days=1))
                self.assertEqual(reversements.rendre_disponibles(), 0)
                self.reversement.refresh_from_db()
                self.assertEqual(self.reversement.statut, statut)

    def test_resultat_echec_immediat_rend_disponible(self):
        from .fournisseurs.base import ResultatTransfert

        with mock.patch.object(FournisseurSimule, "transferer",
                               return_value=ResultatTransfert(statut=ECHEC, identifiant_externe="T1")), \
                self.assertLogs("securite", "WARNING") as journaux:
            r = self.transferer()
        self.assertEqual((r.status_code, r.data["statut"]), (200, "disponible"))
        self.assertEqual(self.etat(), self.disponible_libere())
        self.assertIn("en_cours → disponible, échec confirmé (source=immediat)", "\n".join(journaux.output))

    def test_succes_tardif_sur_reversement_non_en_cours_alerte_sans_transition(self):
        """Notification de succès (transaction SIM-TX) pour un reversement qui
        n'est plus en cours : aucune transition ; alerte, sauf si le
        reversement a été versé par ce même transfert."""
        cas = {
            "disponible": ("disponible", "simule", ""),
            "versé à la main": ("verse", "", "OM-1"),
            "versé par un autre transfert": ("verse", "simule", "SIMT-AUTRE"),
            "annulé": ("annule", "simule", ""),
            "versé par ce transfert": ("verse", "simule", "SIM-TX"),
        }
        for numero, (libelle, (statut, fournisseur, reference_externe)) in enumerate(cas.items()):
            with self.subTest(libelle):
                Reversement.objects.filter(pk=self.reversement.pk).update(
                    statut=statut, fournisseur=fournisseur, reference_externe=reference_externe,
                    montant_a_verser=Decimal("7200"))
                avant = self.alertes().count()
                r = self.notifier(self.reversement.reference, montant=7200, transfert=True,
                                  evenement_id=f"evt-tardif-{numero}")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(JournalWebhook.objects.get(evenement_id=f"evt-tardif-{numero}").statut_traitement,
                                 "traite")
                self.reversement.refresh_from_db()
                self.assertEqual((self.reversement.statut, self.reversement.reference_externe),
                                 (statut, reference_externe))
                self.assertEqual(self.versements_notifies(), 0)
                attendu = 0 if libelle == "versé par ce transfert" else 1
                self.assertEqual(self.alertes().count() - avant, attendu)
                if attendu:
                    self.assertIn("double paiement possible", self.alertes().latest("date_creation").message)

    def test_alerte_immediate_sur_succes_non_conforme(self):
        with self.assertLogs("securite", "ERROR") as journaux:
            r = self.transferer_avec(montant=7199, devise="XOF")
        self.assertEqual((r.status_code, r.data["statut"]), (200, "en_cours"))
        (alerte,) = self.alertes()
        self.assertEqual((alerte.lien_redirection, alerte.type_notification),
                         ("/administration/reversements", Notification.TypeNotification.PAIEMENT))
        self.assertIn(self.reversement.reference, alerte.message)
        self.assertIn("7199", alerte.message)
        self.assertNotIn("0707070707", alerte.message)
        self.assertIn("ALERTE ADMINISTRATION", "\n".join(journaux.output))
        self.assertEqual(self.versements_notifies(), 0)


class MigrationSuiviTransfertTests(Donnees, TransactionTestCase):
    """Migration 0010 : seuls les reversements « en cours » reçoivent une date
    de début de transfert (leur dernière mise à jour), les autres restent
    NULL ; retour arrière et nouvel aller sans erreur."""

    AVANT = [("paiements", "0009_paiement_date_derniere_reconciliation")]

    def setUp(self):
        recreer_bareme_plateforme()
        recreer_tarifs_initiaux()
        self.creer_donnees()

    def tearDown(self):
        self.migrer()

    def migrer(self, cible=None):
        executeur = MigrationExecutor(connection)
        executeur.migrate(cible or executeur.loader.graph.leaf_nodes())

    def test_rattrapage_des_seuls_reversements_en_cours(self):
        dates = {}
        for jours, statut in enumerate(Reversement.Statut.values, start=1):
            (commande,) = self.commander((self.variante1, 1))
            self.notifier_succes(self.payer(commande))
            dates[statut] = timezone.now() - timedelta(days=jours)
            Reversement.objects.filter(commande=commande).update(statut=statut, date_mise_a_jour=dates[statut])

        self.migrer(self.AVANT)
        self.migrer()
        self.assertEqual(Reversement.objects.count(), len(Reversement.Statut.values))
        for reversement in Reversement.objects.all():
            with self.subTest(reversement.statut):
                attendu = dates["en_cours"] if reversement.statut == "en_cours" else None
                self.assertEqual(
                    (reversement.date_debut_transfert, reversement.date_derniere_reconciliation,
                     reversement.date_derniere_alerte),
                    (attendu, None, None),
                )

        # Retour arrière (colonnes retirées), puis de nouveau vers l'avant.
        self.migrer(self.AVANT)
        self.migrer()
        self.assertEqual(Reversement.objects.get(statut="en_cours").date_debut_transfert, dates["en_cours"])


def compter_verifications_de_transfert():
    """Le vrai verifier_transfert du fournisseur simulé, appels comptés."""
    return mock.patch.object(FournisseurSimule, "verifier_transfert", autospec=True,
                             side_effect=FournisseurSimule.verifier_transfert)


def transferts_verifies(verifier):
    return [appel.args[1].reference for appel in verifier.call_args_list]


class DonneesReconciliationReversement(DonneesReversement):
    """Reversement livré puis transféré : le fournisseur a répondu « en
    attente » (référence T1) il y a 30 minutes, la notification n'est
    jamais arrivée."""

    def setUp(self):
        super().setUp()
        self.livrer(il_y_a_jours=8)
        self.mettre_en_cours(self.reversement)

    def mettre_en_cours(self, reversement, minutes=30, identifiant="T1"):
        from .fournisseurs.base import ResultatTransfert

        with mock.patch.object(FournisseurSimule, "transferer",
                               return_value=ResultatTransfert(statut=EN_ATTENTE, identifiant_externe=identifiant)):
            r = self.api(self.admin).post(self.URL_TRANSFERER.format(reversement.pk), {"operateur": "wave"})
        assert (r.status_code, r.data["statut"]) == (200, "en_cours"), r.data
        Reversement.objects.filter(pk=reversement.pk).update(
            date_debut_transfert=timezone.now() - timedelta(minutes=minutes))
        reversement.refresh_from_db()
        return reversement

    def autre_reversement(self, minutes=30):
        """Autre commande de la boutique 1 (net 4 100), transférée et en cours."""
        (commande,) = self.commander((self.variante1, 1))
        self.notifier_succes(self.payer(commande))
        self.livrer(commande, il_y_a_jours=8)
        return self.mettre_en_cours(Reversement.objects.get(commande=commande), minutes)

    def distant(self, statut, reversement=None, montant=None, devise="XOF", identifiant="T1"):
        """État du transfert « chez le fournisseur » (simulé)."""
        reversement = reversement or self.reversement
        definir_etat_distant(reversement.reference, statut,
                             int(reversement.montant_a_verser) if montant is None else montant, devise, identifiant)


class ReconciliationReversementTests(DonneesReconciliationReversement, APITestCase):
    """Un reversement « en cours » dont la notification se perd est
    redemandé au fournisseur ; l'état est appliqué par la même règle que la
    notification. Seuls un succès conforme et un échec confirmé changent le
    statut."""

    def test_succes_conforme_verse_une_seule_fois(self):
        self.distant(SUCCES)
        with self.assertLogs("securite", "INFO") as journaux:
            self.assertEqual(reversements.reconcilier_reversements_en_cours(), (1, 1, False))
        self.reversement.refresh_from_db()
        self.assertEqual((self.reversement.statut, self.reversement.reference_externe), ("verse", "T1"))
        self.assertIsNotNone(self.reversement.date_versement)
        self.assertIsNotNone(self.reversement.date_derniere_reconciliation)
        self.assertEqual(self.versements_notifies(), 1)
        self.assertEqual(self.alertes().count(), 0)
        self.assertIn(f"Reversement {self.reversement.reference} : en_cours → verse (source=reconciliation)",
                      "\n".join(journaux.output))

    def test_succes_renseigne_la_reference_du_fournisseur_si_elle_manque(self):
        Reversement.objects.filter(pk=self.reversement.pk).update(reference_externe="")
        self.distant(SUCCES, identifiant="T-RECONCILIE")
        self.assertEqual(reversements.reconcilier_reversement(self.reversement), (SUCCES, True))
        self.reversement.refresh_from_db()
        self.assertEqual((self.reversement.statut, self.reversement.reference_externe), ("verse", "T-RECONCILIE"))

    def test_succes_non_conforme_refuse(self):
        du = int(self.reversement.montant_a_verser)
        confirmations = {
            "montant et devise absents": (None, None),
            "montant absent": (None, "XOF"),
            "devise absente": (du, None),
            "devise vide": (du, ""),
            "montant fractionnaire": (du + 0.5, "XOF"),
            "montant fractionnaire décimal": (Decimal(f"{du}.50"), "XOF"),
            "montant en texte": (str(du), "XOF"),
            "montant flottant": (float(du), "XOF"),
            "montant booléen": (True, "XOF"),
            "devise différente": (du, "EUR"),
            "montant inférieur": (du - 1, "XOF"),
            "montant supérieur": (du + 1, "XOF"),
        }
        for libelle, (montant, devise) in confirmations.items():
            with self.subTest(libelle):
                Reversement.objects.filter(pk=self.reversement.pk).update(date_derniere_alerte=None)
                avant = self.alertes().count()
                definir_etat_distant(self.reversement.reference, SUCCES, montant, devise, "T1")
                with self.assertLogs("securite", "ERROR") as journaux:
                    self.assertEqual(reversements.reconcilier_reversement(self.reversement), (reversements.REFUSE, False))
                self.reversement.refresh_from_db()
                self.assertEqual((self.reversement.statut, self.reversement.date_versement), ("en_cours", None))
                self.assertIn(f"Transfert {self.reversement.reference} : succès constaté par réconciliation non "
                              "appliqué", journaux.output[0])
                self.assertEqual(self.alertes().count(), avant + 1)
                self.assertEqual(self.versements_notifies(), 0)

    def test_echec_confirme_rend_disponible(self):
        ajustement = AjustementVendeur.objects.create(boutique=self.boutique1, montant=Decimal("-1000"),
                                                      motif="Retour remboursé après versement")
        autre = self.autre_reversement()
        self.assertEqual((autre.montant_a_verser, AjustementVendeur.objects.get(pk=ajustement.pk).reversement_impute),
                         (Decimal("3100"), autre))
        self.distant(ECHEC, autre)
        with self.assertLogs("securite", "WARNING") as journaux:
            self.assertEqual(reversements.reconcilier_reversement(autre), (ECHEC, True))
        autre.refresh_from_db()
        self.assertEqual((autre.statut, autre.montant_a_verser, autre.montant_ajustements),
                         ("disponible", Decimal("0"), Decimal("0")))
        self.assertIsNone(AjustementVendeur.objects.get(pk=ajustement.pk).reversement_impute)
        self.assertIn("en_cours → disponible, échec confirmé (source=reconciliation)", "\n".join(journaux.output))

    def test_introuvable_ne_rend_jamais_disponible(self):
        suivant = self.autre_reversement(minutes=20)
        self.distant("introuvable")
        self.distant(SUCCES, suivant)
        with self.assertLogs("securite", "ERROR"), compter_verifications_de_transfert() as verifier:
            self.assertEqual(reversements.reconcilier_reversements_en_cours(), (2, 1, False))
        self.assertEqual(transferts_verifies(verifier), [self.reversement.reference, suivant.reference])
        self.reversement.refresh_from_db()
        suivant.refresh_from_db()
        self.assertEqual((self.reversement.statut, self.reversement.montant_a_verser), ("en_cours", Decimal("8200")))
        self.assertIsNotNone(self.reversement.date_derniere_reconciliation)
        self.assertEqual(suivant.statut, "verse")

    def test_en_attente_inchange_et_date(self):
        mise_a_jour = self.reversement.date_mise_a_jour
        self.assertEqual(reversements.reconcilier_reversements_en_cours(), (1, 0, False))
        self.reversement.refresh_from_db()
        self.assertEqual((self.reversement.statut, self.reversement.date_mise_a_jour), ("en_cours", mise_a_jour))
        self.assertIsNotNone(self.reversement.date_derniere_reconciliation)

    def test_fournisseur_injoignable_arret_propre(self):
        suivant = self.autre_reversement(minutes=20)
        self.distant("injoignable")
        self.distant(SUCCES, suivant)
        with compter_verifications_de_transfert() as verifier:
            self.assertEqual(reconcilier_reversements(), "0 reversement(s) vérifié(s), 0 statut(s) changé(s), "
                                                         "0 alerte(s). Interrompue : fournisseur injoignable.")
        self.assertEqual(transferts_verifies(verifier), [self.reversement.reference])
        self.reversement.refresh_from_db()
        suivant.refresh_from_db()
        self.assertEqual((self.reversement.statut, suivant.statut), ("en_cours", "en_cours"))
        self.assertIsNone(self.reversement.date_derniere_reconciliation)

    def test_fournisseur_inconnu_ou_reponse_incoherente_reste_en_cours(self):
        with self.subTest("fournisseur inconnu"):
            Reversement.objects.filter(pk=self.reversement.pk).update(fournisseur="ancien_fournisseur")
            self.reversement.refresh_from_db()
            with self.assertLogs("securite", "ERROR"):
                self.assertEqual(reversements.reconcilier_reversement(self.reversement), (reversements.REFUSE, False))
        with self.subTest("réponse incohérente"):
            Reversement.objects.filter(pk=self.reversement.pk).update(fournisseur="simule",
                                                                       date_derniere_reconciliation=None)
            self.reversement.refresh_from_db()
            with mock.patch.object(FournisseurSimule, "verifier_transfert",
                                   side_effect=NotificationInvalide("Réponse pour un autre reversement")), \
                    self.assertLogs("securite", "ERROR"):
                self.assertEqual(reversements.reconcilier_reversement(self.reversement), (reversements.REFUSE, False))
        self.reversement.refresh_from_db()
        self.assertEqual(self.reversement.statut, "en_cours")
        self.assertIsNotNone(self.reversement.date_derniere_reconciliation)

    def test_seuls_les_en_cours_assez_anciens_sont_verifies(self):
        recent = self.autre_reversement(minutes=5)
        autre = self.autre_reversement(minutes=30)
        for statut in ("disponible", "verse", "suspendu", "annule", "en_retractation", "en_attente_livraison"):
            with self.subTest(statut):
                Reversement.objects.filter(pk=autre.pk).update(statut=statut)
                with compter_verifications_de_transfert() as verifier:
                    reversements.reconcilier_reversements_en_cours()
                self.assertEqual(transferts_verifies(verifier), [self.reversement.reference])
        recent.refresh_from_db()
        self.assertIsNone(recent.date_derniere_reconciliation)

    @override_settings(REVERSEMENT_RECONCILIATION_LOT=2)
    def test_lot_et_ordre_jamais_verifies_d_abord(self):
        Reversement.objects.filter(pk=self.reversement.pk).update(
            date_debut_transfert=timezone.now() - timedelta(minutes=60),
            date_derniere_reconciliation=timezone.now() - timedelta(minutes=1))
        deuxieme = self.autre_reversement(minutes=30)
        troisieme = self.autre_reversement(minutes=20)
        with compter_verifications_de_transfert() as verifier:
            self.assertEqual(reversements.reconcilier_reversements_en_cours(), (2, 0, False))
        self.assertEqual(transferts_verifies(verifier), [deuxieme.reference, troisieme.reference])

    def test_reconciliation_idempotente(self):
        self.distant(SUCCES)
        self.assertEqual(reversements.reconcilier_reversement(self.reversement), (SUCCES, True))
        self.reversement.refresh_from_db()
        date_versement = self.reversement.date_versement
        self.assertEqual(reversements.reconcilier_reversement(self.reversement), (SUCCES, False))
        self.reversement.refresh_from_db()
        self.assertEqual((self.reversement.statut, self.reversement.date_versement), ("verse", date_versement))
        self.assertEqual(self.versements_notifies(), 1)
        self.assertEqual(self.alertes().count(), 0)

    def test_commande_de_developpement_pour_un_transfert(self):
        sortie = StringIO()
        call_command("simuler_etat_paiement", self.reversement.reference, "succes", stdout=sortie)
        self.assertIn("état simulé « succes » (8200 XOF)", sortie.getvalue())
        self.assertEqual(reversements.reconcilier_reversements_en_cours(), (1, 1, False))
        Reversement.objects.filter(pk=self.reversement.pk).update(fournisseur="")
        with self.assertRaises(CommandError):
            call_command("simuler_etat_paiement", self.reversement.reference, "echec")

    # --- Tâche planifiée et commande d'exploitation ---------------------------

    def test_tache_planifiee_enregistree(self):
        from django.utils.module_loading import import_string

        entree = settings.CELERY_BEAT_SCHEDULE["paiements-reconcilier-reversements"]
        self.assertEqual(entree["task"], "apps.paiements.tasks.reconcilier_reversements")
        self.assertEqual(entree["schedule"].minute, set(range(5, 60, 10)))
        self.assertEqual((settings.REVERSEMENT_RECONCILIATION_AGE_MINUTES, settings.REVERSEMENT_RECONCILIATION_LOT,
                          settings.REVERSEMENT_ALERTE_BLOCAGE_HEURES), (10, 50, 2))
        tache = import_string(entree["task"])
        self.distant(SUCCES)
        self.assertEqual(tache(), "1 reversement(s) vérifié(s), 1 statut(s) changé(s), 0 alerte(s).")

    def test_commande_reconcilier_reversements(self):
        recent = self.autre_reversement(minutes=1)
        self.distant(SUCCES, recent)
        # --reference : un seul reversement, sans condition d'âge.
        sortie = StringIO()
        call_command("reconcilier_reversements", "--reference", recent.reference, stdout=sortie)
        self.assertIn(f"{recent.reference} : issue « succes », statut verse (changé).", sortie.getvalue())
        self.reversement.refresh_from_db()
        self.assertIsNone(self.reversement.date_derniere_reconciliation)
        # Sans argument : le travail de la tâche planifiée.
        sortie = StringIO()
        call_command("reconcilier_reversements", stdout=sortie)
        self.assertIn("1 reversement(s) vérifié(s), 0 statut(s) changé(s), 0 alerte(s).", sortie.getvalue())
        for arguments in (("--reference", "REV-INCONNU"), ("--reference", recent.reference), ("--lecture-seule",)):
            with self.subTest(arguments), self.assertRaises(CommandError):
                call_command("reconcilier_reversements", *arguments, stdout=StringIO())

    def test_lecture_seule_n_ecrit_rien(self):
        self.distant(SUCCES)
        avant = Reversement.objects.filter(pk=self.reversement.pk).values().get()
        notifications = Notification.objects.count()
        sortie = StringIO()
        call_command("reconcilier_reversements", "--reference", self.reversement.reference, "--lecture-seule",
                     stdout=sortie)
        self.assertIn(f"{self.reversement.reference} (simule) : succes, montant confirmé 8200 XOF (à verser : 8200 XOF)",
                      sortie.getvalue())
        self.assertIn("Rien n'a été écrit.", sortie.getvalue())
        self.assertEqual(Reversement.objects.filter(pk=self.reversement.pk).values().get(), avant)
        self.assertEqual(Notification.objects.count(), notifications)
        self.distant("introuvable")
        sortie = StringIO()
        call_command("reconcilier_reversements", "--reference", self.reversement.reference, "--lecture-seule",
                     stdout=sortie)
        self.assertIn("TransactionIntrouvable", sortie.getvalue())

    # --- Alertes ------------------------------------------------------------

    def test_alerte_bloque_apres_le_seuil(self):
        creer = Utilisateur.objects.create_user
        admin2 = creer(email="admin2@pay.ci", password="x", nom="A", prenom="E", role=Role.ADMIN)
        inactif = creer(email="admin3@pay.ci", password="x", nom="A", prenom="F", role=Role.ADMIN, is_active=False)
        debut = self.reversement.date_debut_transfert
        self.assertEqual(reversements.alerter_reversements_bloques(debut + timedelta(hours=2, minutes=-1)), 0)
        with self.assertLogs("securite", "ERROR") as journaux:
            self.assertEqual(reversements.alerter_reversements_bloques(debut + timedelta(hours=2, minutes=1)), 1)
        self.assertIn("ALERTE ADMINISTRATION", journaux.output[0])
        self.assertEqual((self.alertes().count(), self.alertes(admin2).count(), self.alertes(inactif).count()),
                         (1, 1, 0))
        alerte = self.alertes().get()
        self.assertEqual(alerte.lien_redirection, "/administration/reversements")
        for attendu in (self.reversement.reference, "en cours depuis 2 h",
                        f"reconcilier_reversements --reference {self.reversement.reference} --lecture-seule"):
            self.assertIn(attendu, alerte.message)
        self.assertNotIn("0707070707", alerte.message)

    def test_alerte_non_repetee_avant_24h_puis_repetee(self):
        debut = self.reversement.date_debut_transfert
        with self.assertLogs("securite", "ERROR"):
            self.assertEqual(reversements.alerter_reversements_bloques(debut + timedelta(hours=3)), 1)
        self.assertEqual(reversements.alerter_reversements_bloques(debut + timedelta(hours=26, minutes=59)), 0)
        with self.assertLogs("securite", "ERROR"):
            self.assertEqual(reversements.alerter_reversements_bloques(debut + timedelta(hours=27)), 1)
        self.assertEqual(self.alertes().count(), 2)

    def test_alerte_sur_succes_non_conforme_non_repetee_par_la_reconciliation(self):
        self.distant(SUCCES, montant=8199)
        for _ in range(2):
            with self.assertLogs("securite", "ERROR"):
                reversements.reconcilier_reversement(self.reversement)
        self.assertEqual(self.alertes().count(), 1)
        # Même compteur que l'alerte de blocage : une alerte par 24 h et par reversement.
        self.assertEqual(reversements.alerter_reversements_bloques(timezone.now() + timedelta(hours=3)), 0)


class ResolutionManuelleTests(DonneesReconciliationReversement, APITestCase):
    """resoudre_reversement : seule sortie d'un reversement en cours que le
    fournisseur ne tranche pas. Administrateur actif nommé, motif
    obligatoire, référence obligatoire pour « verse », journal de sécurité."""

    def resoudre(self, reference, decision, *options, par="admin@pay.ci", motif="Vérifié dans le tableau de bord"):
        sortie = StringIO()
        call_command("resoudre_reversement", reference, decision, "--par", par, "--motif", motif, *options,
                     stdout=sortie)
        return sortie.getvalue()

    def test_resoudre_non_parti(self):
        ajustement = AjustementVendeur.objects.create(boutique=self.boutique1, montant=Decimal("-1000"),
                                                      motif="Retour remboursé après versement")
        autre = self.autre_reversement()
        with self.assertLogs("securite", "WARNING") as journaux:
            sortie = self.resoudre(autre.reference, "non-parti", motif="Absent du tableau de bord du fournisseur")
        self.assertIn(f"{autre.reference} : Disponible", sortie)
        autre.refresh_from_db()
        self.assertEqual((autre.statut, autre.montant_a_verser, autre.montant_ajustements),
                         ("disponible", Decimal("0"), Decimal("0")))
        self.assertIsNone(AjustementVendeur.objects.get(pk=ajustement.pk).reversement_impute)
        journal = "\n".join(journaux.output)
        for attendu in (f"Reversement {autre.reference} : en_cours → disponible (source=admin, admin_id={self.admin.id}",
                        "décision=non-parti", "Absent du tableau de bord du fournisseur"):
            self.assertIn(attendu, journal)
        self.assertEqual(self.versements_notifies(), 0)
        # Le versement manuel redevient possible.
        r = self.api(self.admin).post(f"/api/paiements/admin/reversements/{autre.pk}/verser/",
                                      {"reference_externe": "OM-7"})
        self.assertEqual((r.status_code, r.data["statut"]), (200, "verse"))

    def test_resoudre_verse(self):
        with self.assertLogs("securite", "WARNING"):
            self.resoudre(self.reversement.reference, "verse", "--reference-externe", "T-TABLEAU")
        self.reversement.refresh_from_db()
        self.assertEqual((self.reversement.statut, self.reversement.reference_externe, self.reversement.verse_par,
                          self.reversement.fournisseur), ("verse", "T-TABLEAU", self.admin, "simule"))
        self.assertIsNotNone(self.reversement.date_versement)
        self.assertEqual(self.versements_notifies(), 1)
        # Succès tardif de ce même transfert : silencieux.
        r = self.notifier(self.reversement.reference, montant=8200, transfert=True, transaction_id="T-TABLEAU")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((self.versements_notifies(), self.alertes().count()), (1, 0))

    def test_resolution_refusee(self):
        creer = Utilisateur.objects.create_user
        creer(email="inactif@pay.ci", password="x", nom="A", prenom="I", role=Role.ADMIN, is_active=False)
        refus = {
            "motif vide": ("non-parti", (), {"motif": "   "}),
            "vendeur": ("non-parti", (), {"par": "v1@pay.ci"}),
            "client": ("non-parti", (), {"par": "c1@pay.ci"}),
            "administrateur inactif": ("non-parti", (), {"par": "inactif@pay.ci"}),
            "utilisateur inconnu": ("non-parti", (), {"par": "personne@pay.ci"}),
            "versé sans référence": ("verse", (), {}),
            "référence trop longue": ("verse", ("--reference-externe", "X" * 151), {}),
            "décision inconnue": ("annule", (), {}),
        }
        for libelle, (decision, options, parametres) in refus.items():
            with self.subTest(libelle), self.assertRaises(CommandError):
                self.resoudre(self.reversement.reference, decision, *options, **parametres)
        self.reversement.refresh_from_db()
        self.assertEqual((self.reversement.statut, self.reversement.montant_a_verser), ("en_cours", Decimal("8200")))
        # Hors « en cours » : refusé (un reversement versé ou disponible ne se résout pas).
        Reversement.objects.filter(pk=self.reversement.pk).update(statut=Reversement.Statut.DISPONIBLE)
        with self.assertRaises(CommandError):
            self.resoudre(self.reversement.reference, "verse", "--reference-externe", "T-1")
        with self.assertRaises(CommandError):
            self.resoudre("REV-INCONNU", "non-parti")
        self.assertEqual(self.versements_notifies(), 0)


class ReconciliationReversementConcurrenceTests(FilsConcurrents, DonneesReconciliationReversement,
                                                TransactionTestCase):
    """PostgreSQL : la réconciliation, une autre réconciliation et la
    notification appliquent l'état sous le même verrou ; une seule transition,
    un seul message au vendeur."""

    def setUp(self):
        recreer_bareme_plateforme()
        recreer_tarifs_initiaux()
        super().setUp()

    def bloquer_premiere_finalisation(self):
        """Le premier fil qui verse attend, verrou tenu, que l'autre soit
        bloqué sur ce verrou."""
        vraie = reversements._finaliser
        premiere = threading.Event()

        def finaliser(reversement):
            if not premiere.is_set():
                premiere.set()
                if not attendre_transactions_bloquees():
                    raise AssertionError("Aucune transaction n'attend le verrou du reversement.")
            return vraie(reversement)

        return mock.patch.object(reversements, "_finaliser", side_effect=finaliser)

    def test_deux_reconciliations_simultanees(self):
        self.distant(SUCCES)
        with self.bloquer_premiere_finalisation():
            resultats = self.lancer(lambda: reversements.reconcilier_reversement(self.reversement),
                                    lambda: reversements.reconcilier_reversement(self.reversement))
        self.assertEqual(sorted(resultats), [(SUCCES, False), (SUCCES, True)])
        self.reversement.refresh_from_db()
        self.assertEqual(self.reversement.statut, "verse")
        self.assertEqual((self.versements_notifies(), self.alertes().count()), (1, 0))

    def test_reconciliation_pendant_une_notification(self):
        self.distant(SUCCES)
        with self.bloquer_premiere_finalisation():
            code_http, (issue, _) = self.lancer(
                lambda: self.notifier(self.reversement.reference, montant=8200, transfert=True, transaction_id="T1",
                                      evenement_id="evt-concurrent").status_code,
                lambda: reversements.reconcilier_reversement(self.reversement),
            )
        self.assertEqual((code_http, issue), (200, SUCCES))
        self.reversement.refresh_from_db()
        self.assertEqual(self.reversement.statut, "verse")
        self.assertEqual(JournalWebhook.objects.get(evenement_id="evt-concurrent").statut_traitement, "traite")
        self.assertEqual((self.versements_notifies(), self.alertes().count()), (1, 0))

    def test_echec_reconcilie_puis_succes_notifie(self):
        self.distant(ECHEC)
        self.assertEqual(reversements.reconcilier_reversement(self.reversement), (ECHEC, True))
        with self.assertLogs("securite", "ERROR"):
            r = self.notifier(self.reversement.reference, montant=8200, transfert=True, transaction_id="T1")
        self.assertEqual(r.status_code, 200)
        self.reversement.refresh_from_db()
        self.assertEqual(self.reversement.statut, "disponible")
        self.assertEqual(self.versements_notifies(), 0)
        self.assertIn("double paiement possible", self.alertes().get().message)


class FournisseurSimuleTransfertTests(SimpleTestCase):
    """Le transfert simulé tient un état « côté fournisseur », relu par
    verifier_transfert sans notification (réconciliation), et répond comme
    programmé : délai dépassé après exécution, refus certain, panne avant
    envoi."""

    def setUp(self):
        cache.clear()
        self.fournisseur = FournisseurSimule()
        self.reversement = SimpleNamespace(reference=f"REV-SIM-{uuid.uuid4().hex[:8]}",
                                           montant_a_verser=Decimal("8200"), reference_externe="")

    def transferer(self):
        return self.fournisseur.transferer(self.reversement, "+2250707070707", "orange_money")

    def etat(self):
        return self.fournisseur.verifier_transfert(self.reversement)

    def test_transfert_refuse_est_une_erreur_fournisseur(self):
        self.assertTrue(issubclass(TransfertRefuse, ErreurFournisseur))

    def test_transfert_execute_retrouve_par_la_verification(self):
        resultat = self.transferer()
        etat = self.etat()
        self.assertEqual((etat.statut, etat.montant, etat.devise, etat.identifiant_externe),
                         (SUCCES, 8200, "XOF", resultat.identifiant_externe))

    def test_transfert_inconnu_en_attente(self):
        self.reversement.reference_externe = "T-CONNU"
        etat = self.etat()
        self.assertEqual((etat.statut, etat.identifiant_externe, etat.montant), (EN_ATTENTE, "T-CONNU", None))

    def test_delai_apres_envoi_erreur_incertaine_transfert_execute(self):
        definir_comportement_transfert(self.reversement.reference, DELAI_APRES_ENVOI)
        with self.assertRaises(ErreurFournisseur) as erreur:
            self.transferer()
        self.assertNotIsInstance(erreur.exception, TransfertRefuse)
        etat = self.etat()
        self.assertEqual((etat.statut, etat.montant, etat.devise), (SUCCES, 8200, "XOF"))
        # Programmé pour un seul transfert : le suivant répond normalement.
        self.assertEqual(self.transferer().statut, SUCCES)

    def test_refus_certain_et_panne_avant_envoi_ne_creent_rien(self):
        for comportement, exception in ((REFUS_CERTAIN, TransfertRefuse), (PANNE_AVANT_ENVOI, ErreurFournisseur)):
            with self.subTest(comportement):
                cache.clear()
                definir_comportement_transfert(self.reversement.reference, comportement)
                with self.assertRaises(exception) as erreur:
                    self.transferer()
                if comportement == PANNE_AVANT_ENVOI:
                    self.assertNotIsInstance(erreur.exception, TransfertRefuse)
                self.assertEqual(self.etat().statut, EN_ATTENTE)

    def test_panne_et_transfert_introuvable_a_la_verification(self):
        definir_etat_distant(self.reversement.reference, "injoignable")
        with self.assertRaises(ErreurFournisseur):
            self.etat()
        definir_etat_distant(self.reversement.reference, "introuvable")
        with self.assertRaises(TransactionIntrouvable):
            self.etat()

    def test_notification_devient_l_etat_distant(self):
        from .fournisseurs.base import Notification as NotificationFournisseur

        notification = NotificationFournisseur(reference=self.reversement.reference, identifiant_externe="T9",
                                               cle_idempotence="evt-1", donnees={"statut": ECHEC})
        self.assertEqual(self.fournisseur.verifier_transfert(self.reversement, notification).statut, ECHEC)
        self.assertEqual((self.etat().statut, self.etat().identifiant_externe), (ECHEC, "T9"))

    def test_comportement_inconnu_refuse(self):
        with self.assertRaises(ValueError):
            definir_comportement_transfert(self.reversement.reference, "succes")


# =====================================================================
# RÉCONCILIATION : REFUS D'UN PAIEMENT ET PANNE DU FOURNISSEUR
# =====================================================================

class ReconciliationRefusEtPanneTests(DonneesReconciliation, APITestCase):
    """Une transaction inconnue du fournisseur pour UN paiement P1 est un
    refus pour P1, pas une panne : la tâche et l'expiration vérifient ensuite
    P2, encaissé et placé derrière. Une vraie panne arrête l'exécution."""

    def setUp(self):
        self.creer_donnees()
        (premiere,) = self.commander((self.variante1, 1))
        (seconde,) = self.commander((self.variante2, 1), client=self.client2)
        # P1 passe toujours en premier : plus petite clé de commande
        # (expiration, triée par pk) et paiement le plus ancien (tâche).
        self.c1, self.c2 = sorted((premiere, seconde), key=lambda commande: commande.pk)
        self.p1 = self.payer(self.c1, client=self.c1.client)
        self.p2 = self.payer(self.c2, client=self.c2.client)
        vieillir_paiement(self.p1, 20)
        vieillir_paiement(self.p2, 16)

    @staticmethod
    def verifies(verifier):
        return [appel.args[1].reference for appel in verifier.call_args_list]

    def test_refus_sur_p1_n_empeche_pas_l_expiration_de_valider_p2(self):
        self.etat_distant("introuvable", self.p1)
        self.etat_distant("succes", self.p2)
        vieillir_commandes(self.c1, self.c2, minutes=31)
        with self.assertLogs("securite", "ERROR"), compter_verifications() as verifier:
            self.assertEqual(expirer_commandes_impayees(), 0)
        self.assertEqual(self.verifies(verifier), [self.p1.reference, self.p2.reference])
        self.assertEqual(self.statuts(self.p2, self.c2), ("valide", "confirmee"))
        # Refus pour P1 : état incertain, expiration repoussée jusqu'à la fin
        # du délai de grâce, puis faite.
        self.assertEqual(self.statuts(self.p1, self.c1), ("en_attente", "creee"))
        vieillir_commandes(self.c1, minutes=61)
        with self.assertLogs("securite", "ERROR"):
            self.assertEqual(expirer_commandes_impayees(), 1)
        self.assertEqual(self.statuts(self.p1, self.c1), ("annule", "annulee"))

    def test_panne_arrete_la_tache_et_l_expiration_sans_demander_p2(self):
        self.etat_distant("injoignable", self.p1)
        self.etat_distant("succes", self.p2)
        vieillir_commandes(self.c1, self.c2, minutes=31)
        with compter_verifications() as verifier:
            self.assertEqual(reconcilier_paiements(), "0 paiement(s) vérifié(s), 0 statut(s) changé(s). "
                                                      "Interrompue : fournisseur injoignable.")
            self.assertEqual(expirer_commandes_impayees(), 0)
        # Un seul appel par exécution, sur P1 : P2 n'est jamais demandé.
        self.assertEqual(self.verifies(verifier), [self.p1.reference, self.p1.reference])
        self.assertEqual(self.statuts(self.p2, self.c2), ("en_attente", "creee"))
        self.assertEqual(self.statuts(self.p1, self.c1), ("en_attente", "creee"))
        self.assertIsNone(self.p1.date_derniere_reconciliation)


# =====================================================================
# ADAPTATEUR GENIUSPAY (faux serveur branché sur requests, aucun appel réel)
# =====================================================================

BASE_GENIUSPAY = "https://geniuspay.test/api/v1/merchant"
CLE_GENIUSPAY = "sk_sandbox_cle-factice"
SECRET_GENIUSPAY = "ss_sandbox_secret-factice"
SECRET_WEBHOOK_GENIUSPAY = "whsec_secret-factice"
#: Secret renvoyé par le faux serveur à la création d'un webhook.
SECRET_WEBHOOK_CREE = "whsec_secret-cree-jamais-affiche"
#: Heure figée de l'adaptateur pendant les tests (tolérance de la signature).
MAINTENANT_GENIUSPAY = 1_800_000_000
#: Champ retiré du corps (différent de « null »).
ABSENT = object()


class Brut:
    """Valeur écrite telle quelle dans le JSON : Brut("10000.00") donne
    10000.00, sans guillemets, comme dans les exemples de la documentation."""

    def __init__(self, texte):
        self.texte = texte


def json_brut(donnees):
    texte = json.dumps(donnees, default=lambda valeur: f"@@{valeur.texte}@@")
    return re.sub(r'"@@(.*?)@@"', r"\1", texte).encode()


def sans_absents(donnees):
    return {cle: valeur for cle, valeur in donnees.items() if valeur is not ABSENT}


def signature_geniuspay(corps, horodatage, secret=SECRET_WEBHOOK_GENIUSPAY):
    """Calcul indépendant de l'adaptateur : HMAC-SHA256(horodatage + "." + corps), en hexadécimal."""
    return hmac.new(secret.encode(), f"{horodatage}.".encode() + corps, hashlib.sha256).hexdigest()


def erreur_geniuspay(code, message="Erreur"):
    return {"success": False, "error": {"code": code, "message": message}}


class FauxGeniusPay:
    """Faux serveur GeniusPay, branché sous `requests` (HTTPAdapter.send) :
    tout appel HTTP du processus passe ici, quelle que soit la fonction de
    `requests` utilisée. Une URL hors de la base configurée, un appel sans
    délai maximal ou sans vérification TLS lèvent AssertionError.

    Réponses au format exact de la documentation (POST /payments,
    GET /payments/{reference}, erreur 404). `panne` remplace toute réponse :
    une exception (levée) ou (code, corps[, en-têtes]). POST /webhooks : la
    documentation ne montre pas sa réponse, `reponse_webhook` la remplace."""

    def __init__(self, environnement="sandbox"):
        self.environnement = environnement
        self.transactions = {}
        self.appels = []  # (méthode, chemin, corps, en-têtes)
        self.panne = None
        self.reponse_initiation = {}  # champs remplacés dans la réponse du POST
        self.prochaine_reference = None
        self.reponse_webhook = None  # « data » renvoyé par POST /webhooks

    def envoyer(self, _adaptateur, requete, **options):
        assert requete.url.startswith(f"{BASE_GENIUSPAY}/"), f"Appel réseau hors du faux GeniusPay : {requete.url}"
        assert options.get("timeout"), "Appel sans délai maximal."
        assert options.get("verify") is not False, "Vérification TLS désactivée."
        chemin = requete.url[len(BASE_GENIUSPAY):]
        corps = json.loads(requete.body) if requete.body else None
        self.appels.append((requete.method, chemin, corps, dict(requete.headers)))
        if isinstance(self.panne, Exception):
            raise self.panne
        code, contenu, *entetes = self.panne or self.traiter(requete.method, chemin, corps, requete.headers)
        reponse = requests.Response()
        reponse.status_code, reponse.url, reponse.request = code, requete.url, requete
        reponse._content = contenu if isinstance(contenu, bytes) else json_brut(contenu)
        reponse._content_consumed = True
        reponse.headers.update(entetes[0] if entetes else {})
        return reponse

    def traiter(self, methode, chemin, corps, entetes):
        if entetes.get("X-API-Key") != CLE_GENIUSPAY or entetes.get("X-API-Secret") != SECRET_GENIUSPAY:
            return 401, erreur_geniuspay("UNAUTHORIZED", "Invalid API credentials")
        if (methode, chemin) == ("POST", "/payments"):
            return 201, {"success": True, "data": self.creer(corps)}
        if (methode, chemin) == ("POST", "/webhooks"):
            return 201, {"success": True, "data": self.reponse_webhook or {
                "id": 77, "name": corps["name"], "url": corps["url"], "events": corps["events"],
                "secret": SECRET_WEBHOOK_CREE, "is_active": True,
            }}
        if methode == "GET" and chemin.startswith("/payments/"):
            transaction = self.transactions.get(unquote(chemin[len("/payments/"):]))
            if transaction is None:
                return 404, erreur_geniuspay("TRANSACTION_NOT_FOUND", "Transaction not found")
            return 200, {"success": True, "data": transaction}
        raise AssertionError(f"Appel inattendu au faux GeniusPay : {methode} {chemin}")

    def creer(self, corps):
        numero = len(self.transactions) + 1
        reference = self.prochaine_reference or f"MTX-FAUX{numero:06d}"
        self.prochaine_reference = None
        montant = corps["amount"]
        self.transactions[reference] = {
            "id": 455 + numero, "reference": reference, "amount": montant, "currency": "XOF", "fees": 450,
            "net_amount": montant - 450, "status": "pending", "payment_method": "wave", "payment_provider": "wave",
            "environment": self.environnement, "customer": corps["customer"], "success_url": corps["success_url"],
            "error_url": corps["error_url"], "metadata": corps["metadata"],
            "created_at": "2025-01-15T10:30:00.000000Z", "completed_at": None,
        }
        return sans_absents({
            "id": 455 + numero, "reference": reference, "amount": montant, "fees": 450, "net_amount": montant - 450,
            "status": "pending", "checkout_url": f"https://pay.geniuspay.test/checkout/{reference}", "gateway": "wave",
            "environment": self.environnement, "metadata": corps["metadata"], **self.reponse_initiation,
        })


class DonneesGeniusPay(Donnees):
    """GeniusPay configuré en sandbox, faux serveur branché, heure figée."""

    def configurer_geniuspay(self, environnement="sandbox"):
        reglages = self.settings(
            PAIEMENT_FOURNISSEUR="geniuspay", GENIUSPAY_API_KEY=CLE_GENIUSPAY, GENIUSPAY_API_SECRET=SECRET_GENIUSPAY,
            GENIUSPAY_WEBHOOK_SECRET=SECRET_WEBHOOK_GENIUSPAY, GENIUSPAY_ENVIRONNEMENT=environnement,
            GENIUSPAY_API_URL=BASE_GENIUSPAY,
        )
        reglages.enable()
        self.addCleanup(reglages.disable)
        self.faux = FauxGeniusPay(environnement)
        branchement = mock.patch.object(requests.adapters.HTTPAdapter, "send", autospec=True,
                                        side_effect=self.faux.envoyer)
        branchement.start()
        self.addCleanup(branchement.stop)
        horloge = mock.patch.object(geniuspay, "time")
        horloge.start().time.return_value = float(MAINTENANT_GENIUSPAY)
        self.addCleanup(horloge.stop)
        self.adaptateur = FournisseurGeniusPay()

    def encaisser(self, paiement=None, **champs):
        """La transaction devient « completed » chez GeniusPay ; `champs`
        remplace des champs de la réponse du GET (ABSENT : champ retiré)."""
        transaction = self.faux.transactions[(paiement or self.paiement).transaction_id_externe]
        transaction.update({"status": "completed", "completed_at": "2025-01-15T10:31:22.000000Z", **champs})
        for cle in [cle for cle, valeur in transaction.items() if valeur is ABSENT]:
            del transaction[cle]

    def evenement(self, evenement="payment.success", paiement=None, identifiant=None, environnement="sandbox",
                  **data):
        """Corps d'un webhook au format de la documentation (montants décimaux)."""
        paiement = paiement or self.paiement
        du = int(paiement.montant)
        return sans_absents({
            "id": identifiant or str(uuid.uuid4()),
            "event": evenement,
            "timestamp": MAINTENANT_GENIUSPAY,
            "created_at": "2025-12-30T12:00:00.000000Z",
            "data": sans_absents({
                "object": "transaction", "id": 12345, "reference": paiement.transaction_id_externe,
                "amount": Brut(f"{du}.00"), "currency": "XOF", "fees": Brut("250.00"),
                "net_amount": Brut(f"{du - 250}.00"), "status": "completed", "payment_method": "mobile_money",
                "provider": "wave", "customer_name": "Jean Kouassi", "customer_phone": "+2250748123456",
                "merchant_id": 25, "metadata": {"reference": paiement.reference},
                "environment": environnement, **data,
            }),
            "api_version": "2024-01-01",
        })

    def poster(self, corps, horodatage=MAINTENANT_GENIUSPAY, secret=SECRET_WEBHOOK_GENIUSPAY, signe=None,
               transfert=False, **entetes):
        """Envoie le webhook. `signe` : octets sur lesquels la signature est
        calculée, s'ils diffèrent du corps envoyé. En-tête à ABSENT : retiré."""
        brut = corps if isinstance(corps, bytes) else json_brut(corps)
        valeurs = sans_absents({
            "HTTP_X_WEBHOOK_SIGNATURE": signature_geniuspay(brut if signe is None else signe, horodatage, secret),
            "HTTP_X_WEBHOOK_TIMESTAMP": str(horodatage),
            **entetes,
        })
        return self.api().post(url_webhook("geniuspay", transfert), data=brut, content_type="application/json",
                               **valeurs)

    def verifications(self):
        """Chemins demandés par GET au faux serveur, dans l'ordre."""
        return [chemin for methode, chemin, _, _ in self.faux.appels if methode == "GET"]

    def statuts(self, paiement=None, commande=None):
        paiement, commande = paiement or self.paiement, commande or self.commande
        paiement.refresh_from_db()
        commande.refresh_from_db()
        return paiement.statut, commande.status


class GeniusPayTests(DonneesGeniusPay, APITestCase):
    """Encaissement par GeniusPay. Le webhook n'est qu'un signal signé :
    seule la réponse du GET /payments/{reference} fait foi."""

    def setUp(self):
        self.creer_donnees()
        (self.commande,) = self.commander((self.variante1, 3))
        self.configurer_geniuspay()
        self.paiement = self.payer(self.commande)
        self.du = int(self.paiement.montant)
        self.reference = self.paiement.transaction_id_externe

    def autre_paiement(self):
        (commande,) = self.commander((self.variante2, 1))
        return self.payer(commande), commande

    # --- Initiation ---------------------------------------------------------

    def test_initiation(self):
        (methode, chemin, corps, entetes), = self.faux.appels
        self.assertEqual((methode, chemin), ("POST", "/payments"))
        self.assertEqual((entetes["X-API-Key"], entetes["X-API-Secret"]), (CLE_GENIUSPAY, SECRET_GENIUSPAY))
        self.assertEqual(corps["amount"], 15000 + FRAIS_ABIDJAN)
        self.assertIs(type(corps["amount"]), int)
        self.assertEqual(corps["customer"], {"name": "Aya Konan", "email": "c1@pay.ci", "country": "CI"})
        retour = f"{settings.FRONTEND_BASE_URL}/paiement/retour?reference={self.paiement.reference}"
        self.assertEqual((corps["success_url"], corps["error_url"]), (retour, retour))
        self.assertEqual(corps["metadata"], {"reference": self.paiement.reference})
        self.assertEqual(
            (self.paiement.fournisseur, self.paiement.statut, self.reference, self.paiement.url_paiement,
             self.paiement.hash_jeton_notification),
            ("geniuspay", "en_attente", "MTX-FAUX000001", "https://pay.geniuspay.test/checkout/MTX-FAUX000001", ""),
        )
        contenu = self.api(self.client1).get(f"/api/paiements/{self.paiement.pk}/").content
        for secret in (CLE_GENIUSPAY, SECRET_GENIUSPAY, SECRET_WEBHOOK_GENIUSPAY):
            self.assertNotIn(secret.encode(), contenu)

    def test_initiation_avec_le_telephone_du_client(self):
        Utilisateur.objects.filter(pk=self.client2.pk).update(telephone="+2250101010101")
        self.client2.refresh_from_db()
        (commande,) = self.commander((self.variante2, 1), client=self.client2)
        self.payer(commande, client=self.client2)
        self.assertEqual(self.faux.appels[-1][2]["customer"]["phone"], "+2250101010101")

    def test_initiation_refusee(self):
        _, commande = self.autre_paiement()
        cible = {"commande_id": str(commande.pk)}
        cas = {
            "environnement de la réponse différent": {"reponse_initiation": {"environment": "live"}},
            "environnement absent": {"reponse_initiation": {"environment": ABSENT}},
            "référence absente": {"reponse_initiation": {"reference": ABSENT}},
            "référence non textuelle": {"reponse_initiation": {"reference": 456}},
            "URL de paiement absente": {"reponse_initiation": {"checkout_url": ABSENT}},
            "URL de paiement sans TLS": {"reponse_initiation": {"checkout_url": "http://pay.geniuspay.test/x"}},
            "redirection (jamais suivie)": {"panne": (302, b"", {"Location": "https://ailleurs.test/vol"})},
            "délai dépassé": {"panne": requests.Timeout()},
            "500": {"panne": (500, erreur_geniuspay("INTERNAL_ERROR"))},
            "401": {"panne": (401, erreur_geniuspay("UNAUTHORIZED"))},
        }
        for libelle, reglage in cas.items():
            with self.subTest(libelle):
                Paiement.objects.filter(commandes=commande).update(statut=Paiement.Statut.ANNULE)
                deja = list(Paiement.objects.filter(commandes=commande).values_list("pk", flat=True))
                self.faux.reponse_initiation, self.faux.panne = reglage.get("reponse_initiation", {}), reglage.get("panne")
                self.assertEqual(self.initier(**cible).status_code, 502)
                refuse = Paiement.objects.filter(commandes=commande).exclude(pk__in=deja).get()
                self.assertEqual((refuse.statut, refuse.transaction_id_externe, refuse.url_paiement),
                                 ("echoue", None, None))
        self.faux.reponse_initiation, self.faux.panne = {}, None
        self.assertEqual(self.initier(**cible).status_code, 201)

    def test_initiation_minimum_200_fcfa(self):
        _, commande = self.autre_paiement()
        Paiement.objects.filter(commandes=commande).update(statut=Paiement.Statut.ANNULE)
        nombre = len(self.faux.appels)
        Commande.objects.filter(pk=commande.pk).update(montant_total=Decimal("199"))
        self.assertEqual(self.initier(commande_id=str(commande.pk)).status_code, 400)
        self.assertEqual(len(self.faux.appels), nombre)  # refusé avant tout appel
        Commande.objects.filter(pk=commande.pk).update(montant_total=Decimal("200"))
        self.assertEqual(self.initier(commande_id=str(commande.pk)).status_code, 201)
        self.assertEqual(self.faux.appels[-1][2]["amount"], 200)

    def test_cles_absentes_aucun_appel(self):
        _, commande = self.autre_paiement()
        Paiement.objects.filter(commandes=commande).update(statut=Paiement.Statut.ANNULE)
        nombre = len(self.faux.appels)
        for reglage in ({"GENIUSPAY_API_KEY": ""}, {"GENIUSPAY_API_SECRET": ""}, {"GENIUSPAY_ENVIRONNEMENT": ""},
                        {"GENIUSPAY_ENVIRONNEMENT": "production"}):
            with self.subTest(reglage=reglage), self.settings(**reglage):
                self.assertEqual(self.initier(commande_id=str(commande.pk)).status_code, 502)
                with self.assertRaises(ErreurFournisseur):
                    self.adaptateur.verifier_transaction(self.paiement)
        self.assertEqual(len(self.faux.appels), nombre)

    # --- Signature du webhook (règle 4) --------------------------------------

    def test_webhook_signe_puis_get_completed_valide(self):
        self.encaisser()
        r = self.poster(self.evenement())
        self.assertEqual((r.status_code, r.data["message"]), (200, f"Paiement {self.paiement.reference} traité."))
        self.assertEqual(self.statuts(), ("valide", "confirmee"))
        self.assertEqual(self.verifications(), [f"/payments/{self.reference}"])
        self.assertEqual(Reversement.objects.filter(commande=self.commande).count(), 1)

    def test_route_du_webhook_passe_par_le_registre_jusqu_a_l_adaptateur(self):
        self.encaisser()
        lire_reel = FournisseurGeniusPay.lire_notification
        # Le fournisseur de la route vient de l'URL, pas du réglage actif.
        with self.settings(PAIEMENT_FOURNISSEUR="simule"), mock.patch.object(
            FournisseurGeniusPay, "lire_notification", autospec=True, side_effect=lire_reel,
        ) as lire:
            self.assertEqual(self.poster(self.evenement(), secret="whsec_autre").status_code, 401)
            self.assertEqual(lire.call_count, 1)
            self.assertIsInstance(lire.call_args.args[0], FournisseurGeniusPay)
            self.assertEqual(self.statuts(), ("en_attente", "creee"))
            self.assertEqual(self.poster(self.evenement()).status_code, 200)
            self.assertEqual(lire.call_count, 2)
        self.assertEqual(self.statuts(), ("valide", "confirmee"))

    def test_corps_reserialise_refuse(self):
        self.encaisser()
        # Montants entiers : json.dumps(json.loads(corps)) redonne exactement le corps signé.
        signe = json.dumps(self.evenement(amount=self.du, fees=250, net_amount=self.du - 250)).encode()
        reserialise = json.dumps(json.loads(signe), separators=(",", ":")).encode()
        self.assertNotEqual(signe, reserialise)
        self.assertEqual(json.loads(signe), json.loads(reserialise))
        self.assertEqual(self.poster(reserialise, signe=signe).status_code, 401)
        self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.assertEqual(self.verifications(), [])
        self.assertEqual(self.poster(signe).status_code, 200)
        self.assertEqual(self.statuts(), ("valide", "confirmee"))

    def test_signature_invalide_absente_ou_secret_vide(self):
        self.encaisser()
        corps = self.evenement()
        cas = {
            "mauvais secret": {"secret": "whsec_autre"},
            "signature absente": {"HTTP_X_WEBHOOK_SIGNATURE": ABSENT},
            "signature vide": {"HTTP_X_WEBHOOK_SIGNATURE": ""},
            "signature non ASCII": {"HTTP_X_WEBHOOK_SIGNATURE": "é" * 64},
            "horodatage absent": {"HTTP_X_WEBHOOK_TIMESTAMP": ABSENT},
            "horodatage non numérique": {"HTTP_X_WEBHOOK_TIMESTAMP": "maintenant"},
            "horodatage décimal": {"HTTP_X_WEBHOOK_TIMESTAMP": f"{MAINTENANT_GENIUSPAY}.0"},
            "horodatage d'un autre envoi": {"HTTP_X_WEBHOOK_TIMESTAMP": str(MAINTENANT_GENIUSPAY + 1)},
        }
        for libelle, options in cas.items():
            with self.subTest(libelle):
                self.assertEqual(self.poster(corps, **options).status_code, 401)
        with self.subTest("secret non configuré"), self.settings(GENIUSPAY_WEBHOOK_SECRET=""):
            # Même signée avec la clé vide, la notification est refusée.
            self.assertEqual(self.poster(corps, secret="").status_code, 401)
        self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.assertEqual(self.verifications(), [])
        self.assertFalse(JournalWebhook.objects.exists())

    def test_horodatage_tolerance_de_300_secondes(self):
        self.encaisser()
        for ecart in (-301, 301):
            with self.subTest(ecart=ecart):
                r = self.poster(self.evenement(), horodatage=MAINTENANT_GENIUSPAY + ecart)
                self.assertEqual(r.status_code, 401)
                self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.assertEqual(self.verifications(), [])
        for ecart in (-299, 299):
            with self.subTest(ecart=ecart):
                r = self.poster(self.evenement(), horodatage=MAINTENANT_GENIUSPAY + ecart)
                self.assertEqual(r.status_code, 200)
        self.assertEqual(self.statuts(), ("valide", "confirmee"))

    def test_comparaison_a_temps_constant(self):
        with mock.patch.object(geniuspay.hmac, "compare_digest", wraps=hmac.compare_digest) as comparer:
            self.assertEqual(self.poster(self.evenement(), secret="whsec_autre").status_code, 401)
            self.assertEqual(self.poster(self.evenement()).status_code, 200)
        self.assertEqual(comparer.call_count, 2)

    def test_json_lu_seulement_apres_la_signature(self):
        with mock.patch.object(geniuspay.json, "loads", wraps=json.loads) as lire:
            self.assertEqual(self.poster(b"{pas du json", secret="whsec_autre").status_code, 401)
            self.assertEqual(self.poster(json_brut(self.evenement()), secret="whsec_autre").status_code, 401)
            lire.assert_not_called()
            # Signé mais illisible : refusé aussi, après lecture.
            for corps in (b"{pas du json", b"[]", b'"texte"', b'{"event": "payment.success", "data": {"amount": NaN}}'):
                self.assertEqual(self.poster(corps).status_code, 401)
            self.assertTrue(lire.called)

    # --- Le GET seul fait foi (règles 1, 2, 3) --------------------------------

    def test_webhook_succes_signe_mais_get_pending_ne_valide_pas(self):
        corps = self.evenement(identifiant="evt-attente")  # « payment.success », statut « completed »
        r = self.poster(corps)
        self.assertEqual((r.status_code, r.data["message"]), (200, "Transaction non finalisée."))
        self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.assertEqual(JournalWebhook.objects.get(evenement_id="evt-attente").statut_traitement, "ignore")
        self.assertFalse(Reversement.objects.exists())
        # Rejoué une fois la transaction finalisée chez GeniusPay : traité.
        self.encaisser()
        self.assertEqual(self.poster(corps).status_code, 200)
        self.assertEqual(self.statuts(), ("valide", "confirmee"))

    def test_contenu_du_webhook_jamais_utilise(self):
        # Webhook d'échec, montant et devise faux : le GET dit « completed », montant dû, XOF.
        self.encaisser()
        r = self.poster(self.evenement("payment.failed", status="failed", amount=1, currency="EUR"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.statuts(), ("valide", "confirmee"))
        # Webhook de succès : le GET dit « failed ».
        paiement, commande = self.autre_paiement()
        self.faux.transactions[paiement.transaction_id_externe]["status"] = "failed"
        self.assertEqual(self.poster(self.evenement(paiement=paiement)).status_code, 200)
        self.assertEqual(self.statuts(paiement, commande), ("echoue", "creee"))

    def test_montant_decimal_a_fraction_nulle_accepte(self):
        # Forme du webhook de la documentation (10000.00), ici dans le GET.
        self.encaisser(amount=Brut(f"{self.du}.00"), fees=Brut("450.00"), net_amount=Brut(f"{self.du - 450}.00"))
        self.assertEqual(self.poster(self.evenement()).status_code, 200)
        self.assertEqual(self.statuts(), ("valide", "confirmee"))

    def test_lecture_du_montant_du_get(self):
        du = self.du
        acceptes = {"entier": du, "décimal .00": Brut(f"{du}.00"), "décimal .0": Brut(f"{du}.0"),
                    "exposant": Brut(f"{du // 100}E2")}
        refuses = {"fractionnaire": Brut(f"{du}.50"), "fractionnaire infime": Brut(f"{du}.0000001"),
                   "texte": str(du), "texte décimal": f"{du}.00", "booléen": True, "absent": ABSENT, "nul": None,
                   "liste": [du], "objet": {"value": du}}
        for libelle, valeur in acceptes.items():
            with self.subTest(libelle):
                self.encaisser(amount=valeur)
                etat = self.adaptateur.verifier_transaction(self.paiement)
                self.assertEqual((etat.statut, etat.montant, etat.devise), (SUCCES, du, "XOF"))
                self.assertIs(type(etat.montant), int)
        for libelle, valeur in refuses.items():
            with self.subTest(libelle):
                self.encaisser(amount=valeur)
                etat = self.adaptateur.verifier_transaction(self.paiement)
                self.assertEqual((etat.statut, etat.montant), (SUCCES, None))

    def test_succes_refuse_sans_montant_et_devise_conformes(self):
        du = self.du
        cas = {
            "montant fractionnaire (.50)": {"amount": Brut(f"{du}.50")},
            "montant en texte": {"amount": str(du)},
            "montant booléen": {"amount": True},
            "montant absent": {"amount": ABSENT},
            "montant nul": {"amount": None},
            "montant inférieur": {"amount": du - 1},
            "montant supérieur": {"amount": du + 1},
            "net_amount égal au dû, amount différent": {"amount": du + 450, "fees": 450, "net_amount": du},
            "fees égal au dû, amount différent": {"amount": du * 2, "fees": du, "net_amount": du},
            "devise absente": {"currency": ABSENT},
            "devise EUR": {"currency": "EUR"},
            "devise vide": {"currency": ""},
            "devise nulle": {"currency": None},
            "devise non textuelle": {"currency": 952},
        }
        for libelle, champs in cas.items():
            with self.subTest(libelle):
                self.encaisser(**{"amount": du, "currency": "XOF", "fees": 450, "net_amount": du - 450, **champs})
                # Le webhook, lui, annonce le montant dû en XOF.
                self.assertEqual(self.poster(self.evenement()).status_code, 400)
                self.assertEqual(reconcilier_paiement(self.paiement), ("refuse", False))
                self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.assertEqual(JournalWebhook.objects.filter(statut_traitement="erreur").count(), len(cas))
        self.assertFalse(Reversement.objects.exists())
        self.assertFalse(Livraison.objects.exists())

    def test_net_amount_et_fees_ignores(self):
        # amount = dû ; net_amount et fees quelconques, absents ou illisibles.
        self.encaisser(amount=self.du, fees="x", net_amount=ABSENT)
        self.assertEqual(self.poster(self.evenement()).status_code, 200)
        self.assertEqual(self.statuts(), ("valide", "confirmee"))

    # --- Référence, metadata, environnement (règles 6 et 7) -------------------

    def test_format_de_la_reference_indifferent(self):
        # « TXN-… » dans le webhook de la documentation, « MTX-… » ailleurs.
        self.faux.prochaine_reference = "TXN-2025-001"
        paiement, commande = self.autre_paiement()
        self.assertEqual(paiement.transaction_id_externe, "TXN-2025-001")
        self.encaisser(paiement)
        self.assertEqual(self.poster(self.evenement(paiement=paiement)).status_code, 200)
        self.assertEqual(self.statuts(paiement, commande), ("valide", "confirmee"))
        self.assertEqual(self.verifications(), ["/payments/TXN-2025-001"])
        self.assertEqual(self.statuts(), ("en_attente", "creee"))

    def test_reference_inconnue_absente_ou_differente_dans_le_get(self):
        self.encaisser()
        self.assertEqual(self.poster(self.evenement(reference="MTX-INCONNUE")).status_code, 404)
        # La référence ANITCHE (PAY-…) n'est pas une clé de recherche.
        self.assertEqual(self.poster(self.evenement(reference=self.paiement.reference)).status_code, 404)
        for valeur in (ABSENT, "", None, 456):
            with self.subTest(reference=valeur):
                self.assertEqual(self.poster(self.evenement(reference=valeur)).status_code, 401)
        self.assertEqual(self.verifications(), [])
        # Le GET renvoie une autre référence que celle stockée à l'initiation.
        for valeur in ("MTX-AUTRE", ABSENT, None):
            with self.subTest(reference_du_get=valeur):
                self.encaisser(reference=valeur)
                self.assertEqual(self.poster(self.evenement()).status_code, 400)
                self.assertEqual(reconcilier_paiement(self.paiement), ("refuse", False))
        self.assertEqual(self.statuts(), ("en_attente", "creee"))

    def test_paiement_ouvert_chez_un_autre_fournisseur_refuse(self):
        self.encaisser()
        Paiement.objects.filter(pk=self.paiement.pk).update(fournisseur="simule")
        self.assertEqual(self.poster(self.evenement()).status_code, 404)
        self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.assertEqual(self.verifications(), [])

    def test_recoupement_par_metadata(self):
        autre, autre_commande = self.autre_paiement()
        self.encaisser()
        # Webhook : référence GeniusPay de ce paiement, référence ANITCHE d'un autre.
        r = self.poster(self.evenement(metadata={"reference": autre.reference}))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.verifications(), [])
        # GET : metadata d'un autre paiement, vide, absente ou illisible.
        for metadata in ({"reference": autre.reference}, {"order_id": self.paiement.reference}, {}, None, ABSENT,
                         {"reference": 12}, "texte"):
            with self.subTest(metadata_du_get=metadata):
                self.encaisser(metadata=metadata)
                self.assertEqual(self.poster(self.evenement(metadata=ABSENT)).status_code, 400)
                self.assertEqual(reconcilier_paiement(self.paiement), ("refuse", False))
        self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.assertEqual(self.statuts(autre, autre_commande), ("en_attente", "creee"))
        # Webhook sans metadata, GET conforme : validé (le GET fait foi).
        self.encaisser(metadata={"reference": self.paiement.reference})
        self.assertEqual(self.poster(self.evenement(metadata=ABSENT)).status_code, 200)
        self.assertEqual(self.statuts(), ("valide", "confirmee"))

    def test_reference_geniuspay_portee_par_deux_paiements_refusee(self):
        autre, autre_commande = self.autre_paiement()
        Paiement.objects.filter(pk=autre.pk).update(transaction_id_externe=self.reference)
        self.encaisser()
        self.assertEqual(self.poster(self.evenement(metadata=ABSENT)).status_code, 400)
        self.assertEqual(self.verifications(), [])
        self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.assertEqual(self.statuts(autre, autre_commande), ("en_attente", "creee"))

    def test_environnement_croise_refuse(self):
        self.encaisser()
        # Webhook d'un autre environnement, ou sans environnement.
        for environnement in ("live", "LIVE", "", ABSENT):
            with self.subTest(environnement_du_webhook=environnement):
                self.assertEqual(self.poster(self.evenement(environnement=environnement)).status_code, 401)
        self.assertEqual(self.verifications(), [])
        self.assertFalse(JournalWebhook.objects.exists())
        # GET d'un autre environnement, ou sans environnement.
        for environnement in ("live", ABSENT, None):
            with self.subTest(environnement_du_get=environnement):
                self.encaisser(environment=environnement)
                self.assertEqual(self.poster(self.evenement()).status_code, 400)
                self.assertEqual(reconcilier_paiement(self.paiement), ("refuse", False))
        # Configuration « live », webhook et transaction « sandbox ».
        self.encaisser(environment="sandbox")
        with self.settings(GENIUSPAY_ENVIRONNEMENT="live"):
            self.assertEqual(self.poster(self.evenement()).status_code, 401)
            self.assertEqual(reconcilier_paiement(self.paiement), ("refuse", False))
        self.assertEqual(self.statuts(), ("en_attente", "creee"))
        # Environnements concordants : validé.
        self.assertEqual(self.poster(self.evenement()).status_code, 200)
        self.assertEqual(self.statuts(), ("valide", "confirmee"))

    # --- Idempotence (règle 5) -------------------------------------------------

    def test_rejeu_du_meme_id_traite_une_fois(self):
        self.encaisser()
        corps = self.evenement(identifiant="550e8400-e29b-41d4-a716-446655440000")
        self.assertEqual(self.poster(corps).status_code, 200)
        for _ in range(2):
            r = self.poster(corps)
            self.assertEqual((r.status_code, r.data["message"]), (200, "Événement déjà traité."))
        journal = JournalWebhook.objects.get()
        self.assertEqual((journal.fournisseur, journal.evenement_id, journal.statut_traitement),
                         ("geniuspay", "550e8400-e29b-41d4-a716-446655440000", "traite"))
        self.assertEqual(len(self.verifications()), 1)
        self.assertEqual(Reversement.objects.count(), 1)
        self.assertEqual(Notification.objects.filter(destinataire=self.client1, titre="Paiement confirmé").count(), 1)

    def test_cle_de_secours_sans_id(self):
        self.encaisser()
        for identifiant in (ABSENT, "", 42):
            with self.subTest(id=identifiant):
                corps = self.evenement()
                corps["id"] = identifiant
                self.assertEqual(self.poster(sans_absents(corps)).status_code, 200)
        self.assertEqual(JournalWebhook.objects.get().evenement_id, f"payment.success:{self.reference}")
        self.assertEqual(len(self.verifications()), 1)

    # --- Événements sans effet (règle 8) et statuts (règle 9) -----------------

    def test_webhook_test_sans_effet(self):
        self.encaisser()
        essai = {"id": str(uuid.uuid4()), "event": "webhook.test", "timestamp": MAINTENANT_GENIUSPAY,
                 "data": {"message": "Test webhook"}, "environment": "sandbox", "api_version": "2024-01-01"}
        # Même s'il désigne un vrai paiement encaissé.
        for corps in (essai, self.evenement("webhook.test")):
            with self.assertLogs("apps.paiements.services", "INFO") as journaux:
                r = self.poster(corps)
            self.assertEqual((r.status_code, r.data["message"]), (200, "Événement ignoré."))
            self.assertIn("essai du webhook", journaux.output[0])
        self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.assertEqual(self.verifications(), [])
        self.assertFalse(JournalWebhook.objects.exists())
        # Non signé : refusé comme tout autre.
        self.assertEqual(self.poster(essai, secret="whsec_autre").status_code, 401)

    def test_cashout_et_evenements_hors_perimetre_ignores_et_journalises(self):
        self.encaisser()
        for evenement in ("cashout.completed", "cashout.failed", "invoice.created"):
            with self.subTest(evenement), self.assertLogs("apps.paiements.services", "INFO") as journaux:
                r = self.poster(self.evenement(evenement))
                self.assertEqual((r.status_code, r.data["message"]), (200, "Événement ignoré."))
                self.assertIn(evenement, journaux.output[0])
        self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.assertEqual(self.verifications(), [])
        self.assertFalse(JournalWebhook.objects.exists())
        self.assertEqual(self.poster(self.evenement("cashout.completed", environnement="live")).status_code, 401)

    def test_statuts_du_get(self):
        cas = {"completed": SUCCES, "failed": ECHEC, "cancelled": ECHEC, "expired": ECHEC, "pending": EN_ATTENTE,
               "processing": EN_ATTENTE, "refunded": EN_ATTENTE, "inconnu": EN_ATTENTE, "COMPLETED": EN_ATTENTE,
               "": EN_ATTENTE,
               None: EN_ATTENTE, ABSENT: EN_ATTENTE, True: EN_ATTENTE}
        for statut, attendu in cas.items():
            with self.subTest(statut=statut):
                self.encaisser(status=statut)
                self.assertEqual(self.adaptateur.verifier_transaction(self.paiement).statut, attendu)

    def test_echecs_confirmes_par_le_get(self):
        for statut in ("failed", "cancelled", "expired"):
            with self.subTest(statut):
                paiement, commande = self.autre_paiement()
                self.encaisser(paiement, status=statut)
                self.assertEqual(self.poster(self.evenement(f"payment.{statut}", paiement=paiement)).status_code, 200)
                self.assertEqual(self.statuts(paiement, commande), ("echoue", "creee"))
                self.api(self.client1).post(f"/api/commandes/{commande.pk}/annuler/")

    def test_rembourse_aucune_transition_journalise(self):
        corps = self.evenement("payment.refunded", status="refunded")
        # Paiement encore en attente chez nous.
        self.encaisser(status="refunded")
        with self.assertLogs("securite", "WARNING") as journaux:
            self.assertEqual(self.poster(corps).status_code, 200)
            self.assertEqual(reconcilier_paiement(self.paiement), ("en_attente", False))
        self.assertIn(self.reference, journaux.output[0])
        self.assertIn("rembours", journaux.output[0])
        self.assertEqual(self.statuts(), ("en_attente", "creee"))
        # Paiement déjà validé : il le reste.
        self.encaisser()
        self.assertEqual(self.poster(self.evenement()).status_code, 200)
        self.encaisser(status="refunded")
        with self.assertLogs("securite", "WARNING"):
            self.assertEqual(self.poster(self.evenement("payment.refunded", status="refunded")).status_code, 200)
        self.assertEqual(self.statuts(), ("valide", "confirmee"))
        self.assertFalse(Remboursement.objects.exists())

    # --- Erreurs HTTP (règle 10) et réconciliation ------------------------------

    def test_classement_des_reponses_du_get(self):
        self.encaisser()
        encaissee = {"success": True, "data": self.faux.transactions[self.reference]}
        cas = {
            # Corps d'un succès conforme, mais code HTTP d'échec ou de redirection.
            "500 avec un corps de succès": ((500, encaissee), ErreurFournisseur),
            "302 avec un corps de succès": ((302, encaissee, {"Location": "https://ailleurs.test/vol"}),
                                            ErreurFournisseur),
            "404 TRANSACTION_NOT_FOUND": ((404, erreur_geniuspay("TRANSACTION_NOT_FOUND")), TransactionIntrouvable),
            "404 autre code": ((404, erreur_geniuspay("ROUTE_NOT_FOUND")), ErreurFournisseur),
            "404 illisible": ((404, b"<html>Not Found</html>"), ErreurFournisseur),
            "400": ((400, erreur_geniuspay("VALIDATION_ERROR")), ErreurFournisseur),
            "401": ((401, erreur_geniuspay("UNAUTHORIZED")), ErreurFournisseur),
            "403": ((403, erreur_geniuspay("FORBIDDEN")), ErreurFournisseur),
            "429": ((429, erreur_geniuspay("RATE_LIMITED")), ErreurFournisseur),
            "500": ((500, erreur_geniuspay("INTERNAL_ERROR")), ErreurFournisseur),
            "503 illisible": ((503, b"Service Unavailable"), ErreurFournisseur),
            "302": ((302, b"", {"Location": "https://ailleurs.test/vol"}), ErreurFournisseur),
            "200 illisible": ((200, b"<html>"), ErreurFournisseur),
            "200 vide": ((200, b""), ErreurFournisseur),
            "200 non UTF-8": ((200, b"\xff\xfe"), ErreurFournisseur),
            "200 liste": ((200, b"[]"), ErreurFournisseur),
            "200 NaN": ((200, b'{"success": true, "data": {"amount": NaN}}'), ErreurFournisseur),
            "200 success false": ((200, erreur_geniuspay("TRANSACTION_NOT_FOUND")), ErreurFournisseur),
            "200 sans success": ((200, {"data": {"status": "completed"}}), ErreurFournisseur),
            "200 sans data": ((200, {"success": True}), ErreurFournisseur),
            "200 data non objet": ((200, {"success": True, "data": []}), ErreurFournisseur),
            "délai dépassé": (requests.Timeout(), ErreurFournisseur),
            "connexion": (requests.ConnectionError(), ErreurFournisseur),
            "TLS": (requests.exceptions.SSLError(), ErreurFournisseur),
        }
        for libelle, (panne, attendue) in cas.items():
            with self.subTest(libelle):
                self.faux.panne = panne
                with self.assertRaises(attendue) as contexte:
                    self.adaptateur.verifier_transaction(self.paiement)
                if attendue is TransactionIntrouvable:
                    self.assertNotIsInstance(contexte.exception, ErreurFournisseur)
                # Rien n'est validé, par le webhook comme par la réconciliation.
                self.assertEqual(self.poster(self.evenement()).status_code, 503)
                issue = "refuse" if attendue is TransactionIntrouvable else "injoignable"
                self.assertEqual(reconcilier_paiement(self.paiement), (issue, False))
                self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.faux.panne = None
        with self.subTest("identifiants refusés par GeniusPay"), self.settings(GENIUSPAY_API_SECRET="sk_autre"):
            with self.assertRaises(ErreurFournisseur):
                self.adaptateur.verifier_transaction(self.paiement)
        with self.subTest("transaction inconnue de GeniusPay"):
            transaction = self.faux.transactions.pop(self.reference)
            with self.assertRaises(TransactionIntrouvable):
                self.adaptateur.verifier_transaction(self.paiement)
            self.faux.transactions[self.reference] = transaction
        with self.subTest("paiement sans référence GeniusPay"):
            nombre = len(self.faux.appels)
            for valeur in (None, ""):
                self.paiement.transaction_id_externe = valeur
                with self.assertRaises(TransactionIntrouvable):
                    self.adaptateur.verifier_transaction(self.paiement)
            self.assertEqual(len(self.faux.appels), nombre)

    def test_reference_encodee_dans_l_url(self):
        Paiement.objects.filter(pk=self.paiement.pk).update(transaction_id_externe="MTX/../payments?x=1")
        self.paiement.refresh_from_db()
        with self.assertRaises(TransactionIntrouvable):
            self.adaptateur.verifier_transaction(self.paiement)
        self.assertEqual(self.verifications(), ["/payments/MTX%2F..%2Fpayments%3Fx%3D1"])

    def test_reconciliation_sans_webhook(self):
        self.assertEqual(reconcilier_paiement(self.paiement), ("en_attente", False))
        self.encaisser()
        self.assertEqual(reconcilier_paiement(self.paiement), ("succes", True))
        self.assertEqual(self.statuts(), ("valide", "confirmee"))
        self.assertFalse(JournalWebhook.objects.exists())
        # Webhook tardif : sans second effet.
        self.assertEqual(self.poster(self.evenement()).status_code, 200)
        self.assertEqual(Reversement.objects.count(), 1)

    def test_tache_de_reconciliation(self):
        autre, autre_commande = self.autre_paiement()
        vieillir_paiement(self.paiement, 20)
        vieillir_paiement(autre, 16)
        self.faux.transactions.pop(self.reference)  # 404 pour le premier
        self.encaisser(autre)
        with self.assertLogs("securite", "ERROR"):
            self.assertEqual(reconcilier_paiements_en_suspens(), (2, 1, False))
        self.assertEqual(self.statuts(), ("en_attente", "creee"))
        self.assertEqual(self.statuts(autre, autre_commande), ("valide", "confirmee"))

    # --- Secrets (règle 11) et transferts ---------------------------------------

    def test_aucun_secret_ni_donnee_client_dans_les_journaux_ni_en_base(self):
        self.encaisser()
        corps = json_brut(self.evenement())
        signature = signature_geniuspay(corps, MAINTENANT_GENIUSPAY)
        with self.assertLogs("securite", "DEBUG") as securite, self.assertLogs("apps.paiements", "DEBUG") as module:
            self.poster(corps, secret="whsec_autre")
            self.poster(self.evenement(environnement="live"))
            self.poster(self.evenement("cashout.completed"))
            for panne in ((401, erreur_geniuspay("UNAUTHORIZED")), (500, b"<html>"), requests.Timeout(),
                          requests.ConnectionError(f"{CLE_GENIUSPAY} {SECRET_GENIUSPAY}")):
                self.faux.panne = panne
                self.poster(self.evenement())
                reconcilier_paiement(self.paiement)
            self.faux.panne = None
            self.assertEqual(self.poster(corps).status_code, 200)
        journaux = "\n".join(securite.output + module.output)
        for interdit in (CLE_GENIUSPAY, SECRET_GENIUSPAY, SECRET_WEBHOOK_GENIUSPAY, signature, "+2250748123456",
                         "Jean Kouassi"):
            self.assertNotIn(interdit, journaux)
        enregistre = json.dumps(list(JournalWebhook.objects.values("payload", "erreur")), default=str)
        for interdit in (CLE_GENIUSPAY, SECRET_GENIUSPAY, SECRET_WEBHOOK_GENIUSPAY, signature, "+2250748123456",
                         "Jean Kouassi"):
            self.assertNotIn(interdit, enregistre)
        self.assertIn(self.reference, enregistre)

    def test_transfert_non_implemente_versement_manuel_possible(self):
        self.encaisser()
        self.assertEqual(self.poster(self.evenement()).status_code, 200)
        reversement = Reversement.objects.get(commande=self.commande)
        Reversement.objects.filter(pk=reversement.pk).update(statut=Reversement.Statut.DISPONIBLE)
        with self.assertRaises(OperationNonSupportee):
            self.adaptateur.transferer(reversement, "+2250707070707", "wave")
        with self.assertRaises(OperationNonSupportee):
            self.adaptateur.verifier_transfert(reversement)
        nombre = len(self.faux.appels)
        admin = self.api(self.admin)
        r = admin.post(f"/api/paiements/admin/reversements/{reversement.pk}/transferer/", {"operateur": "wave"})
        self.assertEqual(r.status_code, 409)
        reversement.refresh_from_db()
        self.assertEqual((reversement.statut, reversement.montant_a_verser), ("disponible", Decimal("0")))
        self.assertEqual(len(self.faux.appels), nombre)
        r = admin.post(f"/api/paiements/admin/reversements/{reversement.pk}/verser/", {"reference_externe": "WAVE-1"})
        self.assertEqual((r.status_code, r.data["statut"]), (200, "verse"))
        self.assertEqual(Notification.objects.filter(
            destinataire=self.vendeur1, titre="Reversement effectué").count(), 1)

    def test_notification_de_transfert_refusee(self):
        for evenement in ("cashout.completed", "payment.success", "webhook.test"):
            with self.subTest(evenement):
                self.assertEqual(self.poster(self.evenement(evenement), transfert=True).status_code, 401)
        with self.assertRaises(NotificationInvalide):
            self.adaptateur.lire_notification_transfert(None)


class ReconciliationGeniusPayTests(DonneesGeniusPay, DonneesReconciliationReversement, APITestCase):
    """GeniusPay n'a pas de transferts par API : la réconciliation d'un
    reversement en cours chez lui est « non disponible », sans appel réseau
    ni changement d'état, et n'arrête pas le lot."""

    def setUp(self):
        super().setUp()
        self.chez_geniuspay = self.autre_reversement(minutes=60)  # premier du lot
        Reversement.objects.filter(pk=self.chez_geniuspay.pk).update(fournisseur="geniuspay")
        self.chez_geniuspay.refresh_from_db()
        self.distant(SUCCES)  # le reversement du fournisseur simulé, derrière
        self.configurer_geniuspay()

    def test_reconciliation_geniuspay_non_disponible(self):
        with self.assertLogs("apps.paiements.reversements", "WARNING") as journaux, \
                compter_verifications_de_transfert() as verifier:
            self.assertEqual(reversements.reconcilier_reversements_en_cours(), (2, 1, False))
        self.assertEqual(self.faux.appels, [])
        self.assertEqual(transferts_verifies(verifier), [self.reversement.reference])
        self.assertIn("Transfert par API non disponible chez geniuspay", "\n".join(journaux.output))
        self.chez_geniuspay.refresh_from_db()
        self.assertEqual((self.chez_geniuspay.statut, self.chez_geniuspay.montant_a_verser),
                         ("en_cours", Decimal("4100")))
        self.assertIsNotNone(self.chez_geniuspay.date_derniere_reconciliation)
        self.reversement.refresh_from_db()
        self.assertEqual(self.reversement.statut, "verse")
        self.assertEqual(reversements.reconcilier_reversement(self.chez_geniuspay),
                         (reversements.NON_DISPONIBLE, False))
        self.assertEqual(self.faux.appels, [])


class GeniusPayInactifParDefautTests(Donnees, APITestCase):
    """Sans configuration (réglages vides de base.py et test.py), GeniusPay
    est inscrit au registre mais n'ouvre, ne vérifie et n'accepte rien."""

    def setUp(self):
        self.creer_donnees()
        (self.commande,) = self.commander((self.variante1, 1))
        aucun_appel = mock.patch.object(requests.adapters.HTTPAdapter, "send",
                                        side_effect=AssertionError("Appel réseau réel interdit."))
        aucun_appel.start()
        self.addCleanup(aucun_appel.stop)

    def test_reglages_vides_et_fournisseur_actif_inchange(self):
        for nom in ("GENIUSPAY_API_KEY", "GENIUSPAY_API_SECRET", "GENIUSPAY_WEBHOOK_SECRET",
                    "GENIUSPAY_ENVIRONNEMENT", "GENIUSPAY_API_URL"):
            with self.subTest(nom):
                self.assertEqual(getattr(settings, nom), "")
        self.assertEqual(settings.PAIEMENT_FOURNISSEUR, "simule")
        self.assertEqual(self.payer(self.commande).fournisseur, "simule")

    def test_registre(self):
        self.assertEqual(set(FOURNISSEURS), {"simule", "geniuspay"})
        self.assertIsInstance(obtenir_fournisseur("geniuspay"), FournisseurGeniusPay)
        with self.assertRaises(FournisseurInconnu):
            obtenir_fournisseur("cinetpay")

    def test_url_de_l_api_par_defaut(self):
        self.assertEqual(FournisseurGeniusPay()._url("/payments"), "https://geniuspay.ci/api/v1/merchant/payments")
        with self.settings(GENIUSPAY_API_URL="https://exemple.test/api/"):
            self.assertEqual(FournisseurGeniusPay()._url("/payments"), "https://exemple.test/api/payments")

    def test_sans_configuration_rien_n_est_ouvert_verifie_ni_accepte(self):
        paiement = self.payer(self.commande)
        Paiement.objects.filter(pk=paiement.pk).update(fournisseur="geniuspay", transaction_id_externe="MTX-A1B2C3D4E5")
        paiement.refresh_from_db()
        adaptateur = FournisseurGeniusPay()
        with self.assertRaises(ErreurFournisseur):
            adaptateur.initier(paiement)
        with self.assertRaises(ErreurFournisseur):
            adaptateur.verifier_transaction(paiement)
        self.assertEqual(reconcilier_paiement(paiement), ("injoignable", False))
        corps = json.dumps({"id": "e", "event": "payment.success", "environment": "sandbox",
                            "data": {"reference": "MTX-A1B2C3D4E5"}}).encode()
        horodatage = int(time.time())
        r = self.api().post(url_webhook("geniuspay"), data=corps, content_type="application/json",
                            HTTP_X_WEBHOOK_SIGNATURE=signature_geniuspay(corps, horodatage, ""),
                            HTTP_X_WEBHOOK_TIMESTAMP=str(horodatage))
        self.assertEqual(r.status_code, 401)
        paiement.refresh_from_db()
        self.assertEqual(paiement.statut, Paiement.Statut.EN_ATTENTE)


# =====================================================================
# ESSAI EN SANDBOX (commande essai_geniuspay, réglages et journal de dev)
# =====================================================================

URL_TUNNEL = "https://essai-anitche.trycloudflare.com"
URL_WEBHOOK_TUNNEL = f"{URL_TUNNEL}/api/paiements/webhook/geniuspay/"
ENV_INITIAL = "SECRET_KEY=valeur-factice\n# clés GeniusPay\nGENIUSPAY_ENVIRONNEMENT=sandbox\n"


class EssaiGeniusPayTests(DonneesGeniusPay, APITestCase):
    """Commande essai_geniuspay : réservée au développement en sandbox, le
    secret du webhook ne va que dans .env (un fichier temporaire ici), le
    paiement passe par le vrai checkout et le vrai service."""

    def setUp(self):
        cache.clear()
        self.configurer_geniuspay()
        # Le lanceur de tests force DEBUG=False.
        reglages = self.settings(DEBUG=True, ALLOWED_HOSTS=["testserver", ".trycloudflare.com"])
        reglages.enable()
        self.addCleanup(reglages.disable)
        dossier = tempfile.TemporaryDirectory()
        self.addCleanup(dossier.cleanup)
        self.dossier = Path(dossier.name)
        self.env = self.dossier / ".env"
        self.ecrire_env(ENV_INITIAL)
        chemin = mock.patch.object(essai_geniuspay, "chemin_env", return_value=self.env)
        chemin.start()
        self.addCleanup(chemin.stop)

    def ecrire_env(self, texte):
        self.env.write_bytes(texte.encode())

    def lire_env(self):
        return self.env.read_bytes().decode()

    def lancer(self, *arguments, reponse=AssertionError("Confirmation demandée.")):
        """Sortie complète de la commande (stdout et stderr). `reponse` :
        saisie à la demande de confirmation, ou exception levée par input."""
        sortie = StringIO()
        with mock.patch("builtins.input", side_effect=[reponse]) as saisie:
            call_command("essai_geniuspay", *arguments, stdout=sortie, stderr=sortie)
        self.questions = [appel.args[0] for appel in saisie.call_args_list]
        return sortie.getvalue()

    def payer_par_la_commande(self, montant=500, *options):
        sortie = self.lancer("payer", "--montant", str(montant), *options)
        return sortie, Paiement.objects.latest("date_creation")

    # --- Réservée au développement en sandbox -----------------------------------

    def test_refusee_hors_developpement(self):
        cas = {
            "DEBUG désactivé": ({"DEBUG": False}, "DEBUG est désactivé"),
            "settings de production": ({"SETTINGS_MODULE": "config.settings.prod"}, "settings de production"),
            "fournisseur simulé": ({"PAIEMENT_FOURNISSEUR": "simule"}, "PAIEMENT_FOURNISSEUR n'est pas geniuspay"),
            "fournisseur inconnu": ({"PAIEMENT_FOURNISSEUR": "autre"}, "PAIEMENT_FOURNISSEUR n'est pas geniuspay"),
            "environnement live": ({"GENIUSPAY_ENVIRONNEMENT": "live"}, "GENIUSPAY_ENVIRONNEMENT n'est pas sandbox"),
            "clé publique live": ({"GENIUSPAY_API_KEY": "sk_live_factice"}, "GENIUSPAY_API_KEY ne commence pas"),
            "clé secrète live": ({"GENIUSPAY_API_SECRET": "ss_live_factice"}, "GENIUSPAY_API_SECRET ne commence pas"),
            "clés absentes": ({"GENIUSPAY_API_KEY": "", "GENIUSPAY_API_SECRET": ""}, "GENIUSPAY_API_KEY"),
        }
        actions = (("creer-webhook", "--url", URL_TUNNEL), ("payer", "--montant", "500"),
                   ("etat", "--reference", "PAY-INCONNU"))
        for nom, (reglages, message) in cas.items():
            for action in actions:
                with self.subTest(nom, action=action[0]), self.settings(**reglages):
                    with self.assertRaises(CommandError) as refus:
                        self.lancer(*action)
                    self.assertIn("réservée au développement en sandbox", str(refus.exception))
                    self.assertIn(message, str(refus.exception))
                    for secret in ("sk_live_factice", "ss_live_factice", CLE_GENIUSPAY, SECRET_GENIUSPAY):
                        self.assertNotIn(secret, str(refus.exception))
        self.assertEqual(self.faux.appels, [])
        self.assertEqual(self.lire_env(), ENV_INITIAL)
        self.assertFalse(Paiement.objects.exists() or Utilisateur.objects.exists())

    # --- creer-webhook ----------------------------------------------------------

    def test_creer_webhook_enregistre_le_secret_sans_jamais_l_afficher(self):
        with self.assertLogs("apps.paiements", "DEBUG") as journaux:
            sortie = self.lancer("creer-webhook", "--url", URL_TUNNEL)
        (methode, chemin, corps, entetes), = self.faux.appels
        self.assertEqual((methode, chemin), ("POST", "/webhooks"))
        self.assertEqual((entetes["X-API-Key"], entetes["X-API-Secret"]), (CLE_GENIUSPAY, SECRET_GENIUSPAY))
        self.assertEqual(corps, {
            "name": "ANITCHE essai sandbox", "url": URL_WEBHOOK_TUNNEL,
            "events": ["payment.initiated", "payment.success", "payment.failed", "payment.cancelled",
                       "payment.refunded", "payment.expired"],
        })
        self.assertIn("secret enregistré dans .env", sortie)
        self.assertIn("Webhook 77", sortie)
        self.assertIn(URL_WEBHOOK_TUNNEL, sortie)
        for texte in (sortie, "\n".join(journaux.output)):
            for interdit in (SECRET_WEBHOOK_CREE, "whsec_", CLE_GENIUSPAY, SECRET_GENIUSPAY):
                self.assertNotIn(interdit, texte)
        self.assertEqual(self.lire_env(), f"{ENV_INITIAL}GENIUSPAY_WEBHOOK_SECRET={SECRET_WEBHOOK_CREE}\n")
        self.assertEqual(self.questions, [])
        # Aucun fichier provisoire ne reste à côté du .env.
        self.assertEqual([fichier.name for fichier in self.dossier.iterdir()], [".env"])

    def test_creer_webhook_formes_de_l_url(self):
        for url in (URL_TUNNEL, f"{URL_TUNNEL}/", URL_WEBHOOK_TUNNEL):
            with self.subTest(url):
                self.faux.appels.clear()
                self.ecrire_env(ENV_INITIAL)
                self.lancer("creer-webhook", "--url", url)
                self.assertEqual(self.faux.appels[0][2]["url"], URL_WEBHOOK_TUNNEL)
        self.faux.appels.clear()
        self.ecrire_env(ENV_INITIAL)
        refusees = {
            "http": "http://essai-anitche.trycloudflare.com",
            "sans schéma": "essai-anitche.trycloudflare.com",
            "autre chemin": f"{URL_TUNNEL}/api/",
            "paramètres": f"{URL_TUNNEL}/?a=1",
            "hôte hors ALLOWED_HOSTS": "https://exemple.test",
        }
        for nom, url in refusees.items():
            with self.subTest(nom), self.assertRaises(CommandError):
                self.lancer("creer-webhook", "--url", url)
        self.assertEqual(self.faux.appels, [])
        self.assertEqual(self.lire_env(), ENV_INITIAL)

    def test_creer_webhook_variable_deja_definie_demande_confirmation(self):
        existant = f"{ENV_INITIAL}GENIUSPAY_WEBHOOK_SECRET=whsec_ancien\nFRONTEND_BASE_URL=http://localhost:5173\n"
        for nom, reponse in {"non": "n", "vide": "", "autre": "yes", "sans terminal": EOFError()}.items():
            with self.subTest(nom):
                self.ecrire_env(existant)
                with self.assertRaises(CommandError) as refus:
                    self.lancer("creer-webhook", "--url", URL_TUNNEL, reponse=reponse)
                self.assertNotIn("whsec_", str(refus.exception))
                self.assertEqual(self.lire_env(), existant)
        # Refusé avant tout appel : aucun webhook créé dont le secret serait perdu.
        self.assertEqual(self.faux.appels, [])
        for reponse in ("o", " OUI "):
            with self.subTest(reponse):
                self.ecrire_env(existant)
                sortie = self.lancer("creer-webhook", "--url", URL_TUNNEL, reponse=reponse)
                self.assertEqual(len(self.questions), 1)
                self.assertNotIn("whsec_", self.questions[0] + sortie)
                self.assertEqual(self.lire_env(), (
                    f"{ENV_INITIAL}GENIUSPAY_WEBHOOK_SECRET={SECRET_WEBHOOK_CREE}\n"
                    "FRONTEND_BASE_URL=http://localhost:5173\n"
                ))

    def test_creer_webhook_variable_vide_doublon_et_fins_de_ligne(self):
        # Variable sans valeur : pas de confirmation. Doublon retiré, CRLF et
        # dernière ligne sans fin de ligne conservés.
        self.ecrire_env("A=1\r\nGENIUSPAY_WEBHOOK_SECRET=\r\nB=2\r\nexport GENIUSPAY_WEBHOOK_SECRET = ''\r\nC=3")
        self.lancer("creer-webhook", "--url", URL_TUNNEL)
        self.assertEqual(self.lire_env(), f"A=1\r\nGENIUSPAY_WEBHOOK_SECRET={SECRET_WEBHOOK_CREE}\r\nB=2\r\nC=3")
        self.ecrire_env("A=1")
        self.lancer("creer-webhook", "--url", URL_TUNNEL)
        self.assertEqual(self.lire_env(), f"A=1\nGENIUSPAY_WEBHOOK_SECRET={SECRET_WEBHOOK_CREE}\n")

    def test_creer_webhook_secret_cherche_dans_la_reponse(self):
        trouves = {
            "autre nom de champ": {"id": "wh_1", "signing_secret": SECRET_WEBHOOK_CREE},
            "objet imbriqué": {"webhook": {"id": 5, "url": URL_WEBHOOK_TUNNEL, "secret": SECRET_WEBHOOK_CREE}},
            "champ inconnu de forme whsec_": {"id": 5, "cle_de_signature": SECRET_WEBHOOK_CREE},
        }
        for nom, reponse in trouves.items():
            with self.subTest(nom):
                self.ecrire_env(ENV_INITIAL)
                self.faux.reponse_webhook = reponse
                sortie = self.lancer("creer-webhook", "--url", URL_TUNNEL)
                self.assertNotIn(SECRET_WEBHOOK_CREE, sortie)
                self.assertIn(f"GENIUSPAY_WEBHOOK_SECRET={SECRET_WEBHOOK_CREE}\n", self.lire_env())
        absents = {
            "aucun secret": {"id": 9, "url": URL_WEBHOOK_TUNNEL, "jeton": "abc"},
            "secret sur deux lignes": {"id": 9, "secret": "whsec_a\nAUTRE=1"},
            "secret avec espace": {"id": 9, "secret": "whsec_a b"},
        }
        for nom, reponse in absents.items():
            with self.subTest(nom):
                self.ecrire_env(ENV_INITIAL)
                self.faux.reponse_webhook = reponse
                with self.assertRaises(CommandError) as refus:
                    self.lancer("creer-webhook", "--url", URL_TUNNEL)
                # Noms des champs reçus seulement, jamais leurs valeurs.
                self.assertIn("id 9", str(refus.exception))
                self.assertIn("champs reçus", str(refus.exception))
                self.assertNotIn("whsec_", str(refus.exception))
                self.assertNotIn("abc", str(refus.exception))
                self.assertEqual(self.lire_env(), ENV_INITIAL)

    def test_creer_webhook_refus_de_geniuspay_ou_env_absent(self):
        for panne in ((401, erreur_geniuspay("UNAUTHORIZED")), (422, erreur_geniuspay("VALIDATION_ERROR")),
                      requests.ConnectionError(f"{CLE_GENIUSPAY} {SECRET_GENIUSPAY}")):
            with self.subTest(panne=panne):
                self.faux.panne = panne
                with self.assertLogs("apps.paiements", "ERROR") as journaux, self.assertRaises(CommandError) as refus:
                    self.lancer("creer-webhook", "--url", URL_TUNNEL)
                for interdit in (CLE_GENIUSPAY, SECRET_GENIUSPAY):
                    self.assertNotIn(interdit, str(refus.exception) + "\n".join(journaux.output))
                self.assertEqual(self.lire_env(), ENV_INITIAL)
        self.faux.panne = None
        self.faux.appels.clear()
        self.env.unlink()
        with self.assertRaises(CommandError) as refus:
            self.lancer("creer-webhook", "--url", URL_TUNNEL)
        self.assertIn(".env introuvable", str(refus.exception))
        self.assertEqual(self.faux.appels, [])
        self.assertFalse(self.env.exists())

    # --- payer ------------------------------------------------------------------

    def test_payer_passe_par_le_vrai_checkout_et_le_vrai_service(self):
        with mock.patch.object(services, "initier_paiement", wraps=services.initier_paiement) as initier:
            sortie, paiement = self.payer_par_la_commande(500)
        initier.assert_called_once()
        (methode, chemin, corps, _), = self.faux.appels
        self.assertEqual((methode, chemin), ("POST", "/payments"))
        self.assertEqual((corps["amount"], corps["metadata"]), (500, {"reference": paiement.reference}))
        self.assertEqual(corps["customer"], {
            "name": "Client Essai", "email": "client.essai-geniuspay@essai.anitche.test", "country": "CI",
        })
        commande = paiement.commande
        self.assertEqual(
            (paiement.fournisseur, paiement.statut, paiement.montant, paiement.transaction_id_externe),
            ("geniuspay", "en_attente", Decimal("500"), "MTX-FAUX000001"),
        )
        # Commande du vrai checkout : livraison offerte par la boutique de
        # test (le montant payé est le prix de l'article), stock décrémenté.
        self.assertEqual((commande.status, commande.montant_total, commande.frais_livraison, commande.livraison_offerte),
                         ("creee", Decimal("500"), Decimal("0"), True))
        article = CommandeItem.objects.get(commande=commande)
        self.assertEqual(Stock.objects.get(variante=article.variante).quantite_disponible, 99)
        self.assertFalse(PanierItem.objects.exists())
        self.assertFalse(paiement.client.has_usable_password())
        for attendu in (paiement.reference, "MTX-FAUX000001", "https://pay.geniuspay.test/checkout/MTX-FAUX000001",
                        commande.numero_commande, "500 XOF"):
            self.assertIn(attendu, sortie)
        self.assertTrue(paiement.reference.startswith("PAY-"))
        for interdit in (CLE_GENIUSPAY, SECRET_GENIUSPAY, SECRET_WEBHOOK_GENIUSPAY):
            self.assertNotIn(interdit, sortie)

    def test_payer_reutilise_les_donnees_de_test(self):
        _, premier = self.payer_par_la_commande(500)
        comptes = set(Utilisateur.objects.values_list("email", flat=True))
        Stock.objects.update(quantite_disponible=0)
        _, second = self.payer_par_la_commande(750, "--telephone", "+2250700000009")
        self.assertEqual(set(Utilisateur.objects.values_list("email", flat=True)), comptes)
        self.assertEqual((Boutique.objects.count(), Produit.objects.count(), VarianteProduit.objects.count()), (1, 1, 1))
        self.assertNotEqual(premier.commande_id, second.commande_id)
        self.assertEqual((second.montant, second.client_id), (Decimal("750"), premier.client_id))
        self.assertEqual(self.faux.appels[-1][2]["customer"]["phone"], "+2250700000009")
        self.assertEqual(Stock.objects.get().quantite_disponible, 99)

    def test_payer_refus(self):
        with self.assertRaises(CommandError) as refus:
            self.lancer("payer", "--montant", "199")
        self.assertIn("200 FCFA", str(refus.exception))
        self.assertFalse(Utilisateur.objects.exists() or Commande.objects.exists())
        self.assertEqual(self.faux.appels, [])
        self.faux.panne = (401, erreur_geniuspay("UNAUTHORIZED"))
        with self.assertLogs("apps.paiements", "ERROR"), self.assertRaises(CommandError) as refus:
            self.lancer("payer", "--montant", "500")
        self.assertIn("Initiation refusée par GeniusPay", str(refus.exception))
        self.assertNotIn(SECRET_GENIUSPAY, str(refus.exception))
        self.assertEqual(Paiement.objects.get().statut, "echoue")

    # --- etat -------------------------------------------------------------------

    def test_etat_puis_reconciliation(self):
        _, paiement = self.payer_par_la_commande(500)
        commande = paiement.commande
        sortie = self.lancer("etat", "--reference", paiement.reference)
        self.assertIn(f"Paiement {paiement.reference} : en_attente, 500 XOF, référence GeniusPay MTX-FAUX000001", sortie)
        self.assertIn(f"Commande {commande.numero_commande} : creee", sortie)
        self.assertIn("Aucune notification journalisée", sortie)
        self.assertIn("statut en_attente, montant 500, devise XOF", sortie)
        self.assertEqual(self.verifications(), ["/payments/MTX-FAUX000001"])

        # Sans --reconcilier, la vérification ne change rien, même encaissé.
        self.encaisser(paiement)
        sortie = self.lancer("etat", "--reference", paiement.reference)
        self.assertIn("statut succes, montant 500, devise XOF", sortie)
        self.assertEqual(self.statuts(paiement, commande), ("en_attente", "creee"))

        with self.assertLogs("securite", "WARNING"):
            sortie = self.lancer("etat", "--reference", paiement.reference, "--reconcilier")
        self.assertIn("Réconciliation : issue succes, statut modifié.", sortie)
        self.assertIn(f"Paiement {paiement.reference} : valide", sortie)
        self.assertIn(f"Commande {commande.numero_commande} : confirmee", sortie)
        self.assertEqual(self.statuts(paiement, commande), ("valide", "confirmee"))

    def test_etat_montre_les_notifications_et_les_refus(self):
        _, paiement = self.payer_par_la_commande(500)
        self.encaisser(paiement)
        self.assertEqual(self.poster(self.evenement(paiement=paiement)).status_code, 200)
        sortie = self.lancer("etat", "--reference", paiement.reference)
        self.assertIn(f"Paiement {paiement.reference} : valide", sortie)
        self.assertIn("Notification payment.success : traite", sortie)
        # metadata absente du GET : refus affiché, la commande se termine normalement.
        self.encaisser(paiement, metadata=ABSENT)
        sortie = self.lancer("etat", "--reference", paiement.reference)
        self.assertIn("refusée (NotificationInvalide)", sortie)
        self.assertIn("metadata", sortie)
        with self.assertRaises(CommandError):
            self.lancer("etat", "--reference", "PAY-INCONNU")
        Paiement.objects.filter(pk=paiement.pk).update(fournisseur="simule")
        with self.assertRaises(CommandError):
            self.lancer("etat", "--reference", paiement.reference)

    # --- Journal de l'essai et réglages de dev ----------------------------------

    def test_journal_info_de_la_notification_et_de_la_verification(self):
        _, paiement = self.payer_par_la_commande(500)
        self.encaisser(paiement)
        corps = json_brut(self.evenement(paiement=paiement))
        signature = signature_geniuspay(corps, MAINTENANT_GENIUSPAY)
        with self.assertLogs("apps.paiements.fournisseurs.geniuspay", "INFO") as journaux:
            reponse = self.poster(corps, HTTP_X_WEBHOOK_EVENT="payment.success", HTTP_X_WEBHOOK_ENVIRONMENT="sandbox")
        self.assertEqual(reponse.status_code, 200)
        journal = "\n".join(journaux.output)
        for attendu in (
            f"Notification GeniusPay reçue : {len(corps)} octets, événement annoncé « payment.success », "
            "environnement annoncé « sandbox », signature hexadécimale (64 caractères).",
            "Notification GeniusPay : signature valide (encodage hex).",
            "Notification GeniusPay payment.success : champs [api_version, created_at, data, event, id, timestamp] ; "
            "data [amount, currency, customer_name, customer_phone, environment, fees,",
            "amount de type Decimal ; référence « MTX-FAUX000001 ».",
            "GeniusPay GET /payments/MTX-FAUX000001 : HTTP 200.",
            f"Vérification GeniusPay de MTX-FAUX000001 (paiement {paiement.reference}) : statut « completed », "
            "amount de type int, devise « XOF », environnement « sandbox », metadata [reference].",
        ):
            self.assertIn(attendu, journal)
        for interdit in (signature, SECRET_WEBHOOK_GENIUSPAY, CLE_GENIUSPAY, SECRET_GENIUSPAY, "Jean Kouassi",
                         "+2250748123456", "essai.anitche.test"):
            self.assertNotIn(interdit, journal)

    def test_journal_info_formes_de_signature_et_webhook_test(self):
        _, paiement = self.payer_par_la_commande(500)
        formes = {
            "A" * 43 + "=": "signature base64 probable (44 caractères).",
            "sha256=" + "a" * 64: "signature de forme inconnue (71 caractères).",
            ABSENT: "signature absente.",
        }
        for signature, attendu in formes.items():
            with self.subTest(attendu), self.assertLogs("apps.paiements", "INFO") as journaux, \
                    self.assertLogs("securite", "WARNING"):
                reponse = self.poster(self.evenement(paiement=paiement), HTTP_X_WEBHOOK_SIGNATURE=signature)
                self.assertEqual(reponse.status_code, 401)
                self.assertIn(attendu, journaux.output[0])
                self.assertNotIn("signature valide", "\n".join(journaux.output))
        essai = {"id": str(uuid.uuid4()), "event": "webhook.test", "data": {"message": "Test webhook"}}
        with self.assertLogs("apps.paiements", "INFO") as journaux:
            self.assertEqual(self.poster(essai).status_code, 200)
        self.assertIn("Notification GeniusPay webhook.test : champs [data, event, id] ; data [message] ; "
                      "amount de type NoneType", "\n".join(journaux.output))

    def test_reglages_de_dev_hotes_du_tunnel_et_journal(self):
        script = (
            "import json\n"
            "from config.settings import dev\n"
            "print(json.dumps({'hotes': dev.ALLOWED_HOSTS, 'debug': dev.DEBUG,\n"
            "                  'journal': dev.LOGGING['loggers']['apps.paiements']['level'],\n"
            "                  'racine': dev.LOGGING['root']['level']}))\n"
        )
        locaux = ["localhost", "127.0.0.1", "anitche-backend"]
        cas = {
            "": locaux,
            "essai-anitche.trycloudflare.com": [*locaux, "essai-anitche.trycloudflare.com"],
            ".trycloudflare.com, autre.exemple.test": [*locaux, ".trycloudflare.com", "autre.exemple.test"],
        }
        for valeur, attendus in cas.items():
            with self.subTest(valeur):
                env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.dev", "ALLOWED_HOSTS_DEV": valeur}
                resultat = subprocess.run([sys.executable, "-c", script], cwd=Path(settings.BASE_DIR), env=env,
                                          capture_output=True, text=True)
                self.assertEqual(resultat.returncode, 0, resultat.stderr[-500:])
                self.assertEqual(json.loads(resultat.stdout.strip().splitlines()[-1]),
                                 {"hotes": attendus, "debug": True, "journal": "INFO", "racine": "WARNING"})
        # base.py, dont héritent prod.py, ci.py et test.py : aucun journal INFO du module.
        self.assertNotIn("apps.paiements", settings.LOGGING["loggers"])
        prod = (Path(settings.BASE_DIR) / "config" / "settings" / "prod.py").read_text(encoding="utf-8")
        self.assertNotIn("ALLOWED_HOSTS_DEV", prod)


# =====================================================================
# CONFIGURATION DE PRODUCTION
# =====================================================================

class ConfigurationProductionTests(APITestCase):
    """prod.py refuse tout fournisseur hors de sa liste fermée (dont le
    simulé), une clé de sandbox ou l'absence de clés."""

    def importer_prod(self, script="import config.settings.prod", **variables):
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.prod",
            "SECRET_KEY": "x" * 50,
            "FIELD_ENCRYPTION_KEYS": Fernet.generate_key().decode(),
            "ALLOWED_HOSTS": "api.anitche.com",
            "CORS_ALLOWED_ORIGINS": "https://anitche.com",
            "BACKEND_BASE_URL": "https://api.anitche.com",
            "PAIEMENT_FOURNISSEUR": "geniuspay",
            "GENIUSPAY_API_KEY": "sk_live_cle",
            "GENIUSPAY_API_SECRET": "ss_live_cle",
            "GENIUSPAY_WEBHOOK_SECRET": "whsec_cle",
            "GENIUSPAY_ENVIRONNEMENT": "live",
            "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "EMAIL_HOST": "smtp.exemple.test",
            "EMAIL_HOST_USER": "identifiant-factice",
            "EMAIL_HOST_PASSWORD": "mot-de-passe-factice",
            "DEFAULT_FROM_EMAIL": "ANITCHE <no-reply@anitche.com>",
            "REDIS_URL": "redis://:rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr@redis:6379/0",
            "REDIS_CACHE_URL": "redis://:rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr@redis:6379/1",
            **variables,
        }
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(settings.BASE_DIR), env=env, capture_output=True, text=True,
        )

    def test_simulation_figee_a_false_dans_prod(self):
        source = (Path(settings.BASE_DIR) / "config" / "settings" / "prod.py").read_text(encoding="utf-8")
        self.assertIn("\nPAIEMENT_SIMULATION_API_ACTIVE = False\n", source)

    def test_simulation_jamais_montee_en_production(self):
        """Settings de production, même avec PAIEMENT_SIMULATION_API_ACTIVE=True
        dans l'environnement : la route n'existe pas."""
        script = (
            "import django, json\n"
            "django.setup()\n"
            "from django.conf import settings\n"
            "from django.urls import Resolver404, resolve\n"
            "try:\n"
            "    resolve('/api/paiements/simulation/PAY-2026-0000000000/')\n"
            "    montee = True\n"
            "except Resolver404:\n"
            "    montee = False\n"
            "print(json.dumps({'active': settings.PAIEMENT_SIMULATION_API_ACTIVE, 'montee': montee}))\n"
        )
        resultat = self.importer_prod(script=script, PAIEMENT_SIMULATION_API_ACTIVE="True")
        self.assertEqual(resultat.returncode, 0, resultat.stderr[-800:])
        self.assertEqual(json.loads(resultat.stdout.strip().splitlines()[-1]), {"active": False, "montee": False})

    def test_configuration_valide(self):
        cas = {
            "défaut": {},
            # Configuration de infra/.env.example : même domaine que le site.
            "même domaine": {"ALLOWED_HOSTS": "anitche.com,www.anitche.com,backend-django",
                             "BACKEND_BASE_URL": "https://anitche.com"},
            "sous-domaines": {"ALLOWED_HOSTS": ".anitche.com"},
        }
        for libelle, variables in cas.items():
            with self.subTest(libelle):
                resultat = self.importer_prod(**variables)
                self.assertEqual(resultat.returncode, 0, resultat.stderr[-500:])

    def test_refus(self):
        cas = {
            "simulé": {"PAIEMENT_FOURNISSEUR": "simule"},
            "sandbox": {"GENIUSPAY_ENVIRONNEMENT": "sandbox", "GENIUSPAY_API_KEY": "sk_sandbox_cle",
                        "GENIUSPAY_API_SECRET": "ss_sandbox_cle"},
            "sans clé": {"GENIUSPAY_API_KEY": ""},
            "http": {"BACKEND_BASE_URL": "http://api.anitche.com"},
        }
        for libelle, variables in cas.items():
            with self.subTest(libelle):
                resultat = self.importer_prod(**variables)
                self.assertNotEqual(resultat.returncode, 0)
                self.assertIn("ImproperlyConfigured", resultat.stderr)

    def test_fournisseur_en_liste_fermee(self):
        """Seuls les fournisseurs listés dans prod.py démarrent : ni le
        simulé, ni une valeur inconnue (qui passerait sinon tous les
        contrôles de clés). Les refus propres à geniuspay (clés,
        environnement, staging) : config.tests_settings_prod."""
        cas = {
            "simulé": "simule",
            "inconnu": "paydunya",
            "vide": "",
            "casse différente": "CinetPay",
            "espace": "cinetpay ",
            "cinetpay": "cinetpay",
        }
        for libelle, valeur in cas.items():
            with self.subTest(libelle):
                resultat = self.importer_prod(PAIEMENT_FOURNISSEUR=valeur)
                self.assertNotEqual(resultat.returncode, 0)
                self.assertIn("ImproperlyConfigured", resultat.stderr)
                self.assertIn("PAIEMENT_FOURNISSEUR", resultat.stderr)

    def test_fournisseur_absent_de_l_environnement_refuse(self):
        # base.py : « simule » par défaut. Sans fichier .env, comme dans le conteneur.
        script = (
            "import os, decouple\n"
            "decouple.config = decouple.Config(decouple.RepositoryEmpty())\n"
            "os.environ.pop('PAIEMENT_FOURNISSEUR')\n"
            "import config.settings.prod\n"
        )
        resultat = self.importer_prod(script=script)
        self.assertNotEqual(resultat.returncode, 0)
        self.assertIn("PAIEMENT_FOURNISSEUR", resultat.stderr)

    def test_refus_url_de_notification_injoignable(self):
        cas = {
            # Notification rejetée par Django (400 DisallowedHost).
            "hôte hors ALLOWED_HOSTS": ({"BACKEND_BASE_URL": "https://anitche.com"}, "doit figurer dans ALLOWED_HOSTS"),
            # « /x/api/... » part vers le frontend ; « //api/... » dépend de la
            # fusion des « / » par nginx.
            "« / » final": ({"BACKEND_BASE_URL": "https://api.anitche.com/"}, "sans chemin"),
            "chemin": ({"BACKEND_BASE_URL": "https://api.anitche.com/backend"}, "sans chemin"),
        }
        for libelle, (variables, message) in cas.items():
            with self.subTest(libelle):
                resultat = self.importer_prod(**variables)
                self.assertNotEqual(resultat.returncode, 0)
                self.assertIn("ImproperlyConfigured", resultat.stderr)
                self.assertIn(message, resultat.stderr)


# =====================================================================
# FRAIS DE LIVRAISON : REVERSEMENTS, RETOURS, POINTS
# =====================================================================

class FraisDeLivraisonFinancesTests(Donnees, APITestCase):
    """Produit A : 5 000 FCFA, barème de la plateforme 14 % + 200 par article
    (prix au-dessus de 3 000 FCFA) ; tarif Abidjan : 1 500. Deux articles :
    brut 10 000, net vendeur 8 200."""

    def setUp(self):
        self.creer_donnees()

    def commande_payee(self, quantite=2):
        (commande,) = self.commander((self.variante1, quantite))
        self.notifier_succes(self.payer(commande))
        return commande, Reversement.objects.get(commande=commande)

    def livrer(self, commande):
        Commande.objects.filter(pk=commande.pk).update(status=Commande.Status.EXPEDIEE)
        synchroniser_depuis_livraison(commande, "livree")
        Livraison.objects.filter(commande=commande).update(status=Livraison.Status.LIVREE, date_livraison=timezone.now())

    def demander_retour(self, commande, motif="produit_defectueux", quantite=1):
        r = self.api(self.client1).post("/api/retours/", {
            "commande_id": str(commande.pk), "motif": motif, "type_resolution": "remboursement",
            "description": "Article abîmé à l'ouverture du colis",
            "articles": [{"commande_item_id": str(commande.article.get().pk), "quantite": quantite}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return DemandeRetour.objects.get(pk=r.data["id"])

    def rembourser(self, demande):
        DemandeRetour.objects.filter(pk=demande.pk).update(statut=DemandeRetour.Statut.RECEPTIONNE)
        r = self.api(self.vendeur1).patch(f"/api/retours/{demande.pk}/traiter/", {"action": "rembourser"},
                                          format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def offrir_la_livraison(self, tarif=None):
        Boutique.objects.filter(pk=self.boutique1.pk).update(livraison_offerte=True)
        if tarif is not None:
            TarifLivraison.objects.filter(zone="abidjan").update(montant=tarif)

    # --- Reversements ---

    def test_frais_payes_par_le_client_jamais_reverses_au_vendeur(self):
        commande, reversement = self.commande_payee()
        self.assertEqual(Paiement.objects.get().montant, Decimal("10000") + FRAIS_ABIDJAN)
        self.assertEqual((reversement.montant_livraison, reversement.montant_net), (Decimal("0"), Decimal("8200")))

    def test_livraison_offerte_deduite_du_reversement(self):
        self.offrir_la_livraison()
        commande, reversement = self.commande_payee()
        self.assertEqual(Paiement.objects.get().montant, Decimal("10000"))
        self.assertEqual((reversement.montant_livraison, reversement.montant_net), (FRAIS_ABIDJAN, Decimal("6700")))
        self.livrer(commande)
        self.assertFalse(AjustementVendeur.objects.exists())  # couverte par la vente
        ligne = self.api(self.vendeur1).get("/api/paiements/vendeur/reversements/").data["results"][0]
        self.assertEqual((ligne["montant_livraison"], ligne["montant_net"]), ("1500.00", "6700.00"))

    def test_livraison_offerte_superieure_au_net_reste_du_apres_livraison(self):
        self.offrir_la_livraison(tarif=9000)
        commande, reversement = self.commande_payee()
        # Plafonnée au net (8 200) : jamais de reversement négatif.
        self.assertEqual((reversement.montant_livraison, reversement.montant_net), (Decimal("8200"), Decimal("0")))
        self.assertFalse(AjustementVendeur.objects.exists())  # rien de dû avant la livraison
        self.livrer(commande)
        reversements.ouvrir_retractation(commande)  # rejoué : idempotent
        ajustement = AjustementVendeur.objects.get()
        self.assertEqual((ajustement.nature, ajustement.montant, ajustement.commande, ajustement.boutique),
                         (AjustementVendeur.Nature.LIVRAISON_OFFERTE, Decimal("-800"), commande, self.boutique1))

    def test_livraison_offerte_annulee_avant_livraison_rien_n_est_du(self):
        self.offrir_la_livraison(tarif=9000)
        commande, reversement = self.commande_payee()
        annuler_commande(commande, Commande.MotifAnnulation.CLIENT)
        reversement.refresh_from_db()
        self.assertEqual(reversement.statut, Reversement.Statut.ANNULE)
        self.assertFalse(AjustementVendeur.objects.exists())

    def test_contestation_fondee_efface_le_reste_non_encore_deduit(self):
        self.offrir_la_livraison(tarif=9000)
        commande, _ = self.commande_payee()
        self.livrer(commande)
        self.assertTrue(AjustementVendeur.objects.exists())
        reversements.annuler_reversement(commande)  # colis déclaré non reçu, contestation fondée
        self.assertFalse(AjustementVendeur.objects.exists())

    # --- Retours ---

    def test_retour_imputable_au_vendeur_rend_les_frais_une_fois(self):
        commande, reversement = self.commande_payee()
        self.livrer(commande)
        premiere = self.demander_retour(commande)
        self.assertEqual((premiere.frais_livraison_rembourses, premiere.montant_remboursement),
                         (FRAIS_ABIDJAN, Decimal("5000") + FRAIS_ABIDJAN))
        seconde = self.demander_retour(commande, motif="non_conforme")
        self.assertEqual((seconde.frais_livraison_rembourses, seconde.montant_remboursement),
                         (Decimal("0"), Decimal("5000")))

        self.rembourser(premiere)
        self.assertEqual(Remboursement.objects.get(retour=premiere).montant, Decimal("6500"))
        reversement.refresh_from_db()
        # Part du vendeur réduite des seuls articles (5 000 − 700 de commission rendue).
        self.assertEqual(reversement.montant_retours, Decimal("4300"))
        ajustement = AjustementVendeur.objects.get(nature=AjustementVendeur.Nature.FRAIS_LIVRAISON_RETOUR)
        self.assertEqual((ajustement.montant, ajustement.commande, ajustement.boutique),
                         (-FRAIS_ABIDJAN, commande, self.boutique1))
        reversements.facturer_frais_livraison_retour(premiere)  # rejoué : idempotent
        self.assertEqual(AjustementVendeur.objects.count(), 1)
        self.assertEqual(self.api(self.client1).get(f"/api/retours/{premiere.pk}/").data["frais_livraison_rembourses"],
                         "1500.00")

    def test_changement_d_avis_ou_mauvaise_taille_sans_frais(self):
        commande, _ = self.commande_payee()
        self.livrer(commande)
        for motif in ("changement_avis", "mauvaise_taille", "autre"):
            with self.subTest(motif):
                demande = self.demander_retour(commande, motif=motif)
                self.assertEqual((demande.frais_livraison_rembourses, demande.montant_remboursement),
                                 (Decimal("0"), Decimal("5000")))
                demande.delete()

    def test_frais_rendus_si_la_premiere_demande_a_ete_rejetee(self):
        commande, _ = self.commande_payee()
        self.livrer(commande)
        premiere = self.demander_retour(commande)
        DemandeRetour.objects.filter(pk=premiere.pk).update(statut=DemandeRetour.Statut.REJETE)
        self.assertEqual(self.demander_retour(commande).frais_livraison_rembourses, FRAIS_ABIDJAN)

    def test_livraison_offerte_rien_a_rendre(self):
        self.offrir_la_livraison()
        commande, _ = self.commande_payee()
        self.livrer(commande)
        demande = self.demander_retour(commande)
        self.assertEqual((demande.frais_livraison_rembourses, demande.montant_remboursement),
                         (Decimal("0"), Decimal("5000")))

    # --- Points de fidélité ---

    def test_points_jamais_sur_les_frais(self):
        commande, _ = self.commande_payee(quantite=1)  # 5 000 + 1 500 payés
        self.livrer(commande)
        self.assertEqual(GainFidelite.objects.get(commande=commande).points, 5)

    def test_retour_avec_frais_points_recalcules_sur_les_articles(self):
        commande, _ = self.commande_payee()  # 10 000 d'articles → 10 points en attente
        self.livrer(commande)
        self.rembourser(self.demander_retour(commande))  # 5 000 d'articles + 1 500 de frais rendus
        # Reste 5 000 d'articles payés → 5 points (les frais rendus ne
        # comptent pas comme des articles remboursés : 10 000 − 6 500 → 3).
        self.assertEqual(GainFidelite.objects.get(commande=commande).points, 5)

    def test_retour_sans_frais_points_recalcules_hors_livraison(self):
        commande, _ = self.commande_payee()
        self.livrer(commande)
        self.rembourser(self.demander_retour(commande, motif="changement_avis"))
        # 10 000 − 5 000 → 5 points (et non 11 500 − 5 000 → 6 : les frais
        # payés n'ont jamais rapporté de points).
        self.assertEqual(GainFidelite.objects.get(commande=commande).points, 5)

    @override_settings(RETOUR_DELAI_JOURS=30)
    def test_points_deja_credites_reprise_sur_les_articles_seulement(self):
        commande, _ = self.commande_payee()
        self.livrer(commande)
        gain = GainFidelite.objects.get(commande=commande)
        GainFidelite.objects.filter(pk=gain.pk).update(date_disponibilite=timezone.now() - timedelta(minutes=1))
        crediter_gains_echus()
        compte = CompteFidelite.objects.get(utilisateur=self.client1)
        self.assertEqual(compte.solde_points, 10)
        self.rembourser(self.demander_retour(commande))
        compte.refresh_from_db()
        # 5 points repris pour 5 000 d'articles (6 si les frais comptaient).
        self.assertEqual(compte.solde_points, 5)
