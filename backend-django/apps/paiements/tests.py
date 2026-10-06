import importlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet
from django.apps import apps as registre_apps
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
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

from . import reversements
from .fournisseurs.base import EN_ATTENTE, ErreurFournisseur, EtatTransaction, hacher_jeton
from .fournisseurs.cinetpay import FournisseurCinetPay
from .fournisseurs.simule import EN_TETE_SIGNATURE, FournisseurSimule, definir_etat_distant, signer
from .frais import bareme_en_vigueur, calculer_frais_ligne
from .models import AjustementVendeur, BaremeFrais, JournalWebhook, Paiement, Remboursement, Reversement
from .services import reconcilier_paiement, reconcilier_paiements_en_suspens, valider_paiement
from .signals import paiement_valide
from .tasks import reconcilier_paiements

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
        with override_settings(PAIEMENT_FOURNISSEUR="cinetpay"):
            self.assertEqual(self.simuler().status_code, 404)
        Paiement.objects.filter(pk=self.paiement.pk).update(fournisseur="cinetpay")
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
        Paiement.objects.filter(pk=self.paiement.pk).update(fournisseur="cinetpay")
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
        """Fournisseur simulé, même règle que CinetPay
        (ReconciliationReponsesCinetPayTests) : refus pour ce paiement,
        journalisé et daté ; les suivants sont vérifiés."""
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
        Paiement.objects.filter(pk=self.paiement.pk).update(fournisseur="cinetpay")
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


class ReconciliationConcurrenceTests(DonneesReconciliation, TransactionTestCase):
    """PostgreSQL : vraies transactions concurrentes (fils d'exécution)."""

    def setUp(self):
        recreer_bareme_plateforme()
        recreer_tarifs_initiaux()
        self.preparer()

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

class ReversementTests(Donnees, APITestCase):
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
    def test_retour_ouvert_suspend_puis_rejet_reprend(self):
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

    def test_transfert_echoue_reste_disponible(self):
        self.livrer(il_y_a_jours=8)
        with mock.patch.object(FournisseurSimule, "transferer", side_effect=ErreurFournisseur("solde")):
            r = self.api(self.admin).post(f"/api/paiements/admin/reversements/{self.reversement.pk}/transferer/")
        self.assertEqual(r.status_code, 409)
        self.reversement.refresh_from_db()
        self.assertEqual(self.reversement.statut, Reversement.Statut.DISPONIBLE)

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


# =====================================================================
# ADAPTATEUR CINETPAY (appels HTTP simulés, format des SDK officiels)
# =====================================================================

class ReponseHTTP:
    def __init__(self, donnees, code=200):
        self.donnees, self.status_code = donnees, code

    def json(self):
        return self.donnees


class ReponseIllisible(ReponseHTTP):
    """Corps non JSON (page HTML d'un proxy, par exemple)."""

    def json(self):
        raise ValueError("Expecting value")


class ReponsesCinetPay:
    """CinetPay en sandbox, réponses HTTP simulées : aucun appel au vrai CinetPay."""

    def configurer_cinetpay(self):
        reglages = self.settings(PAIEMENT_FOURNISSEUR="cinetpay", CINETPAY_API_KEY="sk_test_cle",
                                 CINETPAY_API_PASSWORD="mdp", CINETPAY_API_URL="")
        reglages.enable()
        self.addCleanup(reglages.disable)
        self.appels = []
        self.reponses = {}

    def repondre(self, methode, url, json=None, headers=None, timeout=None):
        self.appels.append((methode, url, json, headers))
        chemin = url.replace("https://api.cinetpay.net", "")
        reponse = self.reponses.get((methode, chemin))
        if callable(reponse):
            return reponse()
        return reponse or ReponseHTTP({"access_token": "jwt"} if chemin == "/v1/oauth/login" else {}, 200)

    def initier_cinetpay(self, commande=None, client=None, transaction_id="CP-TX-1"):
        self.reponses[("POST", "/v1/payment")] = ReponseHTTP({
            "code": 200, "status": "OK", "payment_token": "pt", "notify_token": "jeton-secret",
            "transaction_id": transaction_id, "merchant_transaction_id": "x",
            "payment_url": "https://pay.cinetpay.co/p/1",
        })
        with mock.patch("requests.request", side_effect=self.repondre):
            paiement = self.payer(commande or self.commande, client=client, methode="orange_money")
        return paiement

    def verifications(self):
        """Références vérifiées par GET /v1/payment/{référence}, dans l'ordre."""
        return [url.rsplit("/", 1)[1] for methode, url, _, _ in self.appels
                if methode == "GET" and "/v1/payment/" in url]


class CinetPayTests(ReponsesCinetPay, Donnees, APITestCase):
    def setUp(self):
        self.creer_donnees()
        (self.commande,) = self.commander((self.variante1, 3))
        self.configurer_cinetpay()

    def notification(self, paiement, jeton="jeton-secret", transaction_id="CP-TX-1"):
        return self.api().post(url_webhook("cinetpay"), {
            "notify_token": jeton, "merchant_transaction_id": paiement.reference, "transaction_id": transaction_id,
            "status": "SUCCESS",
        }, format="json")

    def test_initiation(self):
        paiement = self.initier_cinetpay()
        self.assertEqual(paiement.fournisseur, "cinetpay")
        self.assertEqual(paiement.url_paiement, "https://pay.cinetpay.co/p/1")
        self.assertEqual(paiement.hash_jeton_notification, hacher_jeton("jeton-secret"))
        (_, url, corps, entetes) = self.appels[-1]
        self.assertEqual(url, "https://api.cinetpay.net/v1/payment")
        self.assertEqual(entetes["Authorization"], "Bearer jwt")
        self.assertEqual((corps["amount"], corps["currency"], corps["payment_method"], corps["merchant_transaction_id"]),
                         (15000 + FRAIS_ABIDJAN, "XOF", "OM_CI", paiement.reference))
        self.assertEqual(corps["notify_url"], "https://api.anitche.test/api/paiements/webhook/cinetpay/")
        self.assertNotIn(b"jeton-secret", self.api(self.client1).get(f"/api/paiements/{paiement.pk}/").content)

    def test_carte_bancaire_sans_moyen_impose(self):
        self.reponses[("POST", "/v1/payment")] = ReponseHTTP({"payment_url": "https://p", "notify_token": "t",
                                                               "transaction_id": "T"})
        with mock.patch("requests.request", side_effect=self.repondre):
            self.payer(self.commande, methode="carte_bancaire")
        self.assertNotIn("payment_method", self.appels[-1][2])

    def test_notification_verifiee_aupres_de_cinetpay(self):
        paiement = self.initier_cinetpay()
        self.reponses[("GET", f"/v1/payment/{paiement.reference}")] = ReponseHTTP({
            "code": 100, "status": "SUCCESS", "merchant_transaction_id": paiement.reference, "transaction_id": "CP-TX-1",
        })
        with mock.patch("requests.request", side_effect=self.repondre):
            self.assertEqual(self.notification(paiement, jeton="mauvais").status_code, 401)
            self.assertEqual(self.notification(paiement).status_code, 200)
        paiement.refresh_from_db()
        self.assertEqual(paiement.statut, Paiement.Statut.VALIDE)
        self.assertEqual(self.appels[-1][:2], ("GET", f"https://api.cinetpay.net/v1/payment/{paiement.reference}"))

    def test_statut_de_la_notification_ignore_seule_l_api_fait_foi(self):
        paiement = self.initier_cinetpay()
        self.reponses[("GET", f"/v1/payment/{paiement.reference}")] = ReponseHTTP({
            "code": 2010, "status": "FAILED", "merchant_transaction_id": paiement.reference, "transaction_id": "CP-TX-1",
        })
        with mock.patch("requests.request", side_effect=self.repondre):
            self.assertEqual(self.notification(paiement).status_code, 200)  # la notification dit SUCCESS
        paiement.refresh_from_db()
        self.assertEqual(paiement.statut, Paiement.Statut.ECHOUE)

    def test_transaction_ou_montant_incoherents_refuses(self):
        paiement = self.initier_cinetpay()
        chemin = ("GET", f"/v1/payment/{paiement.reference}")
        self.reponses[chemin] = ReponseHTTP({"status": "SUCCESS", "merchant_transaction_id": paiement.reference,
                                             "transaction_id": "AUTRE"})
        with mock.patch("requests.request", side_effect=self.repondre):
            self.assertEqual(self.notification(paiement).status_code, 400)
            self.reponses[chemin] = ReponseHTTP({"status": "SUCCESS", "merchant_transaction_id": paiement.reference,
                                                 "transaction_id": "CP-TX-2", "amount": "100", "currency": "XOF"})
            self.assertEqual(self.notification(paiement, transaction_id="CP-TX-2").status_code, 400)
        paiement.refresh_from_db()
        self.assertEqual(paiement.statut, Paiement.Statut.EN_ATTENTE)

    def test_jeton_expire_renouvele_une_fois(self):
        paiement = self.initier_cinetpay()
        reponses = iter([ReponseHTTP({"code": 1003, "status": "EXPIRED_TOKEN"}, 401),
                         ReponseHTTP({"status": "SUCCESS", "merchant_transaction_id": paiement.reference,
                                      "transaction_id": "CP-TX-1"})])
        self.reponses[("GET", f"/v1/payment/{paiement.reference}")] = lambda: next(reponses)
        with mock.patch("requests.request", side_effect=self.repondre):
            self.assertEqual(self.notification(paiement).status_code, 200)
        self.assertEqual(sum(1 for appel in self.appels if appel[1].endswith("/v1/oauth/login")), 2)

    def test_cinetpay_injoignable(self):
        import requests

        with mock.patch("requests.request", side_effect=requests.ConnectionError()):
            r = self.initier(commande_id=str(self.commande.pk))
        self.assertEqual(r.status_code, 502)
        paiement = self.initier_cinetpay()
        with mock.patch("requests.request", side_effect=requests.Timeout()):
            self.assertEqual(self.notification(paiement).status_code, 503)

    def test_classement_des_reponses_a_la_verification(self):
        """Un 400 ou un 404 sur la vérification vise CE paiement
        (TransactionIntrouvable, qui n'est pas une ErreurFournisseur). Tout
        le reste est une panne du fournisseur, y compris un 4xx de
        l'authentification ou d'un transfert."""
        import requests

        from .fournisseurs.base import TransactionIntrouvable

        paiement = self.initier_cinetpay()
        adaptateur = FournisseurCinetPay()
        verification = ("GET", f"/v1/payment/{paiement.reference}")

        def lever(erreur):
            def reponse():
                raise erreur
            return reponse

        cas = {
            "404": (ReponseHTTP({"code": 404, "status": "TRANSACTION_NOT_FOUND"}, 404), TransactionIntrouvable),
            "400": (ReponseHTTP({"code": 400, "status": "INVALID_REQUEST"}, 400), TransactionIntrouvable),
            "500": (ReponseHTTP({"code": 500, "status": "INTERNAL_ERROR"}, 500), ErreurFournisseur),
            "503": (ReponseHTTP({"code": 503, "status": "SERVICE_UNAVAILABLE"}, 503), ErreurFournisseur),
            "429": (ReponseHTTP({"code": 429, "status": "TOO_MANY_REQUESTS"}, 429), ErreurFournisseur),
            "401 hors jeton": (ReponseHTTP({"code": 401, "status": "UNAUTHORIZED"}, 401), ErreurFournisseur),
            "403": (ReponseHTTP({"code": 403, "status": "FORBIDDEN"}, 403), ErreurFournisseur),
            "jeton refusé après renouvellement": (ReponseHTTP({"code": 1003, "status": "EXPIRED_TOKEN"}, 401),
                                                  ErreurFournisseur),
            "404 illisible": (ReponseIllisible(None, 404), ErreurFournisseur),
            "200 illisible": (ReponseIllisible(None, 200), ErreurFournisseur),
            "délai dépassé": (lever(requests.Timeout()), ErreurFournisseur),
            "connexion": (lever(requests.ConnectionError()), ErreurFournisseur),
        }
        with mock.patch("requests.request", side_effect=self.repondre):
            for libelle, (reponse, attendue) in cas.items():
                with self.subTest(libelle):
                    self.reponses[verification] = reponse
                    with self.assertRaises(attendue) as contexte:
                        adaptateur.verifier_transaction(paiement)
                    if attendue is TransactionIntrouvable:
                        self.assertNotIsInstance(contexte.exception, ErreurFournisseur)

            with self.subTest("4xx de l'authentification"):
                cache.clear()
                self.reponses[("POST", "/v1/oauth/login")] = ReponseHTTP({"code": 400, "status": "INVALID_CREDENTIALS"},
                                                                         400)
                with self.assertRaises(ErreurFournisseur):
                    adaptateur.verifier_transaction(paiement)
                del self.reponses[("POST", "/v1/oauth/login")]

            with self.subTest("404 d'un transfert"):
                self.reponses[("GET", "/v1/transfer/REV-2026-A")] = ReponseHTTP({"code": 404, "status": "NOT_FOUND"}, 404)
                with self.assertRaises(ErreurFournisseur):
                    adaptateur.verifier_transfert(Reversement(reference="REV-2026-A"))

    def test_webhook_transaction_inconnue_toujours_503(self):
        # Webhook : vérification impossible → 503, CinetPay rejoue la notification.
        paiement = self.initier_cinetpay()
        self.reponses[("GET", f"/v1/payment/{paiement.reference}")] = ReponseHTTP(
            {"code": 404, "status": "TRANSACTION_NOT_FOUND"}, 404)
        with mock.patch("requests.request", side_effect=self.repondre):
            self.assertEqual(self.notification(paiement).status_code, 503)
        self.assertEqual(JournalWebhook.objects.get().statut_traitement, JournalWebhook.StatutTraitement.ERREUR)
        paiement.refresh_from_db()
        self.assertEqual(paiement.statut, Paiement.Statut.EN_ATTENTE)

    def test_montant_hors_limites(self):
        Commande.objects.filter(pk=self.commande.pk).update(montant_total=Decimal("3000000"))
        self.assertEqual(self.initier(commande_id=str(self.commande.pk)).status_code, 400)

    def test_transfert(self):
        adaptateur = FournisseurCinetPay()
        reversement = Reversement(reference="REV-2026-A", montant_a_verser=Decimal("8400"))
        self.reponses[("POST", "/v1/transfer")] = ReponseHTTP({"code": 200, "status": "PENDING",
                                                                "transaction_id": "TR-1", "notify_token": "nt"})
        with mock.patch("requests.request", side_effect=self.repondre):
            resultat = adaptateur.transferer(reversement, "+2250707070707", "wave")
        self.assertEqual((resultat.statut, resultat.identifiant_externe), (EN_ATTENTE, "TR-1"))
        corps = self.appels[-1][2]
        self.assertEqual((corps["payment_method"], corps["amount"], corps["phone_number"]),
                         ("WAVE_CI", 8400, "+2250707070707"))


class ReconciliationReponsesCinetPayTests(ReponsesCinetPay, DonneesReconciliation, TransactionTestCase):
    """Un 4xx de CinetPay pour UN paiement P1 (transaction inconnue) est un
    refus pour P1, pas une panne du fournisseur : la réconciliation vérifie
    ensuite P2, encaissé et placé derrière. Une vraie panne (5xx, délai
    dépassé) arrête l'exécution. PostgreSQL, vraies transactions ; réponses
    HTTP simulées."""

    def setUp(self):
        recreer_bareme_plateforme()
        recreer_tarifs_initiaux()
        self.creer_donnees()
        self.configurer_cinetpay()
        (premiere,) = self.commander((self.variante1, 1))
        (seconde,) = self.commander((self.variante2, 1), client=self.client2)
        # P1 passe toujours en premier : plus petite clé de commande
        # (expiration, triée par pk) et paiement le plus ancien (tâche).
        self.c1, self.c2 = sorted((premiere, seconde), key=lambda commande: commande.pk)
        self.p1 = self.initier_cinetpay(self.c1, self.c1.client, "CP-TX-1")
        self.p2 = self.initier_cinetpay(self.c2, self.c2.client, "CP-TX-2")
        vieillir_paiement(self.p1, 20)
        vieillir_paiement(self.p2, 16)
        self.repondre_a(self.p1, ReponseHTTP({"code": 404, "status": "TRANSACTION_NOT_FOUND"}, 404))
        self.repondre_a(self.p2, ReponseHTTP({"code": 100, "status": "SUCCESS", "transaction_id": "CP-TX-2",
                                              "merchant_transaction_id": self.p2.reference}))
        self.appels.clear()

    def repondre_a(self, paiement, reponse):
        self.reponses[("GET", f"/v1/payment/{paiement.reference}")] = reponse

    def reconcilier(self):
        with mock.patch("requests.request", side_effect=self.repondre):
            return reconcilier_paiements()

    def expirer(self):
        from apps.commandes.tasks import expirer_commandes_non_payees

        with mock.patch("requests.request", side_effect=self.repondre):
            return expirer_commandes_non_payees()

    def test_404_sur_p1_n_empeche_pas_la_tache_de_valider_p2(self):
        with self.assertLogs("securite", "ERROR") as journaux:
            self.assertEqual(self.reconcilier(), "2 paiement(s) vérifié(s), 1 statut(s) changé(s).")
        self.assertEqual(self.verifications(), [self.p1.reference, self.p2.reference])
        self.assertEqual(self.statuts(self.p2, self.c2), ("valide", "confirmee"))
        self.assertEqual(self.statuts(self.p1, self.c1), ("en_attente", "creee"))
        # Daté : P1 passe ensuite après les paiements jamais vérifiés.
        self.assertIsNotNone(self.p1.date_derniere_reconciliation)
        self.assertIn(f"Réconciliation de {self.p1.reference} : transaction refusée par le fournisseur cinetpay",
                      journaux.output[0])

    def test_404_sur_p1_n_empeche_pas_l_expiration_de_valider_p2(self):
        vieillir_commandes(self.c1, self.c2, minutes=31)
        with self.assertLogs("securite", "ERROR"):
            self.assertEqual(self.expirer(), "0 commande(s) non payée(s) annulée(s).")
        self.assertEqual(self.verifications(), [self.p1.reference, self.p2.reference])
        self.assertEqual(self.statuts(self.p2, self.c2), ("valide", "confirmee"))
        # Refus pour P1 : état incertain, expiration repoussée jusqu'à la fin
        # du délai de grâce, puis faite.
        self.assertEqual(self.statuts(self.p1, self.c1), ("en_attente", "creee"))
        vieillir_commandes(self.c1, minutes=61)
        with self.assertLogs("securite", "ERROR"):
            self.assertEqual(self.expirer(), "1 commande(s) non payée(s) annulée(s).")
        self.assertEqual(self.statuts(self.p1, self.c1), ("annule", "annulee"))

    def test_5xx_ou_delai_depasse_arretent_toujours_l_execution(self):
        import requests

        def delai_depasse():
            raise requests.Timeout()

        vieillir_commandes(self.c1, self.c2, minutes=31)
        for libelle, panne in (("503", ReponseHTTP({"code": 503, "status": "SERVICE_UNAVAILABLE"}, 503)),
                               ("délai dépassé", delai_depasse)):
            with self.subTest(libelle):
                self.repondre_a(self.p1, panne)
                self.appels.clear()
                self.assertEqual(self.reconcilier(), "0 paiement(s) vérifié(s), 0 statut(s) changé(s). "
                                                     "Interrompue : fournisseur injoignable.")
                self.assertEqual(self.expirer(), "0 commande(s) non payée(s) annulée(s).")
                # Un seul appel par exécution, sur P1 : P2 n'est jamais demandé.
                self.assertEqual(self.verifications(), [self.p1.reference, self.p1.reference])
                self.assertEqual(self.statuts(self.p2, self.c2), ("en_attente", "creee"))
                self.assertEqual(self.statuts(self.p1, self.c1), ("en_attente", "creee"))
                self.assertIsNone(self.p1.date_derniere_reconciliation)


# =====================================================================
# CONFIGURATION DE PRODUCTION
# =====================================================================

class ConfigurationProductionTests(APITestCase):
    """prod.py refuse le fournisseur simulé, une clé de sandbox ou l'absence de clés."""

    def importer_prod(self, script="import config.settings.prod", **variables):
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.prod",
            "SECRET_KEY": "x" * 50,
            "FIELD_ENCRYPTION_KEYS": Fernet.generate_key().decode(),
            "ALLOWED_HOSTS": "api.anitche.com",
            "CORS_ALLOWED_ORIGINS": "https://anitche.com",
            "BACKEND_BASE_URL": "https://api.anitche.com",
            "PAIEMENT_FOURNISSEUR": "cinetpay",
            "CINETPAY_API_KEY": "sk_live_cle",
            "CINETPAY_API_PASSWORD": "mdp",
            "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "EMAIL_HOST": "smtp.exemple.test",
            "EMAIL_HOST_USER": "identifiant-factice",
            "EMAIL_HOST_PASSWORD": "mot-de-passe-factice",
            "DEFAULT_FROM_EMAIL": "ANITCHE <no-reply@anitche.com>",
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
            "sandbox": {"CINETPAY_API_KEY": "sk_test_cle"},
            "sans clé": {"CINETPAY_API_KEY": ""},
            "http": {"BACKEND_BASE_URL": "http://api.anitche.com"},
        }
        for libelle, variables in cas.items():
            with self.subTest(libelle):
                resultat = self.importer_prod(**variables)
                self.assertNotEqual(resultat.returncode, 0)
                self.assertIn("ImproperlyConfigured", resultat.stderr)

    def test_refus_url_de_notification_injoignable(self):
        cas = {
            # Notification CinetPay rejetée par Django (400 DisallowedHost).
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
