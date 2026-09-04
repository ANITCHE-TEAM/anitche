from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.utilisateurs.models import DocumentKYC, Role, StatutKYC, Utilisateur

from .models import Boutique
from .services import (
    AutoApprobationInterdite,
    TransitionVendeurImpossible,
    refuser_demande_vendeur,
    valider_demande_vendeur,
)

URL_BOUTIQUES_PUBLIQUES = '/api/vendeurs/boutiques/'
URL_MA_BOUTIQUE = '/api/vendeurs/ma-boutique/'
URL_DEMANDES = '/api/vendeurs/administration/demandes/'


def creer_utilisateur(email, **champs):
    return Utilisateur.objects.create_user(
        email=email, password='MotDePasseSolide123!', nom='Kouassi', prenom='Awa', **champs
    )


def creer_vendeur_valide(email='vendeur@anitche.ci'):
    return creer_utilisateur(email, role=Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)


def creer_demandeur(email='demandeur@anitche.ci', avec_dossier=True):
    """Un client qui a soumis son KYC et attend une décision."""
    utilisateur = creer_utilisateur(email)
    if avec_dossier:
        DocumentKYC.objects.create(
            utilisateur=utilisateur,
            piece_identite='kyc/pieces_identite/cni.pdf',
            selfie='kyc/selfies/selfie.jpg',
            numero_mobile_money='0700000000',
            adresse='Cocody, Abidjan',
        )
    utilisateur.soumettre_demande_vendeur()
    return utilisateur


def creer_administrateur(email='admin@anitche.ci'):
    return creer_utilisateur(email, role=Role.ADMIN)


class BoutiqueModeleTests(TestCase):
    def test_slug_genere_et_unique(self):
        premiere = Boutique.objects.create(
            proprietaire=creer_vendeur_valide('v1@anitche.ci'), nom="Chez Awa"
        )
        seconde = Boutique.objects.create(
            proprietaire=creer_vendeur_valide('v2@anitche.ci'), nom="Chez  Awa!"
        )
        self.assertEqual(premiere.slug, 'chez-awa')
        self.assertEqual(seconde.slug, 'chez-awa-2')

    def test_est_publiable_seulement_si_vendeur_valide(self):
        boutique = Boutique.objects.create(
            proprietaire=creer_vendeur_valide(), nom="Boutique validée"
        )
        self.assertTrue(boutique.est_publiable)

        boutique.proprietaire.statut_kyc = StatutKYC.EN_ATTENTE
        boutique.proprietaire.save(update_fields=['statut_kyc'])
        self.assertFalse(boutique.est_publiable)

    def test_boutique_fermee_non_publiable(self):
        boutique = Boutique.objects.create(
            proprietaire=creer_vendeur_valide(), nom="Boutique fermée", est_active=False
        )
        self.assertFalse(boutique.est_publiable)

    def test_queryset_publiques_exclut_les_non_valides(self):
        Boutique.objects.create(proprietaire=creer_vendeur_valide('ok@anitche.ci'), nom="Visible")
        Boutique.objects.create(
            proprietaire=creer_utilisateur('attente@anitche.ci', role=Role.VENDEUR),
            nom="KYC non validé",
        )
        Boutique.objects.create(
            proprietaire=creer_vendeur_valide('ferme@anitche.ci'), nom="Fermée", est_active=False
        )

        noms = list(Boutique.objects.publiques().values_list('nom', flat=True))
        self.assertEqual(noms, ["Visible"])


class ServicesDecisionTests(TestCase):
    def test_validation_met_a_jour_role_et_statut(self):
        demandeur = creer_demandeur()

        valider_demande_vendeur(demandeur, commentaire="Dossier conforme")

        demandeur.refresh_from_db()
        self.assertEqual(demandeur.role, Role.VENDEUR)
        self.assertEqual(demandeur.statut_kyc, StatutKYC.VALIDE)

    def test_validation_trace_la_decision_dans_le_dossier_kyc(self):
        demandeur = creer_demandeur()

        valider_demande_vendeur(demandeur, commentaire="Dossier conforme")

        dossier = DocumentKYC.objects.get(utilisateur=demandeur)
        self.assertIsNotNone(dossier.date_traitement)
        self.assertEqual(dossier.commentaire_admin, "Dossier conforme")

    def test_refus_laisse_le_compte_client(self):
        demandeur = creer_demandeur()

        refuser_demande_vendeur(demandeur, commentaire="Pièce d'identité illisible")

        demandeur.refresh_from_db()
        self.assertEqual(demandeur.statut_kyc, StatutKYC.REFUSE)
        self.assertEqual(demandeur.role, Role.CLIENT)

    def test_decision_impossible_hors_attente(self):
        client = creer_utilisateur('client@anitche.ci')

        with self.assertRaises(TransitionVendeurImpossible):
            valider_demande_vendeur(client)

    def test_double_validation_refusee(self):
        demandeur = creer_demandeur()
        valider_demande_vendeur(demandeur)

        with self.assertRaises(TransitionVendeurImpossible):
            valider_demande_vendeur(demandeur)

    def test_role_non_eligible_refuse(self):
        livreur = creer_utilisateur('livreur@anitche.ci', role=Role.LIVREUR)
        livreur.soumettre_demande_vendeur()

        with self.assertRaises(TransitionVendeurImpossible):
            valider_demande_vendeur(livreur)

    def test_decideur_ne_peut_pas_traiter_sa_propre_demande(self):
        administrateur = creer_administrateur()
        administrateur.soumettre_demande_vendeur()

        with self.assertRaises(AutoApprobationInterdite):
            valider_demande_vendeur(administrateur, decideur=administrateur)

        administrateur.refresh_from_db()
        self.assertEqual(administrateur.statut_kyc, StatutKYC.EN_ATTENTE)

    def test_decideur_different_peut_traiter_la_demande(self):
        """Non-régression : un décideur tiers reste bien autorisé."""
        demandeur = creer_demandeur()
        administrateur = creer_administrateur()

        valider_demande_vendeur(demandeur, decideur=administrateur)

        demandeur.refresh_from_db()
        self.assertEqual(demandeur.statut_kyc, StatutKYC.VALIDE)

    def test_nouvelle_demande_possible_apres_refus(self):
        demandeur = creer_demandeur()
        refuser_demande_vendeur(demandeur, commentaire="Document manquant")

        demandeur.refresh_from_db()
        demandeur.soumettre_demande_vendeur()
        valider_demande_vendeur(demandeur)

        demandeur.refresh_from_db()
        self.assertEqual(demandeur.role, Role.VENDEUR)
        self.assertEqual(demandeur.statut_kyc, StatutKYC.VALIDE)


class BoutiquePubliqueAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.boutique = Boutique.objects.create(
            proprietaire=creer_vendeur_valide(), nom="Chez Awa", ville="Abidjan"
        )

    def test_liste_accessible_sans_authentification(self):
        reponse = self.client.get(URL_BOUTIQUES_PUBLIQUES)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertEqual(len(reponse.data), 1)

    def test_boutique_de_vendeur_non_valide_absente(self):
        Boutique.objects.create(
            proprietaire=creer_utilisateur('attente@anitche.ci', role=Role.VENDEUR),
            nom="Pas encore validée",
        )
        reponse = self.client.get(URL_BOUTIQUES_PUBLIQUES)
        self.assertEqual([b['nom'] for b in reponse.data], ["Chez Awa"])

    def test_detail_par_slug(self):
        reponse = self.client.get(f'/api/vendeurs/boutiques/{self.boutique.slug}/')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertEqual(reponse.data['nom'], "Chez Awa")

    def test_detail_introuvable_si_boutique_fermee(self):
        self.boutique.est_active = False
        self.boutique.save(update_fields=['est_active'])

        reponse = self.client.get(f'/api/vendeurs/boutiques/{self.boutique.slug}/')
        self.assertEqual(reponse.status_code, status.HTTP_404_NOT_FOUND)

    def test_recherche_par_ville(self):
        reponse = self.client.get(URL_BOUTIQUES_PUBLIQUES, {'ville': 'bouake'})
        self.assertEqual(len(reponse.data), 0)


class MaBoutiqueAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_creation_refusee_sans_authentification(self):
        reponse = self.client.post(URL_MA_BOUTIQUE, {'nom': "Boutique anonyme"})
        self.assertEqual(reponse.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_creation_refusee_pour_client(self):
        self.client.force_authenticate(user=creer_utilisateur('client@anitche.ci'))
        reponse = self.client.post(URL_MA_BOUTIQUE, {'nom': "Boutique interdite"})
        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Boutique.objects.exists())

    def test_creation_refusee_pour_vendeur_en_attente(self):
        demandeur = creer_demandeur()
        demandeur.role = Role.VENDEUR
        demandeur.save(update_fields=['role'])

        self.client.force_authenticate(user=demandeur)
        reponse = self.client.post(URL_MA_BOUTIQUE, {'nom': "Trop tôt"})
        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN)

    def test_creation_par_vendeur_valide(self):
        vendeur = creer_vendeur_valide()
        self.client.force_authenticate(user=vendeur)

        reponse = self.client.post(URL_MA_BOUTIQUE, {'nom': "Chez Awa", 'ville': "Abidjan"})

        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Boutique.objects.get().proprietaire, vendeur)

    def test_une_seule_boutique_par_compte(self):
        vendeur = creer_vendeur_valide()
        Boutique.objects.create(proprietaire=vendeur, nom="Première")
        self.client.force_authenticate(user=vendeur)

        reponse = self.client.post(URL_MA_BOUTIQUE, {'nom': "Seconde"})

        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Boutique.objects.count(), 1)

    def test_consultation_et_mise_a_jour_par_le_proprietaire(self):
        vendeur = creer_vendeur_valide()
        Boutique.objects.create(proprietaire=vendeur, nom="Chez Awa")
        self.client.force_authenticate(user=vendeur)

        self.assertEqual(self.client.get(URL_MA_BOUTIQUE).status_code, status.HTTP_200_OK)

        reponse = self.client.patch(URL_MA_BOUTIQUE, {'description': "Pagnes et accessoires"})
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertEqual(reponse.data['description'], "Pagnes et accessoires")

    def test_un_vendeur_ne_voit_que_sa_boutique(self):
        Boutique.objects.create(proprietaire=creer_vendeur_valide('autre@anitche.ci'), nom="Autre")
        self.client.force_authenticate(user=creer_vendeur_valide('sans@anitche.ci'))

        reponse = self.client.get(URL_MA_BOUTIQUE)

        self.assertEqual(reponse.status_code, status.HTTP_404_NOT_FOUND)

    def test_proprietaire_non_modifiable(self):
        vendeur = creer_vendeur_valide()
        autre = creer_vendeur_valide('autre@anitche.ci')
        Boutique.objects.create(proprietaire=vendeur, nom="Chez Awa")
        self.client.force_authenticate(user=vendeur)

        self.client.patch(URL_MA_BOUTIQUE, {'proprietaire': autre.id})

        self.assertEqual(Boutique.objects.get().proprietaire, vendeur)


class AdministrationDemandesAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.demandeur = creer_demandeur()
        self.administrateur = creer_administrateur()

    def test_liste_interdite_aux_non_administrateurs(self):
        self.client.force_authenticate(user=creer_vendeur_valide())
        reponse = self.client.get(URL_DEMANDES)
        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN)

    def test_liste_ne_contient_que_les_demandes_en_attente(self):
        creer_utilisateur('client@anitche.ci')
        self.client.force_authenticate(user=self.administrateur)

        reponse = self.client.get(URL_DEMANDES)

        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertEqual([d['email'] for d in reponse.data], [self.demandeur.email])

    def test_dossier_kyc_visible_dans_la_demande(self):
        self.client.force_authenticate(user=self.administrateur)
        reponse = self.client.get(URL_DEMANDES)
        self.assertEqual(reponse.data[0]['dossier_kyc']['numero_mobile_money'], '0700000000')

    def test_validation_par_administrateur(self):
        self.client.force_authenticate(user=self.administrateur)

        reponse = self.client.post(
            f'/api/vendeurs/administration/demandes/{self.demandeur.id}/valider/',
            {'commentaire': "Dossier conforme"},
        )

        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.demandeur.refresh_from_db()
        self.assertEqual(self.demandeur.role, Role.VENDEUR)
        self.assertEqual(self.demandeur.statut_kyc, StatutKYC.VALIDE)

    def test_administrateur_ne_peut_pas_valider_sa_propre_demande(self):
        """
        Séparation des responsabilités : un admin qui a lui-même soumis
        une demande vendeur ne doit jamais pouvoir l'approuver.
        """
        self.administrateur.soumettre_demande_vendeur()
        self.client.force_authenticate(user=self.administrateur)

        reponse = self.client.post(
            f'/api/vendeurs/administration/demandes/{self.administrateur.id}/valider/',
            {'commentaire': "Auto-approbation tentée"},
        )

        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN)
        self.administrateur.refresh_from_db()
        self.assertEqual(self.administrateur.statut_kyc, StatutKYC.EN_ATTENTE)
        self.assertNotEqual(self.administrateur.role, Role.VENDEUR)

    def test_administrateur_ne_peut_pas_refuser_sa_propre_demande(self):
        self.administrateur.soumettre_demande_vendeur()
        self.client.force_authenticate(user=self.administrateur)

        reponse = self.client.post(
            f'/api/vendeurs/administration/demandes/{self.administrateur.id}/refuser/',
            {'commentaire': "Auto-refus tenté, mais motivé"},
        )

        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN)
        self.administrateur.refresh_from_db()
        self.assertEqual(self.administrateur.statut_kyc, StatutKYC.EN_ATTENTE)

    def test_validation_interdite_a_un_vendeur(self):
        self.client.force_authenticate(user=creer_vendeur_valide())

        reponse = self.client.post(
            f'/api/vendeurs/administration/demandes/{self.demandeur.id}/valider/'
        )

        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN)
        self.demandeur.refresh_from_db()
        self.assertEqual(self.demandeur.statut_kyc, StatutKYC.EN_ATTENTE)

    def test_refus_exige_un_motif(self):
        self.client.force_authenticate(user=self.administrateur)

        reponse = self.client.post(
            f'/api/vendeurs/administration/demandes/{self.demandeur.id}/refuser/', {}
        )

        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.demandeur.refresh_from_db()
        self.assertEqual(self.demandeur.statut_kyc, StatutKYC.EN_ATTENTE)

    def test_refus_motive(self):
        self.client.force_authenticate(user=self.administrateur)

        reponse = self.client.post(
            f'/api/vendeurs/administration/demandes/{self.demandeur.id}/refuser/',
            {'commentaire': "Pièce d'identité illisible"},
        )

        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.demandeur.refresh_from_db()
        self.assertEqual(self.demandeur.statut_kyc, StatutKYC.REFUSE)
        self.assertEqual(self.demandeur.role, Role.CLIENT)

    def test_compte_hors_attente_introuvable_dans_la_file(self):
        client_simple = creer_utilisateur('client@anitche.ci')
        self.client.force_authenticate(user=self.administrateur)

        reponse = self.client.post(
            f'/api/vendeurs/administration/demandes/{client_simple.id}/valider/'
        )

        self.assertEqual(reponse.status_code, status.HTTP_404_NOT_FOUND)


class AdministrationBoutiquesAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.boutique = Boutique.objects.create(
            proprietaire=creer_vendeur_valide(), nom="Chez Awa", est_active=False
        )

    def test_liste_inclut_les_boutiques_fermees(self):
        self.client.force_authenticate(user=creer_administrateur())

        reponse = self.client.get('/api/vendeurs/administration/boutiques/')

        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertEqual(len(reponse.data), 1)

    def test_suspension_par_administrateur(self):
        self.boutique.est_active = True
        self.boutique.save(update_fields=['est_active'])
        self.client.force_authenticate(user=creer_administrateur())

        reponse = self.client.patch(
            f'/api/vendeurs/administration/boutiques/{self.boutique.id}/', {'est_active': False}
        )

        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.boutique.refresh_from_db()
        self.assertFalse(self.boutique.est_active)

    def test_acces_interdit_au_vendeur(self):
        self.client.force_authenticate(user=creer_vendeur_valide('autre@anitche.ci'))

        reponse = self.client.get('/api/vendeurs/administration/boutiques/')

        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN)