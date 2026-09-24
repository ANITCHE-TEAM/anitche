from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, TransactionTestCase
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from apps.utilisateurs.models import DocumentKYC, Role, StatutKYC, TypePieceIdentite, Utilisateur

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
            type_piece=TypePieceIdentite.CNI,
            piece_identite_recto='kyc/pieces_identite/cni.pdf',
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

    def test_boutique_suspendue_non_publiable_et_hors_publiques(self):
        boutique = Boutique.objects.create(
            proprietaire=creer_vendeur_valide(), nom="Suspendue", est_suspendue=True
        )
        self.assertFalse(boutique.est_publiable)
        self.assertNotIn(boutique, Boutique.objects.publiques())

    def test_creation_directe_refusee_pour_vendeur_non_valide(self):
        """A04:2025 — défense en profondeur : même un appel ORM direct
        (shell, script, tâche Celery), hors de toute vue/permission API,
        doit être bloqué si le propriétaire n'est pas un vendeur validé."""
        from django.core.exceptions import ValidationError

        vendeur_non_valide = creer_utilisateur('non-valide@anitche.ci', role=Role.VENDEUR)
        with self.assertRaises(ValidationError):
            Boutique.objects.create(proprietaire=vendeur_non_valide, nom="Ne doit pas exister")

    def test_queryset_publiques_exclut_les_non_valides(self):
        Boutique.objects.create(proprietaire=creer_vendeur_valide('ok@anitche.ci'), nom="Visible")

        # La boutique doit être créée pour un vendeur déjà validé (sinon
        # full_clean() la refuse dès la création, voir Boutique.save()),
        # puis on fait redescendre son KYC après coup pour obtenir le même
        # état final que l'ancien scénario ("KYC non validé").
        vendeur_en_attente = creer_vendeur_valide('attente@anitche.ci')
        boutique_en_attente = Boutique.objects.create(
            proprietaire=vendeur_en_attente, nom="KYC non validé",
        )
        vendeur_en_attente.statut_kyc = StatutKYC.EN_ATTENTE
        vendeur_en_attente.save(update_fields=['statut_kyc'])

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
        self.assertEqual(len(reponse.data["results"]), 1)

    def test_boutique_de_vendeur_non_valide_absente(self):
        vendeur_en_attente = creer_vendeur_valide('attente@anitche.ci')
        Boutique.objects.create(
            proprietaire=vendeur_en_attente,
            nom="Pas encore validée",
        )
        vendeur_en_attente.statut_kyc = StatutKYC.EN_ATTENTE
        vendeur_en_attente.save(update_fields=['statut_kyc'])

        reponse = self.client.get(URL_BOUTIQUES_PUBLIQUES)
        self.assertEqual([b['nom'] for b in reponse.data["results"]], ["Chez Awa"])

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
        self.assertEqual(len(reponse.data["results"]), 0)


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

    def test_deux_vendeurs_meme_nom_de_boutique_pas_de_500(self):
        """
        Boutique.nom est unique globalement. Avant le correctif, une
        collision de nom entre deux vendeurs DIFFÉRENTS remontait comme
        une django.core.exceptions.ValidationError non interceptée par
        DRF (levée par full_clean() dans Boutique.save()) — 500 au lieu
        d'un refus propre, puisque ce n'est pas le même compte donc pas
        rattrapé par la vérification 'une seule boutique par compte'.
        """
        premier_vendeur = creer_vendeur_valide('premier@anitche.ci')
        Boutique.objects.create(proprietaire=premier_vendeur, nom="Chez Awa")

        second_vendeur = creer_vendeur_valide('second@anitche.ci')
        self.client.force_authenticate(user=second_vendeur)

        reponse = self.client.post(URL_MA_BOUTIQUE, {'nom': "Chez Awa"})

        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Boutique.objects.filter(proprietaire=second_vendeur).exists())

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

    def test_vendeur_suspendu_qui_se_rouvre_reste_invisible(self):
        """La fermeture volontaire (est_active) reste au vendeur, mais ne
        lève jamais une suspension de l'administration."""
        vendeur = creer_vendeur_valide()
        boutique = Boutique.objects.create(
            proprietaire=vendeur, nom="Chez Awa", est_active=False, est_suspendue=True
        )
        self.client.force_authenticate(user=vendeur)

        reponse = self.client.patch(URL_MA_BOUTIQUE, {'est_active': True}, format='json')

        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        boutique.refresh_from_db()
        self.assertTrue(boutique.est_active)
        self.assertTrue(boutique.est_suspendue)
        self.assertFalse(boutique.est_publiable)

        self.client.force_authenticate(user=None)
        self.assertEqual(
            self.client.get(f'{URL_BOUTIQUES_PUBLIQUES}{boutique.slug}/').status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(self.client.get(URL_BOUTIQUES_PUBLIQUES).data['results'], [])

    def test_vendeur_ferme_puis_rouvre_sa_boutique(self):
        vendeur = creer_vendeur_valide()
        boutique = Boutique.objects.create(proprietaire=vendeur, nom="Chez Awa")
        url_publique = f'{URL_BOUTIQUES_PUBLIQUES}{boutique.slug}/'
        anonyme = APIClient()
        self.client.force_authenticate(user=vendeur)

        reponse = self.client.patch(URL_MA_BOUTIQUE, {'est_active': False}, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertEqual(anonyme.get(url_publique).status_code, status.HTTP_404_NOT_FOUND)

        reponse = self.client.patch(URL_MA_BOUTIQUE, {'est_active': True}, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertEqual(anonyme.get(url_publique).status_code, status.HTTP_200_OK)

    def test_vendeur_ne_peut_pas_lever_sa_suspension(self):
        vendeur = creer_vendeur_valide()
        boutique = Boutique.objects.create(proprietaire=vendeur, nom="Chez Awa", est_suspendue=True)
        self.client.force_authenticate(user=vendeur)

        reponse = self.client.patch(URL_MA_BOUTIQUE, {'est_suspendue': False}, format='json')

        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertTrue(reponse.data['est_suspendue'])
        boutique.refresh_from_db()
        self.assertTrue(boutique.est_suspendue)


class MaBoutiqueThrottleTestCase(TestCase):
    """
    Le scope 'boutique_creation' (taux bas) ne doit s'appliquer qu'au
    POST de MaBoutiqueView — pas à GET/PATCH, qui sont l'usage normal du
    dashboard vendeur et seraient sinon bloqués après quelques
    rafraîchissements.

    DIAGNOSTIC (comme apps.fidelite.tests.FideliteThrottleTestCase) :
    ScopedRateThrottle.THROTTLE_RATES est figé comme attribut de classe
    à l'import ; on le patche directement plutôt que via
    override_settings, sans effet ici. Le cache de throttling
    (LocMemCache en test) n'est pas vidé automatiquement entre tests
    Django : on repart d'un cache propre à chaque test de cette classe.
    """

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.vendeur = creer_vendeur_valide()
        Boutique.objects.create(proprietaire=self.vendeur, nom="Chez Awa")
        self.client.force_authenticate(user=self.vendeur)

    def test_get_repetes_jamais_bloques_par_le_throttle_de_creation(self):
        with patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'boutique_creation': '2/min'}):
            for _ in range(15):
                reponse = self.client.get(URL_MA_BOUTIQUE)
                self.assertNotEqual(reponse.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_post_reste_limite_par_le_throttle_de_creation(self):
        # Ce compte a déjà une boutique : chaque POST échouera en 400
        # métier ("déjà une boutique"), mais le throttle est vérifié
        # AVANT cette validation — seul le comptage des requêtes compte
        # ici, pas le succès de la création.
        with patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'boutique_creation': '2/min'}):
            statuts = [
                self.client.post(URL_MA_BOUTIQUE, {'nom': f"Autre {i}"}).status_code
                for i in range(3)
            ]
        self.assertIn(status.HTTP_429_TOO_MANY_REQUESTS, statuts)


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
        self.assertEqual([d['email'] for d in reponse.data["results"]], [self.demandeur.email])

    def test_dossier_kyc_visible_dans_la_demande(self):
        self.client.force_authenticate(user=self.administrateur)
        reponse = self.client.get(URL_DEMANDES)
        self.assertEqual(reponse.data["results"][0]['dossier_kyc']['numero_mobile_money'], '0700000000')

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
        self.assertEqual(len(reponse.data["results"]), 1)

    def url_detail(self):
        return f'/api/vendeurs/administration/boutiques/{self.boutique.id}/'

    def test_suspension_par_administrateur(self):
        # Contrat : la suspension admin passe par est_suspendue (est_active
        # est désormais la fermeture volontaire du vendeur).
        self.client.force_authenticate(user=creer_administrateur())

        reponse = self.client.patch(self.url_detail(), {'est_suspendue': True}, format='json')

        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.boutique.refresh_from_db()
        self.assertTrue(self.boutique.est_suspendue)

    def test_suspension_puis_levee_par_administrateur(self):
        self.boutique.est_active = True
        self.boutique.save(update_fields=['est_active'])
        self.client.force_authenticate(user=creer_administrateur())
        url_publique = f'{URL_BOUTIQUES_PUBLIQUES}{self.boutique.slug}/'
        anonyme = APIClient()

        with self.assertLogs('securite', level='INFO') as logs:
            reponse = self.client.patch(self.url_detail(), {'est_suspendue': True}, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertIn('suspendue', logs.output[0])
        self.assertEqual(anonyme.get(url_publique).status_code, status.HTTP_404_NOT_FOUND)

        with self.assertLogs('securite', level='INFO') as logs:
            reponse = self.client.patch(self.url_detail(), {'est_suspendue': False}, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertIn('réactivée', logs.output[0])
        self.assertEqual(anonyme.get(url_publique).status_code, status.HTTP_200_OK)

    def test_administrateur_ne_peut_pas_modifier_le_contenu(self):
        """Tout champ autre que est_suspendue → 400 explicite, rien en base."""
        self.client.force_authenticate(user=creer_administrateur())

        reponse = self.client.patch(
            self.url_detail(),
            {'nom': "Nom imposé", 'description': "Réécrite", 'ville': "Bouaké"},
            format='json',
        )

        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Seul le champ est_suspendue est modifiable ici", reponse.data['detail'])
        self.boutique.refresh_from_db()
        self.assertEqual(self.boutique.nom, "Chez Awa")
        self.assertEqual(self.boutique.description, "")
        self.assertEqual(self.boutique.ville, "")

    def test_administrateur_ne_peut_pas_modifier_est_active(self):
        self.client.force_authenticate(user=creer_administrateur())

        reponse = self.client.patch(self.url_detail(), {'est_active': True}, format='json')

        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('est_active', reponse.data['detail'])
        self.boutique.refresh_from_db()
        self.assertFalse(self.boutique.est_active)

    def test_champ_refuse_bloque_toute_la_requete(self):
        """Un champ interdit mêlé à est_suspendue : rien n'est appliqué,
        pas même la suspension (pas d'application partielle)."""
        self.client.force_authenticate(user=creer_administrateur())

        reponse = self.client.patch(
            self.url_detail(), {'est_suspendue': True, 'nom': "Nom imposé"}, format='json'
        )

        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.boutique.refresh_from_db()
        self.assertFalse(self.boutique.est_suspendue)
        self.assertEqual(self.boutique.nom, "Chez Awa")

    def test_acces_interdit_au_vendeur(self):
        self.client.force_authenticate(user=creer_vendeur_valide('autre@anitche.ci'))

        reponse = self.client.get('/api/vendeurs/administration/boutiques/')

        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN)


class BoutiqueConcurrenceTestCase(TransactionTestCase):
    """A04:2025 : deux créations de boutique simultanées pour le MÊME
    compte ne doivent jamais produire de 500 (IntegrityError sur la
    contrainte OneToOne `proprietaire`)."""

    def setUp(self):
        self.vendeur = creer_vendeur_valide()

    @skipUnless(
        connection.vendor == 'postgresql',
        "DIAGNOSTIC : select_for_update() est un no-op sur SQLite (pas de "
        "verrouillage de ligne) ; deux transactions d'écriture concurrentes "
        "s'y soldent par un comportement propre à SQLite, pas par le "
        "verrouillage réel visé par ce test — même limite déjà documentée "
        "sur apps.retours.tests.RetoursConcurrenceTestCase et "
        "apps.utilisateurs.tests.ResoumissionKYCConcurrenceTestCase.",
    )
    def test_deux_creations_simultanees_une_seule_acceptee(self):
        from concurrent.futures import ThreadPoolExecutor
        import django.db

        def appel(index):
            django.db.close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(user=self.vendeur)
                return client.post(URL_MA_BOUTIQUE, {'nom': f"Boutique {index}"}).status_code
            finally:
                django.db.connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuts = list(executor.map(appel, range(2)))

        self.assertNotIn(500, statuts)
        self.assertEqual(statuts.count(status.HTTP_201_CREATED), 1)
        self.assertEqual(statuts.count(status.HTTP_400_BAD_REQUEST), 1)
        self.assertEqual(Boutique.objects.filter(proprietaire=self.vendeur).count(), 1)