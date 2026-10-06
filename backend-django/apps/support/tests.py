from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status

from apps.utilisateurs.models import Role, StatutKYC, Utilisateur
from apps.vendeurs.models import Boutique
from .models import SupportTicket, TicketMessage, TicketAttachment
from django.core.files.uploadedfile import SimpleUploadedFile


class SupportTicketTestCase(APITestCase):

    def setUp(self):
        # Un client, un vendeur (+ sa boutique), un agent support, un admin
        self.client_user = self._create_user("client@test.com", Role.CLIENT)
        self.vendor_user = self._create_user("vendor@test.com", Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        self.support_user = self._create_user("support@test.com", Role.SUPPORT)
        self.admin_user = self._create_user("admin@test.com", Role.ADMIN)
        self.other_client = self._create_user("other@test.com", Role.CLIENT)

        self.boutique = Boutique.objects.create(
            proprietaire=self.vendor_user,
            nom="Boutique Test",
        )

        # Un ticket créé par self.client_user, lié à la boutique du vendeur
        self.ticket = SupportTicket.objects.create(
            created_by=self.client_user,
            vendor=self.boutique,
            subject="Colis non reçu",
            description="Ma commande n'est jamais arrivée.",
            category=SupportTicket.Category.DELIVERY,
        )

    def _create_user(self, email, role, statut_kyc=StatutKYC.NON_SOUMIS):
        return Utilisateur.objects.create_user(
            email=email,
            password="testpass123",
            nom="Test",
            prenom="User",
            role=role,
            statut_kyc=statut_kyc,
        )  # placeholder, remplacé ci-dessous

    # ---------- Création de ticket ----------

    def test_client_can_create_ticket(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("support:ticket-list-create")
        payload = {
            "subject": "Problème de paiement",
            "description": "Mon paiement a échoué deux fois.",
            "category": SupportTicket.Category.PAYMENT,
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # created_by doit être forcé côté serveur, jamais celui envoyé par le client
        self.assertEqual(response.data["created_by"], self.client_user.id)

    def test_client_cannot_fake_created_by(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("support:ticket-list-create")
        payload = {
            "subject": "Test usurpation",
            "description": "Tentative d'usurpation.",
            "category": SupportTicket.Category.OTHER,
            "created_by": self.other_client.id,  # tentative d'usurpation
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["created_by"], self.client_user.id)  # pas other_client

    # ---------- Visibilité par rôle ----------

    def test_client_sees_only_own_tickets(self):
        self.client.force_authenticate(user=self.other_client)
        url = reverse("support:ticket-list-create")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ticket_ids = [t["id"] for t in response.data["results"]]
        self.assertNotIn(str(self.ticket.id), ticket_ids)

    def test_vendor_sees_tickets_linked_to_their_boutique(self):
        self.client.force_authenticate(user=self.vendor_user)
        url = reverse("support:ticket-detail", args=[self.ticket.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_other_vendor_cannot_see_ticket(self):
        other_vendor = self._create_user("vendor2@test.com", Role.VENDEUR)
        self.client.force_authenticate(user=other_vendor)
        url = reverse("support:ticket-detail", args=[self.ticket.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_vendor_sees_own_ticket_without_vendor_field(self):
        """Un vendeur qui ouvre son propre ticket (ex: souci de paiement,
        sans lien avec une boutique donc `vendor` reste null) doit le voir
        dans sa propre liste et pouvoir y accéder en détail : dans
        get_visible_tickets(), la branche VENDEUR ne filtre pas QUE sur
        vendor__proprietaire=user, sinon ce ticket serait invisible pour
        son propre créateur."""
        ticket_perso = SupportTicket.objects.create(
            created_by=self.vendor_user,
            subject="Mon paiement vendeur a échoué",
            description="Je n'ai pas reçu mon virement de ce mois.",
            category=SupportTicket.Category.PAYMENT,
        )

        self.client.force_authenticate(user=self.vendor_user)

        url_liste = reverse("support:ticket-list-create")
        response_liste = self.client.get(url_liste)
        self.assertEqual(response_liste.status_code, status.HTTP_200_OK)
        ticket_ids = [t["id"] for t in response_liste.data["results"]]
        self.assertIn(str(ticket_perso.id), ticket_ids)

        url_detail = reverse("support:ticket-detail", args=[ticket_perso.id])
        response_detail = self.client.get(url_detail)
        self.assertEqual(response_detail.status_code, status.HTTP_200_OK)

    def test_vendor_can_message_on_own_ticket(self):
        """Corollaire du test précédent : TicketMessageListCreateView s'appuie sur
        get_visible_tickets(), donc un vendeur doit pouvoir répondre sur son
        propre ticket, pas seulement le consulter."""
        ticket_perso = SupportTicket.objects.create(
            created_by=self.vendor_user,
            subject="Question sur ma commission",
            description="Le taux appliqué me semble incorrect.",
            category=SupportTicket.Category.ACCOUNT,
        )
        self.client.force_authenticate(user=self.vendor_user)
        url = reverse("support:ticket-messages", args=[ticket_perso.id])
        response = self.client.post(url, {"content": "Pouvez-vous vérifier ?"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["author_role"], TicketMessage.AuthorRole.VENDOR)

    def test_vendor_still_sees_dispute_against_own_boutique(self):
        """La branche VENDEUR de get_visible_tickets() ajoute created_by=user
        en OR : elle ne retire pas la visibilité sur les litiges ouverts par
        un client contre la boutique du vendeur."""
        self.client.force_authenticate(user=self.vendor_user)
        url = reverse("support:ticket-detail", args=[self.ticket.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_admin_sees_all_tickets(self):
        self.client.force_authenticate(user=self.admin_user)
        url = reverse("support:ticket-detail", args=[self.ticket.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_unauthenticated_user_is_rejected(self):
        url = reverse("support:ticket-list-create")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    # ---------- Changement de statut ----------

    def test_owner_can_close_own_ticket(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("support:ticket-change-status", args=[self.ticket.id])
        response = self.client.patch(url, {"status": SupportTicket.Status.CLOSED})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, SupportTicket.Status.CLOSED)

    def test_owner_cannot_set_other_status(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("support:ticket-change-status", args=[self.ticket.id])
        response = self.client.patch(url, {"status": SupportTicket.Status.RESOLVED})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_set_any_status(self):
        self.client.force_authenticate(user=self.support_user)
        url = reverse("support:ticket-change-status", args=[self.ticket.id])
        response = self.client.patch(url, {"status": SupportTicket.Status.IN_PROGRESS})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # ---------- Suppression ----------

    def test_client_cannot_delete_ticket(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("support:ticket-detail", args=[self.ticket.id])
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertTrue(SupportTicket.objects.filter(pk=self.ticket.id).exists())

    def test_admin_cannot_delete_ticket_through_the_api(self):
        """Un ticket est un historique de litige ; aucune suppression par
        l'API, même pour l'administration (Django admin seulement)."""
        self.client.force_authenticate(user=self.admin_user)
        url = reverse("support:ticket-detail", args=[self.ticket.id])
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertTrue(SupportTicket.objects.filter(pk=self.ticket.id).exists())


    def test_vendeur_ne_peut_pas_modifier_le_contenu_du_ticket(self):
        """Le vendeur visé ne doit pas pouvoir réécrire la réclamation."""
        ticket = SupportTicket.objects.create(
            created_by=self.client_user,
            vendor=self.boutique,
            subject="Article contrefait",
            description="Le vendeur a envoyé un article contrefait.",
            category=SupportTicket.Category.VENDOR_DISPUTE,
        )

        self.client.force_authenticate(user=self.vendor_user)
        response = self.client.patch(
            f"/api/support/tickets/{ticket.pk}/",
            {"description": "Tout va bien, rien à signaler."},
        )

        ticket.refresh_from_db()
        self.assertEqual(ticket.description, "Le vendeur a envoyé un article contrefait.")

    def test_vendeur_ne_peut_pas_reassigner_le_ticket(self):
        """Le vendeur ne doit pas pouvoir changer la boutique visée par le ticket."""
        autre_boutique = Boutique.objects.create(
            proprietaire=self._create_user("autrevendeur@test.com", Role.VENDEUR, statut_kyc=StatutKYC.VALIDE),
            nom="Autre boutique", est_active=True,
        )
        ticket = SupportTicket.objects.create(
            created_by=self.client_user, vendor=self.boutique,
            subject="Litige", description="...", category=SupportTicket.Category.VENDOR_DISPUTE,
        )

        self.client.force_authenticate(user=self.vendor_user)
        self.client.patch(f"/api/support/tickets/{ticket.pk}/", {"vendor": str(autre_boutique.pk)})

        ticket.refresh_from_db()
        self.assertEqual(ticket.vendor_id, self.boutique.pk)

    def test_client_ne_peut_pas_modifier_categorie_ou_priorite(self):
        """category/priority sont réservés au staff, pas même au créateur."""
        ticket = SupportTicket.objects.create(
            created_by=self.client_user, vendor=self.boutique,
            subject="X", description="Y", category=SupportTicket.Category.OTHER,
        )
        self.client.force_authenticate(user=self.client_user)

        response = self.client.patch(
            f"/api/support/tickets/{ticket.pk}/",
            {"priority": "urgent"},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_peut_toujours_reprioriser(self):
        """Le staff garde la main pour trier/prioriser les tickets."""
        ticket = SupportTicket.objects.create(
            created_by=self.client_user, vendor=self.boutique,
            subject="X", description="Y", category=SupportTicket.Category.OTHER,
        )
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.patch(
            f"/api/support/tickets/{ticket.pk}/",
            {"priority": "urgent", "category": "delivery"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, "urgent")
        self.assertEqual(ticket.category, "delivery")


class TicketMessageTestCase(APITestCase):

    def setUp(self):
        self.client_user = self._create_user("client@test.com", Role.CLIENT)
        self.support_user = self._create_user("support@test.com", Role.SUPPORT)
        self.other_client = self._create_user("other@test.com", Role.CLIENT)

        self.ticket = SupportTicket.objects.create(
            created_by=self.client_user,
            subject="Test message",
            description="...",
            category=SupportTicket.Category.OTHER,
        )

    def _create_user(self, email, role):
        return Utilisateur.objects.create_user(
            email=email,
            password="testpass123",
            nom="Test",
            prenom="User",
            role=role,
        ) 

    def test_client_can_post_message_on_own_ticket(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("support:ticket-messages", args=[self.ticket.id])
        response = self.client.post(url, {"content": "Bonjour, des nouvelles ?"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["author_role"], TicketMessage.AuthorRole.CLIENT)

    def test_stranger_cannot_post_message_on_others_ticket(self):
        self.client.force_authenticate(user=self.other_client)
        url = reverse("support:ticket-messages", args=[self.ticket.id])
        response = self.client.post(url, {"content": "Tentative intrusive"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_internal_note_hidden_from_client(self):
        TicketMessage.objects.create(
            ticket_link=self.ticket,
            author=self.support_user,
            author_role=TicketMessage.AuthorRole.SUPPORT,
            content="Note interne : vérifier le tracking colis.",
            is_internal_note=True,
        )
        self.client.force_authenticate(user=self.client_user)
        url = reverse("support:ticket-messages", args=[self.ticket.id])
        response = self.client.get(url)
        contents = [m["content"] for m in response.data["results"]]
        self.assertNotIn("Note interne : vérifier le tracking colis.", contents)

    def test_internal_note_visible_to_support(self):
        TicketMessage.objects.create(
            ticket_link=self.ticket,
            author=self.support_user,
            author_role=TicketMessage.AuthorRole.SUPPORT,
            content="Note interne visible staff",
            is_internal_note=True,
        )
        self.client.force_authenticate(user=self.support_user)
        url = reverse("support:ticket-messages", args=[self.ticket.id])
        response = self.client.get(url)
        contents = [m["content"] for m in response.data["results"]]
        self.assertIn("Note interne visible staff", contents)


class TicketAttachmentTestCase(APITestCase):

    def setUp(self):
        self.client_user = self._create_user("client@test.com", Role.CLIENT)
        self.other_client = self._create_user("other@test.com", Role.CLIENT)
        self.support_user = self._create_user("support@test.com", Role.SUPPORT)

        self.ticket = SupportTicket.objects.create(
            created_by=self.client_user,
            subject="Test pièce jointe",
            description="...",
            category=SupportTicket.Category.PRODUCT,
        )

        self.message = TicketMessage.objects.create(
            ticket_link=self.ticket,
            author=self.client_user,
            author_role=TicketMessage.AuthorRole.CLIENT,
            content="Voici une photo du produit défectueux.",
        )

    def _create_user(self, email, role):
        return Utilisateur.objects.create_user(
            email=email,
            password="testpass123",
            nom="Test",
            prenom="User",
            role=role,
        )
    def _fake_file(self, name="photo.jpg"):
        ext = name.rsplit(".", 1)[-1].lower()
        entetes = {
            "jpg": b"\xff\xd8\xff\xe0\x00\x10JFIF",
            "jpeg": b"\xff\xd8\xff\xe0\x00\x10JFIF",
            "png": b"\x89PNG\r\n\x1a\n",
        }
        contenu = entetes.get(ext, b"\xff\xd8\xff") + b"contenu de test factice"
        content_type = "image/png" if ext == "png" else "image/jpeg"
        return SimpleUploadedFile(name, contenu, content_type=content_type)


    def test_owner_can_upload_attachment(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("support:message-attachments", args=[self.message.id])
        response = self.client.post(url, {
            "file": self._fake_file(),
            "file_type": TicketAttachment.FileType.IMAGE,
        }, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["original_filename"], "photo.jpg")

    def test_original_filename_and_size_are_server_computed(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("support:message-attachments", args=[self.message.id])
        fake_file = self._fake_file("evidence.png")
        response = self.client.post(url, {
            "file": fake_file,
            "file_type": TicketAttachment.FileType.IMAGE,
            "original_filename": "faux_nom.exe",  # tentative de tricher
        }, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # doit refléter le vrai nom du fichier, pas celui envoyé en trop
        self.assertEqual(response.data["original_filename"], "evidence.png")
        self.assertGreater(response.data["file_size"], 0)

    def test_stranger_cannot_upload_on_others_message(self):
        self.client.force_authenticate(user=self.other_client)
        url = reverse("support:message-attachments", args=[self.message.id])
        response = self.client.post(url, {
            "file": self._fake_file(),
            "file_type": TicketAttachment.FileType.IMAGE,
        }, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_stranger_cannot_list_attachments(self):
        TicketAttachment.objects.create(
            message=self.message,
            file=self._fake_file(),
            file_type=TicketAttachment.FileType.IMAGE,
            original_filename="secret.jpg",
            file_size=123,
        )
        self.client.force_authenticate(user=self.other_client)
        url = reverse("support:message-attachments", args=[self.message.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_owner_can_list_own_attachments(self):
        TicketAttachment.objects.create(
            message=self.message,
            file=self._fake_file(),
            file_type=TicketAttachment.FileType.IMAGE,
            original_filename="visible.jpg",
            file_size=123,
        )
        self.client.force_authenticate(user=self.client_user)
        url = reverse("support:message-attachments", args=[self.message.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)

    def test_support_can_access_attachment_on_unassigned_ticket(self):
        # ticket pas encore assigné -> file d'attente visible par le support
        self.client.force_authenticate(user=self.support_user)
        url = reverse("support:message-attachments", args=[self.message.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_unauthenticated_cannot_upload(self):
        url = reverse("support:message-attachments", args=[self.message.id])
        response = self.client.post(url, {
            "file": self._fake_file(),
        }, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


# =====================================================================
# Tickets, notes internes, pièces jointes, statuts et limites du support
# (docs/MODULE_SUPPORT.md, § Sécurité).
# =====================================================================

import io
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from PIL import Image
from rest_framework.throttling import SimpleRateThrottle

from apps.catalogue.models import Produit, VarianteProduit
from apps.commandes.models import Commande, CommandeItem
from apps.notifications.models import Notification

URL = "/api/support/"


def png(nom="capture.png"):
    tampon = io.BytesIO()
    Image.new("RGB", (4, 4)).save(tampon, "PNG")
    return SimpleUploadedFile(nom, tampon.getvalue(), content_type="image/png")


class SupportTicketsBase(APITestCase):
    def setUp(self):
        creer = Utilisateur.objects.create_user
        self.client_user = creer(email="c@support.ci", password="testpass123", nom="C", prenom="C", role=Role.CLIENT)
        self.other_client = creer(email="o@support.ci", password="testpass123", nom="O", prenom="O", role=Role.CLIENT)
        self.vendor_user = creer(email="v@support.ci", password="testpass123", nom="V", prenom="V",
                                 role=Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        self.agent = creer(email="a1@support.ci", password="testpass123", nom="A", prenom="Un", role=Role.SUPPORT)
        self.agent2 = creer(email="a2@support.ci", password="testpass123", nom="A", prenom="Deux", role=Role.SUPPORT)
        self.admin_user = creer(email="adm@support.ci", password="testpass123", nom="A", prenom="D", role=Role.ADMIN)
        self.boutique = Boutique.objects.create(proprietaire=self.vendor_user, nom="Boutique Support")
        self.produit = Produit.objects.create(boutique=self.boutique, nom="Lampe", prix_base=Decimal("5000"))
        variante = VarianteProduit.objects.create(produit=self.produit, nom="Std", prix=Decimal("5000"))
        self.order = Commande.objects.create(boutique=self.boutique, client=self.client_user,
                                             montant_total=Decimal("5000"), status=Commande.Status.LIVREE)
        CommandeItem.objects.create(commande=self.order, variante=variante, nom_produit="Lampe",
                                    prix_unitaire=Decimal("5000"), quantite=1)
        self.ticket = SupportTicket.objects.create(created_by=self.client_user, subject="Question",
                                                   description="Détail", category=SupportTicket.Category.OTHER)

    def as_user(self, user):
        self.client.force_authenticate(user=user)

    def create_ticket(self, user=None, **extra):
        self.as_user(user or self.client_user)
        corps = {"subject": "Lampe cassée", "description": "Arrivée fendue.", "category": "product", **extra}
        return self.client.post(f"{URL}tickets/", corps, format="json")

    def post_message(self, user, content="Bonjour", ticket=None, **extra):
        self.as_user(user)
        return self.client.post(f"{URL}tickets/{(ticket or self.ticket).id}/messages/", {"content": content, **extra},
                                format="json")

    def set_status(self, user, value, ticket=None):
        self.as_user(user)
        return self.client.patch(f"{URL}tickets/{(ticket or self.ticket).id}/status/", {"status": value}, format="json")


class FiltresAdministrationTests(SupportTicketsBase):
    """?non_assigne= et ?status= : file d'attente et compteurs du tableau de
    bord de l'administration ; ignorés pour les autres rôles."""

    def setUp(self):
        super().setUp()
        self.assigne = SupportTicket.objects.create(
            created_by=self.client_user, subject="Suivi", description="d", category=SupportTicket.Category.OTHER,
            assigned_to=self.agent, status=SupportTicket.Status.IN_PROGRESS,
        )
        self.ferme = SupportTicket.objects.create(
            created_by=self.other_client, subject="Fini", description="d", category=SupportTicket.Category.OTHER,
            status=SupportTicket.Status.CLOSED,
        )

    def ids(self, user, **params):
        self.as_user(user)
        r = self.client.get(f"{URL}tickets/", params)
        self.assertEqual(r.status_code, 200)
        return {ligne["id"] for ligne in r.data["results"]}

    def test_filtres_de_l_administration(self):
        tous = {str(t.pk) for t in (self.ticket, self.assigne, self.ferme)}
        self.assertEqual(self.ids(self.admin_user), tous)
        self.assertEqual(self.ids(self.admin_user, non_assigne="1"), {str(self.ticket.pk), str(self.ferme.pk)})
        self.assertEqual(self.ids(self.admin_user, non_assigne="true", status="open"), {str(self.ticket.pk)})
        self.assertEqual(self.ids(self.admin_user, status="in_progress"), {str(self.assigne.pk)})
        self.as_user(self.admin_user)
        self.assertEqual(self.client.get(f"{URL}tickets/", {"non_assigne": "1", "status": "open"}).data["count"], 1)
        self.assertEqual(self.ids(self.admin_user, non_assigne="0", status="inconnu"), tous)

    def test_ignores_pour_les_autres_roles(self):
        # Un client garde ses tickets, un agent sa liste : les filtres n'y changent rien.
        self.assertEqual(self.ids(self.client_user, status="closed", non_assigne="1"),
                         self.ids(self.client_user))
        self.assertEqual(self.ids(self.agent, non_assigne="1"), self.ids(self.agent))


class TestsCollectesTests(SupportTicketsBase):
    """La classe de tests des messages est au niveau du module : imbriquée
    dans une autre classe, elle ne serait jamais exécutée."""

    def test_classe_des_messages_au_niveau_du_module(self):
        import apps.support.tests as module
        self.assertTrue(hasattr(module, "TicketMessageTestCase"))
        self.assertFalse(hasattr(module.SupportTicketTestCase, "TicketMessageTestCase"))


class TicketLieACommandeTests(SupportTicketsBase):
    """Un ticket vise SA commande ou SON produit acheté."""

    def test_lien_commande_et_produit_achete(self):
        r = self.create_ticket(order=str(self.order.id), product=self.produit.id)
        self.assertEqual(r.status_code, 201, r.data)
        ticket = SupportTicket.objects.get(pk=r.data["id"])
        self.assertEqual((ticket.order_id, ticket.product_id, ticket.vendor_id),
                         (self.order.id, self.produit.id, self.boutique.id))

    def test_commande_d_un_autre_client_refusee(self):
        r = self.create_ticket(user=self.other_client, order=str(self.order.id))
        self.assertEqual(r.status_code, 400)
        self.assertIn("introuvable", str(r.data))

    def test_produit_non_achete_refuse(self):
        self.assertEqual(self.create_ticket(user=self.other_client, product=self.produit.id).status_code, 400)

    def test_produit_hors_de_la_commande_citee_refuse(self):
        autre = Commande.objects.create(boutique=self.boutique, client=self.client_user, montant_total=Decimal("1"))
        self.assertEqual(self.create_ticket(order=str(autre.id), product=self.produit.id).status_code, 400)

    def test_boutique_hors_du_ticket_pour_une_question_de_paiement(self):
        r = self.create_ticket(order=str(self.order.id), category="payment")
        self.assertIsNone(SupportTicket.objects.get(pk=r.data["id"]).vendor_id)
        self.as_user(self.vendor_user)
        self.assertEqual(self.client.get(f"{URL}tickets/{r.data['id']}/").status_code, 404)

    def test_boutique_voit_le_litige_produit(self):
        r = self.create_ticket(product=self.produit.id)
        self.as_user(self.vendor_user)
        self.assertEqual(self.client.get(f"{URL}tickets/{r.data['id']}/").status_code, 200)

    def test_commande_et_produit_immuables(self):
        r = self.create_ticket(order=str(self.order.id))
        self.as_user(self.admin_user)
        autre = Commande.objects.create(boutique=self.boutique, client=self.client_user, montant_total=Decimal("1"))
        self.client.patch(f"{URL}tickets/{r.data['id']}/", {"order": str(autre.id), "priority": "high"}, format="json")
        ticket = SupportTicket.objects.get(pk=r.data["id"])
        self.assertEqual((ticket.order_id, ticket.priority), (self.order.id, "high"))


class NotesInternesTests(SupportTicketsBase):
    """Le staff écrit des notes internes, jamais le client."""

    def test_staff_cree_une_note_interne_invisible_du_client(self):
        r = self.post_message(self.agent, "Vérifier le transporteur", is_internal_note=True)
        self.assertEqual(r.status_code, 201)
        self.assertTrue(TicketMessage.objects.get(pk=r.data["id"]).is_internal_note)
        self.as_user(self.client_user)
        ids = [m["id"] for m in self.client.get(f"{URL}tickets/{self.ticket.id}/messages/").data["results"]]
        self.assertNotIn(r.data["id"], ids)

    def test_client_ne_cree_pas_de_note_interne(self):
        r = self.post_message(self.client_user, "Je cache ceci", is_internal_note=True)
        self.assertFalse(TicketMessage.objects.get(pk=r.data["id"]).is_internal_note)


class PiecesJointesTests(SupportTicketsBase):
    """Pièces jointes sur ses propres messages, jamais sur une
    note interne, type calculé, nom UUID, 5 au plus, téléchargement contrôlé."""

    def setUp(self):
        super().setUp()
        self.message = TicketMessage.objects.create(ticket_link=self.ticket, author=self.client_user,
                                                    author_role="client", content="Voici la photo")
        self.note = TicketMessage.objects.create(ticket_link=self.ticket, author=self.agent, author_role="support",
                                                 content="interne", is_internal_note=True)

    def upload(self, user, message=None, fichier=None, **extra):
        self.as_user(user)
        return self.client.post(f"{URL}messages/{(message or self.message).id}/attachments/",
                                {"file": fichier or png(), **extra}, format="multipart")

    def test_note_interne_invisible_et_fermee_au_client(self):
        self.as_user(self.client_user)
        self.assertEqual(self.client.get(f"{URL}messages/{self.note.id}/attachments/").status_code, 404)
        self.assertEqual(self.upload(self.client_user, message=self.note).status_code, 404)

    def test_pas_de_piece_jointe_sur_le_message_d_un_autre(self):
        reponse = TicketMessage.objects.create(ticket_link=self.ticket, author=self.agent, author_role="support",
                                               content="Réponse")
        self.assertEqual(self.upload(self.client_user, message=reponse).status_code, 403)

    def test_type_calcule_et_nom_uuid(self):
        r = self.upload(self.client_user, fichier=png("Aya_Konan_facture.png"), file_type="other")
        self.assertEqual(r.status_code, 201, r.data)
        piece = TicketAttachment.objects.get(pk=r.data["id"])
        self.assertEqual((piece.file_type, piece.original_filename), ("image", "Aya_Konan_facture.png"))
        self.assertNotIn("Aya_Konan", piece.file.name)
        self.assertTrue(piece.file.name.startswith("support/pieces_jointes/"))

    def test_cinq_pieces_au_plus(self):
        for _ in range(5):
            self.assertEqual(self.upload(self.client_user).status_code, 201)
        self.assertEqual(self.upload(self.client_user).status_code, 400)

    def test_faux_fichier_refuse(self):
        faux = SimpleUploadedFile("facture.pdf", b"MZ\x90\x00 executable", content_type="application/pdf")
        self.assertEqual(self.upload(self.client_user, fichier=faux).status_code, 400)

    def test_ticket_ferme(self):
        SupportTicket.objects.filter(pk=self.ticket.pk).update(status="closed")
        self.assertEqual(self.upload(self.client_user).status_code, 400)

    def test_telechargement_controle(self):
        r = self.upload(self.client_user)
        url = r.data["file"]
        self.assertIn(f"/api/support/attachments/{r.data['id']}/", url)
        for user, attendu in ((self.client_user, 200), (self.agent, 200), (self.admin_user, 200),
                              (self.other_client, 404), (self.vendor_user, 404)):
            with self.subTest(user=user.email):
                self.as_user(user)
                self.assertEqual(self.client.get(url).status_code, attendu)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_type_de_contenu_reel_documente_dans_le_schema(self):
        from config.schema import FICHIER_DOCUMENT

        (cle,) = FICHIER_DOCUMENT
        pdf = SimpleUploadedFile("facture.pdf", b"%PDF-1.4\nfacture", content_type="application/pdf")
        for attendu, fichier in (("image/png", png()), ("application/pdf", pdf)):
            with self.subTest(attendu=attendu):
                r = self.upload(self.client_user, fichier=fichier)
                self.assertEqual(r.status_code, 201, r.data)
                reponse = self.client.get(r.data["file"])
                self.assertEqual(reponse["Content-Type"], attendu)
                self.assertIn(reponse["Content-Type"], cle[1:])

    def test_piece_d_une_note_interne_jamais_telechargee_par_le_client(self):
        piece = TicketAttachment.objects.create(message=self.note, file=png(), original_filename="n.png", file_size=10)
        self.as_user(self.client_user)
        self.assertEqual(self.client.get(f"{URL}attachments/{piece.id}/").status_code, 404)


class StatutsEtAssignationTests(SupportTicketsBase):
    """Table de transitions, prise en charge, réassignation."""

    def test_ferme_est_definitif(self):
        self.assertEqual(self.set_status(self.agent, "closed").status_code, 200)
        self.assertEqual(self.set_status(self.admin_user, "open").status_code, 400)
        self.assertEqual(self.set_status(self.admin_user, "in_progress").status_code, 400)

    def test_retour_a_open_impossible(self):
        self.set_status(self.agent, "in_progress")
        self.assertEqual(self.set_status(self.agent, "open").status_code, 400)

    def test_l_agent_qui_agit_prend_le_ticket(self):
        self.set_status(self.agent, "in_progress")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.assigned_to, self.agent)
        # Le second agent ne le voit plus.
        self.assertEqual(self.set_status(self.agent2, "resolved").status_code, 404)

    def test_prise_en_charge_explicite(self):
        self.as_user(self.agent)
        self.assertEqual(self.client.post(f"{URL}tickets/{self.ticket.id}/assign/").status_code, 200)
        self.as_user(self.agent2)
        self.assertEqual(self.client.post(f"{URL}tickets/{self.ticket.id}/assign/").status_code, 404)

    def test_agent_ne_reassigne_pas_a_un_autre(self):
        self.as_user(self.agent)
        r = self.client.post(f"{URL}tickets/{self.ticket.id}/assign/", {"assigned_to": self.agent2.pk}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_administration_reassigne(self):
        self.as_user(self.admin_user)
        r = self.client.post(f"{URL}tickets/{self.ticket.id}/assign/", {"assigned_to": self.agent2.pk}, format="json")
        self.assertEqual((r.status_code, r.data["assigned_to"]), (200, self.agent2.pk))
        r = self.client.post(f"{URL}tickets/{self.ticket.id}/assign/", {"assigned_to": self.client_user.pk}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_client_n_assigne_pas(self):
        self.as_user(self.client_user)
        self.assertEqual(self.client.post(f"{URL}tickets/{self.ticket.id}/assign/").status_code, 403)

    def test_note_seulement_une_fois_resolu(self):
        self.as_user(self.client_user)
        url = f"{URL}tickets/{self.ticket.id}/rate/"
        self.assertEqual(self.client.patch(url, {"satisfaction_rating": 5}, format="json").status_code, 400)
        self.set_status(self.agent, "resolved")
        self.as_user(self.client_user)
        self.assertEqual(self.client.patch(url, {"satisfaction_rating": 5}, format="json").status_code, 200)


class FilDeDiscussionTests(SupportTicketsBase):
    """Ticket fermé, statut automatique, lecture, notifications."""

    def test_message_sur_ticket_ferme_refuse(self):
        SupportTicket.objects.filter(pk=self.ticket.pk).update(status="closed")
        self.assertEqual(self.post_message(self.client_user).status_code, 400)

    def test_reponse_du_client_relance_le_ticket(self):
        self.set_status(self.agent, "waiting_customer")
        self.post_message(self.client_user, "Voici le numéro de suivi")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, "in_progress")

    def test_premiere_reponse_du_support(self):
        self.post_message(self.agent, "Nous regardons.")
        self.ticket.refresh_from_db()
        self.assertEqual((self.ticket.status, self.ticket.assigned_to), ("in_progress", self.agent))
        self.assertTrue(Notification.objects.filter(destinataire=self.client_user, type_notification="support").exists())

    def test_reponse_du_client_notifie_l_agent(self):
        self.post_message(self.agent, "Nous regardons.")
        self.post_message(self.client_user, "Merci")
        self.assertTrue(Notification.objects.filter(destinataire=self.agent, type_notification="support").exists())

    def test_nouveau_ticket_annonce_a_l_equipe(self):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.create_ticket(category="other")
        for agent in (self.agent, self.agent2):
            self.assertTrue(Notification.objects.filter(destinataire=agent, metadata__ticket_id=r.data["id"]).exists())

    def test_messages_marques_lus_par_l_autre_partie(self):
        message = self.post_message(self.agent, "Réponse").data
        self.as_user(self.client_user)
        self.client.get(f"{URL}tickets/{self.ticket.id}/messages/")
        self.assertIsNotNone(TicketMessage.objects.get(pk=message["id"]).read_at)


class LimitesDeDebitTests(SupportTicketsBase):
    """Limites dédiées (vraies valeurs de base.py)."""

    def test_creation_de_tickets_limitee(self):
        from apps.core.tests import taux_de_production

        cache.clear()
        taux = taux_de_production()
        n = int(taux["support_ticket"].split("/")[0])
        with patch.object(SimpleRateThrottle, "THROTTLE_RATES", taux):
            codes = [self.create_ticket(category="other").status_code for _ in range(n + 1)]
            self.assertEqual(self.client.get(f"{URL}tickets/").status_code, 200)
        self.assertEqual(codes, [201] * n + [429])
