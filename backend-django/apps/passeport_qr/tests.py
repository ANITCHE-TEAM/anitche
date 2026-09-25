import importlib
import threading
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock, skipUnless

from django.apps import apps as registre_applications
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, connection
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient, APITestCase
from rest_framework import status
from rest_framework.throttling import SimpleRateThrottle

from apps.utilisateurs.models import Utilisateur, Role, StatutKYC
from apps.vendeurs.models import Boutique
from apps.catalogue.models import Produit, VarianteProduit
from .models import PasseportProduit, HistoriqueScanPasseport, tronquer_adresse_ip
from .serializers import (
    MESSAGE_REVOCATION_ADMINISTRATION,
    MOTIF_INDISPONIBILITE,
    VENDEUR_INDISPONIBLE,
    PasseportVendeurSerializer,
)

URL_LISTE = reverse("passeport_qr:passeport-vendeur-liste-creer")


def url_detail(passeport):
    return reverse("passeport_qr:passeport-vendeur-detail", kwargs={"pk": passeport.pk})


def url_verification(code):
    return reverse("passeport_qr:passeport-public-verification", kwargs={"code_passeport": code})


def creer_utilisateur(email, **champs):
    return Utilisateur.objects.create_user(
        email=email, password="TestPassword123!", nom="Kouassi", prenom="Jean", **champs
    )


def avec_proxys(nombre):
    return override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": nombre})


class DonneesPasseport:
    """Deux vendeurs validés, chacun avec sa boutique ; produit1 (et variante1) à vendeur1."""

    def creer_donnees(self):
        self.vendeur1 = creer_utilisateur("vendeur1@anitche.ci", role=Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        self.boutique1 = Boutique.objects.create(proprietaire=self.vendeur1, nom="Artisanat Tiassalé", est_active=True)
        self.vendeur2 = creer_utilisateur("vendeur2@anitche.ci", role=Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        self.boutique2 = Boutique.objects.create(proprietaire=self.vendeur2, nom="Atelier Wax Abidjan", est_active=True)

        self.produit1 = Produit.objects.create(
            boutique=self.boutique1, nom="Masque Baoulé Traditionnel", prix_base=Decimal("35000.00"),
        )
        self.variante1 = VarianteProduit.objects.create(
            produit=self.produit1, nom="Bois d'Iroko Authentique", prix=Decimal("35000.00"),
        )
        self.produit2 = Produit.objects.create(
            boutique=self.boutique2, nom="Sac en Raphia Bassam", prix_base=Decimal("18000.00"),
        )

    def creer_passeport(self, **champs):
        valeurs = {"produit": self.produit1, "boutique": self.boutique1, **champs}
        return PasseportProduit.objects.create(**valeurs)


class BasePasseportTestCase(DonneesPasseport, APITestCase):
    def setUp(self):
        self.creer_donnees()


class PasseportAPITestCase(BasePasseportTestCase):

    @override_settings(FRONTEND_BASE_URL="https://anitche.com")
    def test_vendeur_creer_passeport_produit(self):
        self.client.force_authenticate(user=self.vendeur1)
        data = {
            "produit_id": self.produit1.id,
            "variante_id": self.variante1.id,
            "numero_lot": "LOT-2026-08-01",
            "origine_geographique": "Tiassalé, Côte d'Ivoire",
            "materiaux_utilises": "Bois d'Iroko sculpté main, pigments végétaux naturels",
            "artisan_createur": "Maître Kouamé",
            "statut_certification": "label_local",
        }

        response = self.client.post(URL_LISTE, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertRegex(response.data["code_passeport"], r"^PAS-\d{4}-[0-9A-F]{8}$")
        self.assertEqual(response.data["artisan_createur"], "Maître Kouamé")
        self.assertEqual(
            response.data["url_verification_publique"],
            f"https://anitche.com/qr/verifier/{response.data['code_passeport']}",
        )

        # Vérification en base
        passeport = PasseportProduit.objects.get(code_passeport=response.data["code_passeport"])
        self.assertEqual(passeport.boutique, self.boutique1)
        self.assertEqual(passeport.produit, self.produit1)
        self.assertEqual(passeport.statut_certification, "label_local")
        self.assertEqual(passeport.nb_scans, 0)

    def test_rejet_creation_passeport_produit_autre_boutique(self):
        # Vendeur 2 tente de créer un passeport pour le produit de Vendeur 1
        self.client.force_authenticate(user=self.vendeur2)
        response = self.client.post(URL_LISTE, {"produit_id": self.produit1.id, "numero_lot": "LOT-HACK"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(PasseportProduit.objects.exists())

    def test_consultation_publique_incremente_scans(self):
        passeport = self.creer_passeport(
            variante=self.variante1,
            numero_lot="LOT-PUBLIC-01",
            origine_geographique="Grand-Bassam",
            materiaux_utilises="Cuir véritable",
            artisan_createur="Atelier Bassam",
        )

        # Consultation publique anonyme (sans force_authenticate)
        response = self.client.get(url_verification(passeport.code_passeport), HTTP_USER_AGENT="Mozilla/5.0 (iPhone)")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["code_passeport"], passeport.code_passeport)
        self.assertEqual(response.data["statut_passeport"], "valide")
        self.assertEqual(response.data["produit_nom"], "Masque Baoulé Traditionnel")
        self.assertEqual(response.data["produit_slug"], self.produit1.slug)
        self.assertEqual(response.data["boutique_nom"], "Artisanat Tiassalé")
        self.assertTrue(response.data["disponible_a_la_vente"])
        self.assertIsNone(response.data["motif_indisponibilite"])
        self.assertEqual(response.data["nb_scans"], 1)

        # Vérification en base de l'historique de scan
        passeport.refresh_from_db()
        self.assertEqual(passeport.nb_scans, 1)
        self.assertIsNotNone(passeport.dernier_scan)
        self.assertEqual(HistoriqueScanPasseport.objects.filter(passeport=passeport).count(), 1)

    def test_consultation_publique_code_invalide(self):
        response = self.client.get(url_verification("PAS-INVALIDE-999"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_consultation_publique_insensible_a_la_casse(self):
        passeport = self.creer_passeport()
        response = self.client.get(url_verification(passeport.code_passeport.lower()))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_liste_passeports_espace_vendeur(self):
        self.creer_passeport(numero_lot="LOT-01")

        self.client.force_authenticate(user=self.vendeur1)
        response = self.client.get(URL_LISTE)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["produit_nom"], "Masque Baoulé Traditionnel")

    def test_liste_paginee(self):
        for _ in range(25):
            self.creer_passeport()
        self.client.force_authenticate(user=self.vendeur1)
        response = self.client.get(URL_LISTE)
        self.assertEqual(response.data["count"], 25)
        self.assertEqual(len(response.data["results"]), 20)
        self.assertIsNotNone(response.data["next"])


class PasseportReassignationProduitTestCase(BasePasseportTestCase):
    """F-21 (audit sécurité) : un vendeur ne doit pas pouvoir, via PATCH,
    réassigner un passeport existant à un produit qui n'est pas le sien —
    ce qui casserait la garantie d'authenticité que ce module est censé
    apporter (le passeport resterait affiché sous SA boutique, tout en
    certifiant le produit d'un autre vendeur)."""

    def setUp(self):
        super().setUp()
        # Variante du produit de Vendeur 2, cible de la tentative de réassignation
        self.variante2 = VarianteProduit.objects.create(
            produit=self.produit2,
            nom="Naturel",
            prix=Decimal("18000.00"),
        )
        self.passeport1 = self.creer_passeport(variante=self.variante1, numero_lot="LOT-V1-01")

    def test_vendeur_ne_peut_pas_reassigner_passeport_vers_produit_dun_autre_vendeur(self):
        self.client.force_authenticate(user=self.vendeur1)
        response = self.client.patch(url_detail(self.passeport1), {"produit": self.produit2.id}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.passeport1.refresh_from_db()
        self.assertEqual(self.passeport1.produit_id, self.produit1.id)

    def test_vendeur_ne_peut_pas_assigner_variante_dun_autre_produit(self):
        self.client.force_authenticate(user=self.vendeur1)
        # variante2 appartient à produit2, pas à produit1 (le produit courant du passeport)
        response = self.client.patch(url_detail(self.passeport1), {"variante": self.variante2.id}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.passeport1.refresh_from_db()
        self.assertEqual(self.passeport1.variante_id, self.variante1.id)

    def test_vendeur_peut_toujours_corriger_vers_un_autre_produit_de_sa_propre_boutique(self):
        # Cas légitime : un produit de la MÊME boutique reste autorisé (en
        # remettant aussi la variante à zéro, puisque variante1 est liée à
        # l'ancien produit1 — changer de produit sans y toucher serait
        # justement l'incohérence que le correctif détecte à raison).
        autre_produit_meme_boutique = Produit.objects.create(
            boutique=self.boutique1,
            nom="Masque Baoulé (variante fabrication)",
            prix_base=Decimal("35000.00"),
        )
        self.client.force_authenticate(user=self.vendeur1)
        response = self.client.patch(
            url_detail(self.passeport1),
            {"produit": autre_produit_meme_boutique.id, "variante": None},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.passeport1.refresh_from_db()
        self.assertEqual(self.passeport1.produit_id, autre_produit_meme_boutique.id)


class PasseportAccesTestCase(BasePasseportTestCase):
    """Accès à l'espace vendeur : mêmes règles que ma-boutique/ (vendeurs)."""

    def setUp(self):
        super().setUp()
        self.passeport1 = self.creer_passeport(numero_lot="LOT-A")

    def test_client_refuse(self):
        client = creer_utilisateur("client@anitche.ci", role=Role.CLIENT)
        self.client.force_authenticate(client)
        self.assertEqual(self.client.get(URL_LISTE).status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.post(URL_LISTE, {"produit_id": self.produit1.id}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonyme_refuse(self):
        self.assertEqual(self.client.get(URL_LISTE).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_is_staff_ne_donne_aucun_pouvoir_metier(self):
        # Avant : `user.is_staff` suffisait pour créer sur le produit d'autrui.
        staff_client = creer_utilisateur("staff@anitche.ci", role=Role.CLIENT, is_staff=True)
        self.client.force_authenticate(staff_client)
        response = self.client.post(URL_LISTE, {"produit_id": self.produit2.id}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        Utilisateur.objects.filter(pk=self.vendeur1.pk).update(is_staff=True)
        self.vendeur1.refresh_from_db()
        self.client.force_authenticate(self.vendeur1)
        response = self.client.post(URL_LISTE, {"produit_id": self.produit2.id}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(PasseportProduit.objects.filter(produit=self.produit2).exists())

    def test_administration_admin_et_super_admin(self):
        # Avant : super_admin était refusé à la création (seul role == "admin" passait).
        for role in (Role.ADMIN, Role.SUPER_ADMIN):
            with self.subTest(role=role):
                administrateur = creer_utilisateur(f"{role}@anitche.ci", role=role)
                self.client.force_authenticate(administrateur)
                response = self.client.post(
                    URL_LISTE, {"produit_id": self.produit2.id, "numero_lot": f"LOT-{role}"}, format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                self.assertEqual(response.data["boutique"], self.boutique2.id)
                self.assertEqual(self.client.get(URL_LISTE).data["count"], PasseportProduit.objects.count())

    def test_vendeur_non_valide_refuse_lecture_comprise(self):
        Utilisateur.objects.filter(pk=self.vendeur1.pk).update(statut_kyc=StatutKYC.REFUSE)
        self.vendeur1.refresh_from_db()
        self.client.force_authenticate(self.vendeur1)
        self.assertEqual(self.client.get(URL_LISTE).status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.post(URL_LISTE, {"produit_id": self.produit1.id}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.patch(url_detail(self.passeport1), {"numero_lot": "X"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.passeport1.refresh_from_db()
        self.assertEqual(self.passeport1.numero_lot, "LOT-A")

    def test_boutique_suspendue_lecture_seule(self):
        Boutique.objects.filter(pk=self.boutique1.pk).update(est_suspendue=True)
        self.client.force_authenticate(self.vendeur1)

        self.assertEqual(self.client.get(URL_LISTE).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get(url_detail(self.passeport1)).status_code, status.HTTP_200_OK)

        response = self.client.post(URL_LISTE, {"produit_id": self.produit1.id}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("suspendue", response.data["detail"])
        for methode, donnees in (("patch", {"numero_lot": "X"}), ("put", {}), ("delete", None)):
            with self.subTest(methode=methode):
                response = getattr(self.client, methode)(url_detail(self.passeport1), donnees, format="json")
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.passeport1.refresh_from_db()
        self.assertEqual((self.passeport1.numero_lot, self.passeport1.est_actif), ("LOT-A", True))
        self.assertEqual(PasseportProduit.objects.count(), 1)

    def test_administration_peut_revoquer_passeport_dune_boutique_suspendue(self):
        Boutique.objects.filter(pk=self.boutique1.pk).update(est_suspendue=True)
        self.client.force_authenticate(creer_utilisateur("admin@anitche.ci", role=Role.ADMIN))
        self.assertEqual(self.client.delete(url_detail(self.passeport1)).status_code, status.HTTP_204_NO_CONTENT)
        self.passeport1.refresh_from_db()
        self.assertFalse(self.passeport1.est_actif)
        self.assertEqual(self.passeport1.desactive_par, "administration")

    def test_vendeur_ne_touche_pas_aux_passeports_dautrui(self):
        self.client.force_authenticate(self.vendeur2)
        for methode in ("get", "patch", "put", "delete"):
            with self.subTest(methode=methode):
                response = getattr(self.client, methode)(url_detail(self.passeport1), {}, format="json")
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(URL_LISTE).data["count"], 0)
        self.passeport1.refresh_from_db()
        self.assertTrue(self.passeport1.est_actif)


class PasseportCertificationTestCase(BasePasseportTestCase):
    """« Certifié Authentique ANITCHE » : attribué par l'administration seule."""

    CERTIFIE = PasseportProduit.StatutCertification.CERTIFIE_AUTHENTIQUE

    def test_defaut_standard(self):
        self.client.force_authenticate(self.vendeur1)
        response = self.client.post(URL_LISTE, {"produit_id": self.produit1.id}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["statut_certification"], "standard")

    def test_vendeur_ne_peut_pas_sautocertifier_a_la_creation(self):
        self.client.force_authenticate(self.vendeur1)
        response = self.client.post(
            URL_LISTE, {"produit_id": self.produit1.id, "statut_certification": self.CERTIFIE}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("statut_certification", response.data["errors"])
        self.assertFalse(PasseportProduit.objects.exists())

    def test_vendeur_ne_peut_pas_sautocertifier_en_patch(self):
        passeport = self.creer_passeport()
        self.client.force_authenticate(self.vendeur1)
        response = self.client.patch(url_detail(passeport), {"statut_certification": self.CERTIFIE}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        passeport.refresh_from_db()
        self.assertEqual(passeport.statut_certification, "standard")

    def test_administration_attribue_la_certification(self):
        self.client.force_authenticate(creer_utilisateur("admin@anitche.ci", role=Role.ADMIN))
        response = self.client.post(
            URL_LISTE, {"produit_id": self.produit1.id, "statut_certification": self.CERTIFIE}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        passeport = PasseportProduit.objects.get()
        passeport.statut_certification = "standard"
        passeport.save()
        response = self.client.patch(url_detail(passeport), {"statut_certification": self.CERTIFIE}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_vendeur_modifie_un_passeport_deja_certifie_sans_le_perdre(self):
        passeport = self.creer_passeport(statut_certification=self.CERTIFIE)
        self.client.force_authenticate(self.vendeur1)
        response = self.client.patch(
            url_detail(passeport), {"statut_certification": self.CERTIFIE, "artisan_createur": "Atelier Kouamé"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        passeport.refresh_from_db()
        self.assertEqual((passeport.statut_certification, passeport.artisan_createur), (self.CERTIFIE, "Atelier Kouamé"))


class PasseportRevocationTestCase(BasePasseportTestCase):
    def test_delete_revoque_sans_supprimer(self):
        passeport = self.creer_passeport()
        HistoriqueScanPasseport.objects.create(passeport=passeport)
        self.client.force_authenticate(self.vendeur1)

        self.assertEqual(self.client.delete(url_detail(passeport)).status_code, status.HTTP_204_NO_CONTENT)

        passeport.refresh_from_db()
        self.assertFalse(passeport.est_actif)
        self.assertEqual(passeport.desactive_par, "vendeur")
        self.assertEqual(HistoriqueScanPasseport.objects.filter(passeport=passeport).count(), 1)
        # Toujours visible (et réactivable) par son vendeur.
        self.assertEqual(self.client.get(url_detail(passeport)).data["est_actif"], False)

    def test_scan_dun_passeport_revoque_distinct_dun_faux(self):
        passeport = self.creer_passeport(numero_lot="LOT-R", artisan_createur="Atelier Bassam")
        passeport.desactiver(par=PasseportProduit.OrigineDesactivation.ADMINISTRATION)

        response = self.client.get(url_verification(passeport.code_passeport))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "code_passeport": passeport.code_passeport,
                "statut_passeport": "revoque",
                "statut_passeport_display": "Certificat révoqué",
                "disponible_a_la_vente": False,
            },
        )
        self.assertEqual(HistoriqueScanPasseport.objects.filter(passeport=passeport).count(), 1)
        self.assertEqual(self.client.get(url_verification("PAS-2026-FFFFFFFF")).status_code, status.HTTP_404_NOT_FOUND)


class PasseportReactivationTestCase(BasePasseportTestCase):
    """Qui a désactivé un passeport décide qui peut le réactiver : une
    révocation de l'administration ne se lève que par elle."""

    VENDEUR = PasseportProduit.OrigineDesactivation.VENDEUR
    ADMINISTRATION = PasseportProduit.OrigineDesactivation.ADMINISTRATION

    def setUp(self):
        super().setUp()
        self.passeport = self.creer_passeport(numero_lot="LOT-R", artisan_createur="Atelier Bassam")
        self.administrateur = creer_utilisateur("admin@anitche.ci", role=Role.ADMIN)

    def en_tant_que(self, utilisateur):
        self.client.force_authenticate(utilisateur)

    def reactiver(self, **autres_champs):
        return self.client.patch(url_detail(self.passeport), {"est_actif": True, **autres_champs}, format="json")

    def etat(self):
        self.passeport.refresh_from_db()
        return self.passeport.est_actif, self.passeport.desactive_par

    def test_vendeur_reactive_sa_propre_desactivation(self):
        self.en_tant_que(self.vendeur1)
        self.assertEqual(self.client.delete(url_detail(self.passeport)).status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.etat(), (False, self.VENDEUR))

        response = self.reactiver()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual((response.data["est_actif"], response.data["desactive_par"]), (True, ""))
        self.assertEqual(self.etat(), (True, ""))

    def test_desactivation_par_patch_enregistre_son_origine(self):
        self.en_tant_que(self.vendeur1)
        response = self.client.patch(url_detail(self.passeport), {"est_actif": False}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["desactive_par"], self.VENDEUR)
        self.assertEqual(self.etat(), (False, self.VENDEUR))

    def test_vendeur_ne_leve_pas_une_revocation_de_ladministration(self):
        self.en_tant_que(self.administrateur)
        self.assertEqual(self.client.delete(url_detail(self.passeport)).status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.etat(), (False, self.ADMINISTRATION))

        self.en_tant_que(self.vendeur1)
        response = self.reactiver()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(str(response.data["detail"]), MESSAGE_REVOCATION_ADMINISTRATION)
        self.assertEqual(self.etat(), (False, self.ADMINISTRATION))
        # Le vendeur voit pourquoi.
        self.assertEqual(self.client.get(url_detail(self.passeport)).data["desactive_par"], self.ADMINISTRATION)

    def test_reactivation_refusee_napplique_aucun_autre_champ(self):
        self.passeport.desactiver(par=self.ADMINISTRATION)
        self.en_tant_que(self.vendeur1)
        self.assertEqual(self.reactiver(artisan_createur="Autre").status_code, status.HTTP_403_FORBIDDEN)
        self.passeport.refresh_from_db()
        self.assertEqual(self.passeport.artisan_createur, "Atelier Bassam")

    def test_vendeur_ne_reprend_pas_la_main_en_redesactivant(self):
        self.passeport.desactiver(par=self.ADMINISTRATION)
        self.en_tant_que(self.vendeur1)
        self.assertEqual(self.client.delete(url_detail(self.passeport)).status_code, status.HTTP_204_NO_CONTENT)
        response = self.client.patch(url_detail(self.passeport), {"est_actif": False}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.etat(), (False, self.ADMINISTRATION))
        self.assertEqual(self.reactiver().status_code, status.HTTP_403_FORBIDDEN)

    def test_revocation_administration_simpose_a_une_desactivation_vendeur(self):
        self.passeport.desactiver(par=self.VENDEUR)
        self.en_tant_que(self.administrateur)
        self.assertEqual(self.client.delete(url_detail(self.passeport)).status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.etat(), (False, self.ADMINISTRATION))
        self.en_tant_que(self.vendeur1)
        self.assertEqual(self.reactiver().status_code, status.HTTP_403_FORBIDDEN)

    def test_administration_leve_sa_propre_revocation(self):
        self.passeport.desactiver(par=self.ADMINISTRATION)
        self.en_tant_que(self.administrateur)
        self.assertEqual(self.reactiver().status_code, status.HTTP_200_OK)
        self.assertEqual(self.etat(), (True, ""))

    def test_modifier_un_passeport_revoque_sans_le_reactiver(self):
        self.passeport.desactiver(par=self.ADMINISTRATION)
        self.en_tant_que(self.vendeur1)
        response = self.client.patch(url_detail(self.passeport), {"artisan_createur": "Autre"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.etat(), (False, self.ADMINISTRATION))

    def test_modification_concurrente_nannule_pas_une_revocation(self):
        # Le vendeur a chargé le passeport (actif) avant la révocation de
        # l'administration : sa sauvegarde ne doit pas réécrire est_actif.
        instance_perimee = PasseportProduit.objects.get(pk=self.passeport.pk)
        PasseportProduit.objects.get(pk=self.passeport.pk).desactiver(par=self.ADMINISTRATION)
        serializer = PasseportVendeurSerializer(
            instance_perimee, data={"artisan_createur": "Autre"}, partial=True,
            context={"request": SimpleNamespace(user=self.vendeur1)},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        self.assertEqual(self.etat(), (False, self.ADMINISTRATION))
        self.assertEqual(self.passeport.artisan_createur, "Autre")

    def test_origine_non_revelee_au_public(self):
        for origine in (self.VENDEUR, self.ADMINISTRATION):
            with self.subTest(origine=origine):
                self.passeport.reactiver(par=self.ADMINISTRATION)
                self.passeport.desactiver(par=origine)
                response = self.client.get(url_verification(self.passeport.code_passeport))
                self.assertEqual(response.data["statut_passeport"], "revoque")
                self.assertNotIn("desactive_par", response.data)
                self.assertNotIn(origine, response.content.decode())

    def test_contrainte_de_coherence_en_base(self):
        with self.assertRaises(IntegrityError):
            PasseportProduit.objects.filter(pk=self.passeport.pk).update(est_actif=False)


class PasseportMigrationCertificationTestCase(BasePasseportTestCase):
    def test_anciens_certifie_authentique_repasses_en_standard(self):
        migration = importlib.import_module(
            "apps.passeport_qr.migrations.0005_certifications_vendeur_en_standard"
        )
        certifie = self.creer_passeport(numero_lot="L1", statut_certification="certifie_authentique")
        label = self.creer_passeport(numero_lot="L2", statut_certification="label_local")

        migration.repasser_en_standard(registre_applications, None)

        certifie.refresh_from_db()
        label.refresh_from_db()
        self.assertEqual((certifie.statut_certification, label.statut_certification), ("standard", "label_local"))


class PasseportLotTestCase(BasePasseportTestCase):
    """Un passeport = un lot (produit, variante, numéro de lot)."""

    def creer_via_api(self, **donnees):
        return self.client.post(URL_LISTE, {"produit_id": self.produit1.id, **donnees}, format="json")

    def test_meme_lot_refuse(self):
        self.client.force_authenticate(self.vendeur1)
        self.assertEqual(self.creer_via_api(numero_lot="L1").status_code, status.HTTP_201_CREATED)
        response = self.creer_via_api(numero_lot="L1")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("numero_lot", response.data["errors"])
        self.assertEqual(PasseportProduit.objects.count(), 1)

    def test_meme_lot_autre_variante_ou_sans_lot_accepte(self):
        self.client.force_authenticate(self.vendeur1)
        self.assertEqual(self.creer_via_api(numero_lot="L1").status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            self.creer_via_api(numero_lot="L1", variante_id=self.variante1.id).status_code, status.HTTP_201_CREATED,
        )
        self.assertEqual(self.creer_via_api().status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.creer_via_api().status_code, status.HTTP_201_CREATED)

    def test_patch_vers_un_lot_existant_refuse(self):
        self.creer_passeport(numero_lot="L1")
        passeport = self.creer_passeport(numero_lot="L2")
        self.client.force_authenticate(self.vendeur1)
        response = self.client.patch(url_detail(passeport), {"numero_lot": "L1"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Renvoyer son propre lot reste accepté.
        response = self.client.patch(url_detail(passeport), {"numero_lot": "L2"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @skipUnless(connection.vendor == "postgresql", "nulls_distinct=False n'est appliqué que sous PostgreSQL")
    def test_contrainte_en_base_sans_variante(self):
        self.creer_passeport(numero_lot="L1")
        with self.assertRaises(IntegrityError):
            self.creer_passeport(numero_lot="L1")


class PasseportProduitInactifTestCase(BasePasseportTestCase):
    def test_creation_refusee_sur_produit_inactif(self):
        Produit.objects.filter(pk=self.produit1.pk).update(est_actif=False, desactive_par="vendeur")
        self.client.force_authenticate(self.vendeur1)
        response = self.client.post(URL_LISTE, {"produit_id": self.produit1.id}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("produit_id", response.data["errors"])

    def test_creation_refusee_sur_variante_inactive(self):
        VarianteProduit.objects.filter(pk=self.variante1.pk).update(est_active=False)
        self.client.force_authenticate(self.vendeur1)
        response = self.client.post(
            URL_LISTE, {"produit_id": self.produit1.id, "variante_id": self.variante1.id}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("variante_id", response.data["errors"])

    def test_etat_dun_produit_dautrui_non_revele(self):
        Produit.objects.filter(pk=self.produit2.pk).update(est_actif=False, desactive_par="vendeur")
        self.client.force_authenticate(self.vendeur1)
        response = self.client.post(URL_LISTE, {"produit_id": self.produit2.id}, format="json")
        self.assertEqual(str(response.data["errors"]["produit_id"][0]), "Ce produit n'appartient pas à votre boutique.")

    def test_patch_vers_produit_ou_variante_inactifs_refuse(self):
        passeport = self.creer_passeport()
        produit_inactif = Produit.objects.create(boutique=self.boutique1, nom="Ancien", est_actif=False, desactive_par="vendeur")
        VarianteProduit.objects.filter(pk=self.variante1.pk).update(est_active=False)
        self.client.force_authenticate(self.vendeur1)
        response = self.client.patch(url_detail(passeport), {"produit": produit_inactif.id}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.patch(url_detail(passeport), {"variante": self.variante1.id}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_produit_desactive_apres_coup_reste_modifiable(self):
        passeport = self.creer_passeport(variante=self.variante1)
        Produit.objects.filter(pk=self.produit1.pk).update(est_actif=False, desactive_par="vendeur")
        VarianteProduit.objects.filter(pk=self.variante1.pk).update(est_active=False)
        self.client.force_authenticate(self.vendeur1)
        response = self.client.patch(url_detail(passeport), {"artisan_createur": "Atelier"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class PasseportVerificationPubliqueTestCase(BasePasseportTestCase):
    """Un article qui n'est plus vendable garde son certificat, sans révéler pourquoi."""

    def setUp(self):
        super().setUp()
        self.passeport = self.creer_passeport(variante=self.variante1, numero_lot="LOT-P")

    def verifier(self):
        return self.client.get(url_verification(self.passeport.code_passeport))

    def test_article_non_vendable_certificat_affiche_boutique_masquee(self):
        cas = {
            "boutique suspendue": lambda: Boutique.objects.filter(pk=self.boutique1.pk).update(est_suspendue=True),
            "boutique fermée": lambda: Boutique.objects.filter(pk=self.boutique1.pk).update(est_active=False),
            "vendeur non validé": lambda: Utilisateur.objects.filter(pk=self.vendeur1.pk).update(statut_kyc=StatutKYC.REFUSE),
            "vendeur désactivé": lambda: Utilisateur.objects.filter(pk=self.vendeur1.pk).update(is_active=False),
            "produit inactif": lambda: Produit.objects.filter(pk=self.produit1.pk).update(est_actif=False, desactive_par="vendeur"),
            "variante inactive": lambda: VarianteProduit.objects.filter(pk=self.variante1.pk).update(est_active=False),
        }
        for libelle, rendre_non_vendable in cas.items():
            with self.subTest(libelle):
                sauvegarde = self.sauvegarder_etats()
                rendre_non_vendable()
                response = self.verifier()

                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.data["statut_passeport"], "valide")
                self.assertEqual(response.data["produit_nom"], "Masque Baoulé Traditionnel")
                self.assertEqual(response.data["numero_lot"], "LOT-P")
                self.assertEqual(response.data["boutique_nom"], VENDEUR_INDISPONIBLE)
                self.assertIsNone(response.data["produit_slug"])
                self.assertFalse(response.data["disponible_a_la_vente"])
                self.assertEqual(response.data["motif_indisponibilite"], MOTIF_INDISPONIBILITE)
                contenu = response.content.decode().lower()
                self.assertNotIn("suspend", contenu)
                self.assertNotIn("artisanat tiassal", contenu)
                self.restaurer_etats(sauvegarde)

    def sauvegarder_etats(self):
        return {
            Boutique: (self.boutique1.pk, {"est_suspendue": False, "est_active": True}),
            Utilisateur: (self.vendeur1.pk, {"statut_kyc": StatutKYC.VALIDE, "is_active": True}),
            Produit: (self.produit1.pk, {"est_actif": True, "desactive_par": ""}),
            VarianteProduit: (self.variante1.pk, {"est_active": True}),
        }

    def restaurer_etats(self, sauvegarde):
        for modele, (pk, valeurs) in sauvegarde.items():
            modele.objects.filter(pk=pk).update(**valeurs)

    def verifier_certificat_masque(self, passeport):
        response = self.client.get(url_verification(passeport.code_passeport))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["statut_passeport"], "valide")
        self.assertEqual(response.data["boutique_nom"], VENDEUR_INDISPONIBLE)
        self.assertIsNone(response.data["produit_slug"])
        self.assertFalse(response.data["disponible_a_la_vente"])
        self.assertEqual(response.data["motif_indisponibilite"], MOTIF_INDISPONIBILITE)
        self.assertNotIn("suspend", response.content.decode().lower())

    def test_passeport_sans_variante_dun_produit_sans_variante_active(self):
        # Avant : « disponible à la vente » et lien vers une fiche catalogue en 404.
        passeport = self.creer_passeport(numero_lot="LOT-SANS-VARIANTE")
        VarianteProduit.objects.filter(pk=self.variante1.pk).update(est_active=False)
        self.assertEqual(self.client.get(f"/api/catalogue/produits/{self.produit1.slug}/").status_code, 404)
        self.verifier_certificat_masque(passeport)

    def test_passeport_dun_produit_sans_aucune_variante(self):
        produit_nu = Produit.objects.create(boutique=self.boutique1, nom="Sans variante", prix_base=Decimal("5000"))
        passeport = self.creer_passeport(produit=produit_nu, numero_lot="LOT-NU")
        self.assertEqual(self.client.get(f"/api/catalogue/produits/{produit_nu.slug}/").status_code, 404)
        self.verifier_certificat_masque(passeport)

    def test_meme_regle_que_la_fiche_catalogue(self):
        # Disponible à la vente ⇔ la fiche catalogue du produit répond 200,
        # quel que soit l'état (passeport sans variante : seule la règle
        # produit s'applique).
        passeport = self.creer_passeport(numero_lot="LOT-EQUIVALENCE")
        etats = {
            "vendable": lambda: None,
            "boutique suspendue": lambda: Boutique.objects.filter(pk=self.boutique1.pk).update(est_suspendue=True),
            "boutique fermée": lambda: Boutique.objects.filter(pk=self.boutique1.pk).update(est_active=False),
            "vendeur non validé": lambda: Utilisateur.objects.filter(pk=self.vendeur1.pk).update(statut_kyc=StatutKYC.REFUSE),
            "vendeur désactivé": lambda: Utilisateur.objects.filter(pk=self.vendeur1.pk).update(is_active=False),
            "produit inactif": lambda: Produit.objects.filter(pk=self.produit1.pk).update(est_actif=False, desactive_par="vendeur"),
            "aucune variante active": lambda: VarianteProduit.objects.filter(pk=self.variante1.pk).update(est_active=False),
        }
        for libelle, appliquer in etats.items():
            with self.subTest(libelle):
                sauvegarde = self.sauvegarder_etats()
                appliquer()
                fiche = self.client.get(f"/api/catalogue/produits/{self.produit1.slug}/")
                certificat = self.client.get(url_verification(passeport.code_passeport))
                self.assertIn(fiche.status_code, (200, 404))
                self.assertEqual(certificat.data["disponible_a_la_vente"], fiche.status_code == 200)
                self.assertEqual(certificat.data["produit_slug"] is not None, fiche.status_code == 200)
                self.restaurer_etats(sauvegarde)

    def test_donnees_publiques_exposees(self):
        response = self.verifier()
        self.assertEqual(
            set(response.data),
            {
                "code_passeport", "statut_passeport", "statut_passeport_display", "produit_nom", "produit_slug",
                "boutique_nom", "variante_nom", "numero_lot", "origine_geographique", "materiaux_utilises",
                "date_fabrication", "artisan_createur", "statut_certification", "statut_certification_display",
                "nb_scans", "url_verification_publique", "disponible_a_la_vente", "motif_indisponibilite",
            },
        )
        self.assertNotIn("dernier_scan", response.data)

    @override_settings(FRONTEND_BASE_URL="https://exemple.test/")
    def test_url_de_verification_calculee_depuis_le_reglage(self):
        response = self.verifier()
        self.assertEqual(
            response.data["url_verification_publique"],
            f"https://exemple.test/qr/verifier/{self.passeport.code_passeport}",
        )

    def test_requetes_sql_bornees(self):
        # Lecture en une requête (select_related), puis UPDATE + INSERT du
        # scan (et leur savepoint) et relecture du compteur.
        with self.assertNumQueries(6):
            self.verifier()


class PasseportScanTestCase(BasePasseportTestCase):
    def setUp(self):
        super().setUp()
        self.passeport = self.creer_passeport()
        cache.clear()

    def verifier(self, **meta):
        return self.client.get(url_verification(self.passeport.code_passeport), **meta)

    def derniere_ip(self):
        return HistoriqueScanPasseport.objects.latest("date_scan").adresse_ip

    def test_x_forwarded_for_forge_ignore_sans_proxy(self):
        # Avant : l'en-tête du client était enregistré tel quel.
        response = self.verifier(REMOTE_ADDR="41.66.10.20", HTTP_X_FORWARDED_FOR="6.6.6.6")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.derniere_ip(), "41.66.10.0")

    def test_chaine_x_forwarded_for_ne_provoque_plus_de_500(self):
        # Avant : "a, b" (forme produite par un proxy) était écrit dans une colonne inet → 500.
        response = self.verifier(HTTP_X_FORWARDED_FOR="41.66.1.2, 172.70.1.1")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_derriere_le_proxy_de_confiance_ip_client_tronquee(self):
        with avec_proxys(1):
            self.assertEqual(self.verifier(HTTP_X_FORWARDED_FOR="41.66.1.2").status_code, status.HTTP_200_OK)
            self.assertEqual(self.derniere_ip(), "41.66.1.0")
            self.assertEqual(self.verifier(HTTP_X_FORWARDED_FOR="pas-une-ip").status_code, status.HTTP_200_OK)
            self.assertIsNone(self.derniere_ip())

    def test_troncature_ipv4_et_ipv6(self):
        self.assertEqual(tronquer_adresse_ip("41.66.10.20"), "41.66.10.0")
        self.assertEqual(tronquer_adresse_ip("2001:db8:abcd:1234::1"), "2001:db8:abcd::")
        self.assertIsNone(tronquer_adresse_ip("pas-une-ip"))
        self.assertIsNone(tronquer_adresse_ip(None))

    def test_compteur_et_historique_ecrits_ensemble_ou_pas_du_tout(self):
        # Avant : le compteur était incrémenté même si l'historique échouait.
        with mock.patch.object(HistoriqueScanPasseport.objects, "create", side_effect=RuntimeError("panne")):
            response = self.verifier()
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.passeport.refresh_from_db()
        self.assertEqual(self.passeport.nb_scans, 0)
        self.assertIsNone(self.passeport.dernier_scan)

    def test_limite_de_debit_dediee_non_contournable_par_x_forwarded_for(self):
        with mock.patch.object(SimpleRateThrottle, "THROTTLE_RATES", {"passeport_verification": "3/hour"}):
            statuts = [self.verifier(REMOTE_ADDR="9.9.9.9").status_code for _ in range(3)]
            self.assertEqual(statuts, [status.HTTP_200_OK] * 3)
            self.assertEqual(self.verifier(REMOTE_ADDR="9.9.9.9").status_code, status.HTTP_429_TOO_MANY_REQUESTS)
            for i in range(5):
                response = self.verifier(REMOTE_ADDR="9.9.9.9", HTTP_X_FORWARDED_FOR=f"1.1.1.{i}")
                self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
            # Une autre IP n'est pas pénalisée.
            self.assertEqual(self.verifier(REMOTE_ADDR="8.8.8.8").status_code, status.HTTP_200_OK)


class PasseportCodeTestCase(BasePasseportTestCase):
    def test_collision_de_code_retentee(self):
        existant = self.creer_passeport()
        with mock.patch(
            "apps.passeport_qr.models.generer_code_passeport",
            side_effect=[existant.code_passeport, "PAS-2026-0000ABCD"],
        ):
            self.client.force_authenticate(self.vendeur1)
            response = self.client.post(URL_LISTE, {"produit_id": self.produit1.id}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["code_passeport"], "PAS-2026-0000ABCD")

    def test_collisions_repetees_abandonnees(self):
        existant = self.creer_passeport()
        with mock.patch("apps.passeport_qr.models.generer_code_passeport", return_value=existant.code_passeport):
            with self.assertRaises(IntegrityError):
                self.creer_passeport()
        self.assertEqual(PasseportProduit.objects.count(), 1)


@skipUnless(connection.vendor == "postgresql", "Concurrence réelle : PostgreSQL uniquement")
class PasseportConcurrenceTestCase(DonneesPasseport, TransactionTestCase):
    def setUp(self):
        self.creer_donnees()

    def lancer_en_parallele(self, nb_threads, action):
        barriere = threading.Barrier(nb_threads)
        resultats = []

        def executer():
            try:
                barriere.wait()
                resultats.append(action(APIClient()))
            finally:
                connection.close()

        threads = [threading.Thread(target=executer) for _ in range(nb_threads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return resultats

    def test_scans_simultanes_aucun_increment_perdu(self):
        # Avant : 80 scans simultanés donnaient ~25 dans nb_scans.
        passeport = self.creer_passeport()
        url = url_verification(passeport.code_passeport)

        def scanner(client):
            return [client.get(url).status_code for _ in range(10)]

        resultats = self.lancer_en_parallele(8, scanner)
        self.assertEqual({code for lot in resultats for code in lot}, {status.HTTP_200_OK})
        passeport.refresh_from_db()
        self.assertEqual(passeport.nb_scans, 80)
        self.assertEqual(HistoriqueScanPasseport.objects.filter(passeport=passeport).count(), 80)

    def test_creations_simultanees_du_meme_lot_une_seule_acceptee(self):
        def creer(client):
            client.force_authenticate(self.vendeur1)
            return client.post(URL_LISTE, {"produit_id": self.produit1.id, "numero_lot": "L-COURSE"}, format="json").status_code

        resultats = self.lancer_en_parallele(6, creer)
        self.assertEqual(sorted(resultats), [status.HTTP_201_CREATED] + [status.HTTP_400_BAD_REQUEST] * 5)
        self.assertEqual(PasseportProduit.objects.filter(numero_lot="L-COURSE").count(), 1)
