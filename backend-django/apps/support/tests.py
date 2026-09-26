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
        """Trou corrigé : un vendeur qui ouvre son propre ticket (ex: souci
        de paiement, sans lien avec une boutique donc `vendor` reste null)
        doit continuer à le voir dans sa propre liste et pouvoir y accéder
        en détail. Avant le correctif de get_visible_tickets(), la branche
        VENDEUR ne filtrait QUE sur vendor__proprietaire=user et ce ticket
        devenait invisible pour son propre créateur."""
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
        """Corollaire du correctif : TicketMessageListCreateView s'appuie sur
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
        """Non-régression : le correctif ajoute created_by=user en OR, il ne
        doit pas retirer la visibilité déjà existante sur les litiges ouverts
        par un client contre la boutique du vendeur."""
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
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(SupportTicket.objects.filter(pk=self.ticket.id).exists())

    def test_admin_can_delete_ticket(self):
        self.client.force_authenticate(user=self.admin_user)
        url = reverse("support:ticket-detail", args=[self.ticket.id])
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(SupportTicket.objects.filter(pk=self.ticket.id).exists())


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
            contents = [m["content"] for m in response.data]
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
            contents = [m["content"] for m in response.data]
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