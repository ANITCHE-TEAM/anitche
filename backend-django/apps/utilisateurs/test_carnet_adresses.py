import threading
from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from .models import AdresseClient, Role, Utilisateur

URL_ADRESSES = '/api/utilisateurs/adresses/'
MOT_DE_PASSE = 'MotDePasseSolide123!'
LIMITE = 10

ADRESSE = {
    'libelle': 'Maison',
    'destinataire': 'Awa Kouassi',
    'telephone': '0700000001',
    'commune': 'Cocody',
    'quartier': 'Angré 8e Tranche',
    'point_de_repere': 'Derrière la pharmacie',
}


def creer(email, **extra):
    return Utilisateur.objects.create_user(
        email=email, password=MOT_DE_PASSE, nom='N', prenom='P', email_verifie=True, **extra,
    )


def url_adresse(adresse):
    return f'{URL_ADRESSES}{adresse.pk}/'


class CarnetTestCase(TestCase):
    def setUp(self):
        self.proprietaire = creer('awa@anitche.ci')
        self.autre = creer('kofi@anitche.ci')
        self.api = APIClient()
        self.api.force_authenticate(self.proprietaire)

    def ajouter(self, client=None, **champs):
        return AdresseClient.objects.create(client=client or self.proprietaire, **{**ADRESSE, **champs})


class CreationEtLectureTests(CarnetTestCase):
    def test_creation_nominale(self):
        reponse = self.api.post(URL_ADRESSES, {**ADRESSE, 'indications': 'Portail bleu', 'telephone': '07 00-00.00 01'}, format='json')
        self.assertEqual(reponse.status_code, 201, reponse.data)
        adresse = AdresseClient.objects.get()
        self.assertEqual(adresse.client, self.proprietaire)
        self.assertEqual(adresse.telephone, '0700000001')
        self.assertEqual(adresse.indications, 'Portail bleu')
        self.assertFalse(adresse.par_defaut)
        self.assertEqual(reponse.data['id'], str(adresse.pk))
        self.assertEqual(reponse.data['commune'], 'Cocody')
        self.assertNotIn('client', reponse.data)

    def test_le_proprietaire_vient_de_la_session_jamais_du_corps(self):
        reponse = self.api.post(URL_ADRESSES, {**ADRESSE, 'client': self.autre.pk}, format='json')
        self.assertEqual(reponse.status_code, 201)
        self.assertEqual(AdresseClient.objects.get().client, self.proprietaire)

    def test_position_facultative_mais_complete(self):
        reponse = self.api.post(URL_ADRESSES, {**ADRESSE, 'latitude': 5.359952, 'longitude': -3.986912}, format='json')
        self.assertEqual(reponse.status_code, 201, reponse.data)
        self.assertEqual((reponse.data['latitude'], reponse.data['longitude']), (5.359952, -3.986912))
        seule = self.api.post(URL_ADRESSES, {**ADRESSE, 'latitude': 5.36}, format='json')
        self.assertEqual(seule.status_code, 400)
        hors_pays = self.api.post(URL_ADRESSES, {**ADRESSE, 'latitude': 48.85, 'longitude': 2.35}, format='json')
        self.assertEqual(hors_pays.status_code, 400)

    def test_champs_invalides_refuses(self):
        cas = {
            'téléphone invalide': {'telephone': 'abc'},
            'commune absente': {'commune': ''},
            'libellé absent': {'libelle': ''},
            'destinataire absent': {'destinataire': ''},
            'repère absent': {'point_de_repere': ''},
        }
        for libelle, changement in cas.items():
            with self.subTest(libelle):
                reponse = self.api.post(URL_ADRESSES, {**ADRESSE, **changement}, format='json')
                self.assertEqual(reponse.status_code, 400)
        self.assertFalse(AdresseClient.objects.exists())

    def test_authentification_obligatoire(self):
        anonyme = APIClient()
        self.assertEqual(anonyme.get(URL_ADRESSES).status_code, 401)
        self.assertEqual(anonyme.post(URL_ADRESSES, ADRESSE, format='json').status_code, 401)

    def test_la_liste_ne_contient_que_mes_adresses(self):
        mienne = self.ajouter()
        self.ajouter(client=self.autre, libelle='Bureau')
        reponse = self.api.get(URL_ADRESSES)
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual([a['id'] for a in reponse.data['results']], [str(mienne.pk)])

    def test_lecture_modification_suppression_nominales(self):
        adresse = self.ajouter()
        self.assertEqual(self.api.get(url_adresse(adresse)).data['quartier'], 'Angré 8e Tranche')
        modif = self.api.patch(url_adresse(adresse), {'quartier': 'Riviera 2'}, format='json')
        self.assertEqual(modif.status_code, 200)
        adresse.refresh_from_db()
        self.assertEqual(adresse.quartier, 'Riviera 2')
        self.assertEqual(self.api.delete(url_adresse(adresse)).status_code, 204)
        self.assertFalse(AdresseClient.objects.exists())


class LimiteDeDixAdressesTests(CarnetTestCase):
    def test_la_onzieme_est_refusee(self):
        for numero in range(LIMITE):
            reponse = self.api.post(URL_ADRESSES, {**ADRESSE, 'libelle': f'Adresse {numero}'}, format='json')
            self.assertEqual(reponse.status_code, 201, reponse.data)
        reponse = self.api.post(URL_ADRESSES, {**ADRESSE, 'libelle': 'De trop'}, format='json')
        self.assertEqual(reponse.status_code, 400)
        self.assertEqual(reponse.data['errors']['code'], ['carnet_plein'])
        self.assertEqual(AdresseClient.objects.filter(client=self.proprietaire).count(), LIMITE)

    def test_supprimer_libere_une_place(self):
        adresses = [self.ajouter(libelle=f'Adresse {n}') for n in range(LIMITE)]
        self.api.delete(url_adresse(adresses[0]))
        self.assertEqual(self.api.post(URL_ADRESSES, ADRESSE, format='json').status_code, 201)

    def test_la_limite_est_par_client(self):
        for numero in range(LIMITE):
            self.ajouter(libelle=f'Adresse {numero}')
        autre = APIClient()
        autre.force_authenticate(self.autre)
        self.assertEqual(autre.post(URL_ADRESSES, ADRESSE, format='json').status_code, 201)


class AdresseParDefautTests(CarnetTestCase):
    def par_defaut(self, client=None):
        return list(AdresseClient.objects.filter(client=client or self.proprietaire, par_defaut=True))

    def test_creer_une_adresse_par_defaut_remplace_la_precedente(self):
        premiere = self.ajouter(par_defaut=True)
        reponse = self.api.post(URL_ADRESSES, {**ADRESSE, 'libelle': 'Bureau', 'par_defaut': True}, format='json')
        self.assertEqual(reponse.status_code, 201, reponse.data)
        self.assertTrue(reponse.data['par_defaut'])
        premiere.refresh_from_db()
        self.assertFalse(premiere.par_defaut)
        self.assertEqual([a.libelle for a in self.par_defaut()], ['Bureau'])

    def test_passer_une_adresse_par_defaut_remplace_la_precedente(self):
        premiere = self.ajouter(par_defaut=True)
        seconde = self.ajouter(libelle='Bureau')
        reponse = self.api.patch(url_adresse(seconde), {'par_defaut': True}, format='json')
        self.assertEqual(reponse.status_code, 200, reponse.data)
        premiere.refresh_from_db()
        self.assertFalse(premiere.par_defaut)
        self.assertEqual(self.par_defaut(), [seconde])

    def test_retirer_le_defaut_n_en_promeut_aucune_autre(self):
        premiere = self.ajouter(par_defaut=True)
        self.ajouter(libelle='Bureau')
        self.api.patch(url_adresse(premiere), {'par_defaut': False}, format='json')
        self.assertEqual(self.par_defaut(), [])

    def test_le_defaut_d_un_client_ne_touche_pas_celui_d_un_autre(self):
        celle_de_l_autre = self.ajouter(client=self.autre, par_defaut=True)
        self.api.post(URL_ADRESSES, {**ADRESSE, 'par_defaut': True}, format='json')
        celle_de_l_autre.refresh_from_db()
        self.assertTrue(celle_de_l_autre.par_defaut)

    def test_la_base_refuse_deux_adresses_par_defaut_pour_un_client(self):
        self.ajouter(par_defaut=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.ajouter(libelle='Bureau', par_defaut=True)
        # Un autre client, ou une adresse non par défaut : accepté.
        self.ajouter(client=self.autre, par_defaut=True)
        self.ajouter(libelle='Bureau')

    def test_la_base_refuse_une_position_incomplete(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.ajouter(latitude=Decimal('5.36'))


class AdresseDUnAutreClientTests(CarnetTestCase):
    """Une adresse qui n'est pas la mienne n'existe pas : 404, jamais 403."""

    def setUp(self):
        super().setUp()
        self.etrangere = self.ajouter(client=self.autre, libelle='Chez Kofi')

    def test_lecture(self):
        self.assertEqual(self.api.get(url_adresse(self.etrangere)).status_code, 404)

    def test_modification(self):
        for methode, corps in (('patch', {'quartier': 'Piraté'}), ('put', {**ADRESSE, 'quartier': 'Piraté'})):
            with self.subTest(methode):
                reponse = getattr(self.api, methode)(url_adresse(self.etrangere), corps, format='json')
                self.assertEqual(reponse.status_code, 404)
        self.etrangere.refresh_from_db()
        self.assertEqual(self.etrangere.quartier, 'Angré 8e Tranche')

    def test_modification_du_defaut_d_un_autre(self):
        reponse = self.api.patch(url_adresse(self.etrangere), {'par_defaut': True}, format='json')
        self.assertEqual(reponse.status_code, 404)
        self.etrangere.refresh_from_db()
        self.assertFalse(self.etrangere.par_defaut)

    def test_suppression(self):
        self.assertEqual(self.api.delete(url_adresse(self.etrangere)).status_code, 404)
        self.assertTrue(AdresseClient.objects.filter(pk=self.etrangere.pk).exists())

    def test_meme_reponse_qu_une_adresse_inexistante(self):
        inexistante = '00000000-0000-4000-8000-000000000000'
        reelle = self.api.get(url_adresse(self.etrangere))
        absente = self.api.get(f'{URL_ADRESSES}{inexistante}/')
        self.assertEqual(reelle.status_code, absente.status_code)
        self.assertEqual(reelle.data, absente.data)

    def test_ni_le_vendeur_ni_l_administrateur_ne_lisent_le_carnet_d_un_client(self):
        vendeur = creer('vendeur@anitche.ci', role=Role.VENDEUR)
        admin = creer('admin@anitche.ci', role=Role.ADMIN)
        for utilisateur in (vendeur, admin):
            with self.subTest(utilisateur.role):
                api = APIClient()
                api.force_authenticate(utilisateur)
                self.assertEqual(api.get(url_adresse(self.etrangere)).status_code, 404)
                self.assertEqual(api.get(URL_ADRESSES).data['results'], [])


class ConcurrenceCarnetTests(TransactionTestCase):
    def test_des_creations_simultanees_ne_depassent_pas_la_limite(self):
        proprietaire = creer('awa@anitche.ci')
        barriere = threading.Barrier(LIMITE + 4)
        codes = []

        def creer_une_adresse():
            try:
                api = APIClient()
                api.force_authenticate(proprietaire)
                barriere.wait()
                codes.append(api.post(URL_ADRESSES, ADRESSE, format='json').status_code)
            finally:
                connection.close()

        threads = [threading.Thread(target=creer_une_adresse) for _ in range(LIMITE + 4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(codes), [201] * LIMITE + [400] * 4)
        self.assertEqual(AdresseClient.objects.filter(client=proprietaire).count(), LIMITE)
