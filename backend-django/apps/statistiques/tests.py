from datetime import date, datetime
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.avis.models import Avis, DecisionModeration, SignalementAvis
from apps.catalogue.models import Produit, VarianteProduit
from apps.commandes.models import Commande, CommandeItem
from apps.paiements.models import Reversement
from apps.retours.models import DemandeRetour, RetourItem
from apps.utilisateurs.models import Role, StatutKYC, Utilisateur
from apps.vendeurs.models import Boutique

URL_VENDEUR = '/api/statistiques/vendeur/'
URL_ADMIN = '/api/statistiques/administration/'
OCTOBRE = {'date_debut': '2026-10-01', 'date_fin': '2026-10-31'}

Statut = Commande.Status
Rev = Reversement.Statut
Ret = DemandeRetour.Statut


def instant(texte):
    return datetime.fromisoformat(texte)


class Constructeur:
    """Données construites à la main, sans passer par le checkout : chaque
    commande a ses lignes (produit, prix, quantité, commission, frais fixes)
    et son reversement, pour que les valeurs attendues s'écrivent en dur."""

    @classmethod
    def utilisateur(cls, email, role=Role.CLIENT, quand='2026-09-01T00:00:00+00:00', **extra):
        utilisateur = Utilisateur.objects.create_user(
            email=email, password='x', nom='N', prenom='P', role=role, email_verifie=True, **extra,
        )
        Utilisateur.objects.filter(pk=utilisateur.pk).update(date_creation=instant(quand))
        return utilisateur

    @classmethod
    def vendeur_avec_boutique(cls, nom):
        vendeur = cls.utilisateur(f'{nom}@stat.ci', Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        return vendeur, Boutique.objects.create(proprietaire=vendeur, nom=nom)

    @classmethod
    def produit(cls, boutique, numero):
        produit = Produit.objects.create(boutique=boutique, nom=f'Produit {numero}', prix_base=1000)
        variante = VarianteProduit.objects.create(produit=produit, nom='Standard', prix=1000)
        return produit, variante

    @classmethod
    def commande(cls, boutique, statut, quand, lignes, reversement=None):
        """`lignes` : [(produit, prix, quantité, commission, frais fixes)] ;
        `reversement` : (statut, net, montant à verser) ou None."""
        brut = sum(prix * quantite for _, prix, quantite, _, _ in lignes)
        commande = Commande.objects.create(
            boutique=boutique, client=cls.acheteur, status=statut, montant_total=brut,
        )
        Commande.objects.filter(pk=commande.pk).update(created_at=instant(quand))
        items = {}
        for produit, prix, quantite, commission, frais in lignes:
            items[produit] = CommandeItem.objects.create(
                commande=commande, variante=cls.variantes[produit], nom_produit=f'Produit {produit}',
                prix_unitaire=prix, quantite=quantite, montant_commission=commission, montant_frais_fixes=frais,
            )
        if reversement is not None:
            statut_reversement, net, a_verser = reversement
            Reversement.objects.create(
                commande=commande, boutique=boutique, montant_brut=brut,
                montant_commission=sum(ligne[3] for ligne in lignes),
                montant_frais_fixes=sum(ligne[4] for ligne in lignes),
                montant_net=net, montant_a_verser=a_verser, statut=statut_reversement,
            )
        commande.items = items
        return commande

    @classmethod
    def retour(cls, commande, produit, statut, quantite=1):
        demande = DemandeRetour.objects.create(
            commande=commande, client=cls.acheteur, boutique=commande.boutique, statut=statut,
            description='Description du retour',
        )
        RetourItem.objects.create(demande_retour=demande, commande_item=commande.items[produit], quantite=quantite)
        return demande

    @classmethod
    def avis(cls, commande, produit, note, statut=Avis.Statut.PUBLIE, quand=None):
        auteur = cls.utilisateur(f'avis{Avis.objects.count()}@stat.ci')
        avis = Avis.objects.create(
            client=auteur, produit=cls.produits[produit], boutique=commande.boutique,
            commande_item=commande.items[produit], note=note, statut=statut,
        )
        if quand:
            Avis.objects.filter(pk=avis.pk).update(date_creation=instant(quand))
        return avis

    @classmethod
    def creer_catalogue(cls):
        cls.acheteur = cls.utilisateur('acheteur@stat.ci', quand='2026-10-03T09:00:00+00:00')
        cls.vendeur_a, cls.boutique_a = cls.vendeur_avec_boutique('boutiquea')
        cls.vendeur_b, cls.boutique_b = cls.vendeur_avec_boutique('boutiqueb')
        cls.vendeur_c, cls.boutique_c = cls.vendeur_avec_boutique('boutiquec')
        cls.vendeur_d, cls.boutique_d = cls.vendeur_avec_boutique('boutiqued')
        cls.admin = cls.utilisateur('admin@stat.ci', Role.ADMIN)
        cls.produits, cls.variantes = {}, {}
        for numero, boutique in ((1, cls.boutique_a), (2, cls.boutique_a), (3, cls.boutique_a),
                                 (4, cls.boutique_a), (5, cls.boutique_a), (6, cls.boutique_a),
                                 (7, cls.boutique_b), (8, cls.boutique_c), (9, cls.boutique_d)):
            cls.produits[numero], cls.variantes[numero] = cls.produit(boutique, numero)


class JeuDeDonnees(Constructeur):
    """Octobre 2026 en Africa/Abidjan (UTC+0, sans heure d'été).

    Boutique A, dans la période et comptées : A1, A2, A3 (dernière seconde du
    31), A9 (00 h 30 à +02:00 le 1er novembre, donc le 31 à Abidjan), A10
    (minuit pile du 1er), A11, A12. Hors période : A7 (30/09 23:59:59), A8
    (01/11 minuit), A13 (01 h 00 à +02:00 le 1er octobre, donc le 30/09 à
    Abidjan). Dans la période mais non comptées : A4 annulée, A5 non payée,
    A6 livrée dont le reversement est annulé (contestation fondée).
    """

    @classmethod
    def creer_jeu(cls):
        cls.creer_catalogue()
        c = cls.commande
        cls.a1 = c(cls.boutique_a, Statut.LIVREE, '2026-10-05T12:00:00+00:00',
                   [(1, 1000, 2, 280, 200), (2, 3500, 1, 490, 200)], (Rev.VERSE, 4330, 4330))
        cls.a2 = c(cls.boutique_a, Statut.CONFIRMEE, '2026-10-10T08:00:00+00:00',
                   [(1, 1000, 1, 140, 100)], (Rev.EN_ATTENTE_LIVRAISON, 760, 0))
        cls.a3 = c(cls.boutique_a, Statut.EXPEDIEE, '2026-10-31T23:59:59+00:00',
                   [(2, 3500, 2, 980, 400)], (Rev.EN_COURS, 5620, 0))
        cls.a4 = c(cls.boutique_a, Statut.ANNULEE, '2026-10-12T10:00:00+00:00',
                   [(1, 1000, 3, 420, 300)], (Rev.ANNULE, 0, 0))
        cls.a5 = c(cls.boutique_a, Statut.CREEE, '2026-10-13T10:00:00+00:00', [(1, 1000, 1, 140, 100)])
        cls.a6 = c(cls.boutique_a, Statut.LIVREE, '2026-10-20T10:00:00+00:00',
                   [(1, 1000, 1, 140, 100)], (Rev.ANNULE, 0, 0))
        cls.a7 = c(cls.boutique_a, Statut.LIVREE, '2026-09-30T23:59:59+00:00',
                   [(1, 50000, 1, 7000, 100)], (Rev.VERSE, 42900, 42900))
        cls.a8 = c(cls.boutique_a, Statut.CONFIRMEE, '2026-11-01T00:00:00+00:00',
                   [(2, 70000, 1, 9800, 200)], (Rev.EN_ATTENTE_LIVRAISON, 60000, 0))
        cls.a9 = c(cls.boutique_a, Statut.PREPARATION, '2026-11-01T00:30:00+02:00',
                   [(1, 1000, 1, 140, 100)], (Rev.EN_RETRACTATION, 760, 0))
        cls.a10 = c(cls.boutique_a, Statut.CONFIRMEE, '2026-10-01T00:00:00+00:00', [(2, 3500, 1, 490, 200)])
        cls.a11 = c(cls.boutique_a, Statut.CONFIRMEE, '2026-10-15T10:00:00+00:00',
                    [(3, 500, 1, 70, 100), (4, 400, 1, 56, 100), (5, 300, 1, 42, 100), (6, 200, 1, 28, 100)],
                    (Rev.EN_ATTENTE_LIVRAISON, 804, 0))
        cls.a12 = c(cls.boutique_a, Statut.LIVREE, '2026-10-22T10:00:00+00:00',
                    [(1, 1000, 1, 140, 100)], (Rev.VERSE, 760, 700))
        cls.a13 = c(cls.boutique_a, Statut.CONFIRMEE, '2026-10-01T01:00:00+02:00',
                    [(1, 9000, 1, 1260, 100)], (Rev.EN_ATTENTE_LIVRAISON, 7640, 0))

        cls.b1 = c(cls.boutique_b, Statut.LIVREE, '2026-10-08T10:00:00+00:00',
                   [(7, 2000, 1, 280, 200)], (Rev.VERSE, 1520, 1520))
        cls.b2 = c(cls.boutique_b, Statut.CONFIRMEE, '2026-10-09T10:00:00+00:00',
                   [(7, 2000, 2, 560, 400)], (Rev.EN_ATTENTE_LIVRAISON, 3040, 0))
        cls.b3 = c(cls.boutique_b, Statut.ANNULEE, '2026-10-11T10:00:00+00:00',
                   [(7, 2000, 1, 280, 200)], (Rev.ANNULE, 0, 0))
        c(cls.boutique_c, Statut.ANNULEE, '2026-10-12T10:00:00+00:00', [(8, 1000, 1, 140, 100)], (Rev.ANNULE, 0, 0))
        c(cls.boutique_d, Statut.CONFIRMEE, '2026-09-01T10:00:00+00:00',
          [(9, 1000, 1, 140, 100)], (Rev.EN_ATTENTE_LIVRAISON, 760, 0))

        # Retours : seul un retour remboursé d'une commande comptée se déduit ;
        # tout retour accepté d'une livraison comptée entre dans le taux.
        cls.retour(cls.a1, 2, Ret.REMBOURSE)
        cls.retour(cls.a1, 1, Ret.APPROUVE)
        cls.retour(cls.a12, 1, Ret.REJETE)
        cls.retour(cls.a6, 1, Ret.REMBOURSE)
        cls.retour(cls.a7, 1, Ret.REMBOURSE)

        # Avis de la boutique A : trois publiés dans la période (5, 4, 4).
        cls.avis(cls.a1, 1, 5)
        cls.avis(cls.a1, 2, 4)
        cls.avis(cls.a2, 1, 4)
        masque = cls.avis(cls.a3, 2, 1, Avis.Statut.MASQUE)
        cls.avis(cls.a10, 2, 2, Avis.Statut.SUPPRIME)
        ancien = cls.avis(cls.a12, 1, 1, quand='2026-09-15T10:00:00+00:00')
        cls.avis(cls.b1, 7, 5)

        # Signalements : deux ouverts (dont un créé avant la période), un
        # accepté et un rejeté.
        moderateur = cls.admin
        auteur = cls.utilisateur('signaleur@stat.ci')
        ouvert_ancien = SignalementAvis.objects.create(avis=ancien, auteur=auteur, motif='spam')
        SignalementAvis.objects.filter(pk=ouvert_ancien.pk).update(date_creation=instant('2026-09-15T10:00:00+00:00'))
        SignalementAvis.objects.create(avis=masque, auteur=auteur, motif='spam')
        for avis, type_decision, statut in (
            (Avis.objects.get(commande_item=cls.a1.items[1]), 'rejeter_signalement', SignalementAvis.Statut.REJETE),
            (Avis.objects.get(commande_item=cls.a1.items[2]), 'masquer', SignalementAvis.Statut.ACCEPTE),
        ):
            decision = DecisionModeration.objects.create(
                avis=avis, moderateur=moderateur, type_decision=type_decision, motif='Motif',
            )
            SignalementAvis.objects.create(
                avis=avis, auteur=auteur, motif='spam', statut=statut, decision=decision,
                date_traitement=instant('2026-10-20T10:00:00+00:00'),
            )

        # Comptes créés : trois clients dans la période (dont le 31 à 22 h 30
        # UTC écrit à +02:00 le 1er novembre), un avant, un après, un vendeur.
        cls.utilisateur('c2@stat.ci', quand='2026-10-31T23:00:00+00:00')
        cls.utilisateur('c3@stat.ci', quand='2026-11-01T00:30:00+02:00')
        cls.utilisateur('avant@stat.ci', quand='2026-09-30T23:59:59+00:00')
        cls.utilisateur('apres@stat.ci', quand='2026-11-01T00:00:00+00:00')
        cls.utilisateur('vendeur.nouveau@stat.ci', Role.VENDEUR, quand='2026-10-10T10:00:00+00:00')


class AvecApi:
    def lire(self, url, utilisateur, **parametres):
        api = APIClient()
        api.force_authenticate(utilisateur)
        return api.get(url, parametres)


class IndicateursVendeurTests(JeuDeDonnees, AvecApi, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.creer_jeu()

    def donnees(self, utilisateur=None):
        reponse = self.lire(URL_VENDEUR, utilisateur or self.vendeur_a, **OCTOBRE)
        self.assertEqual(reponse.status_code, 200, reponse.content)
        return reponse.json()

    def test_periode_renvoyee(self):
        self.assertEqual(self.donnees()['periode'], {'date_debut': '2026-10-01', 'date_fin': '2026-10-31'})

    def test_chiffre_d_affaires(self):
        # Ventes brutes 5500 + 1000 + 7000 + 1000 + 3500 + 1400 + 1000 = 20400,
        # moins l'article remboursé de A1 (3500). Exclus : A4 annulée, A5 non
        # payée, A6 au reversement annulé, A7 / A8 / A13 hors période.
        donnees = self.donnees()
        self.assertEqual(donnees['montant_retours'], 3500)
        self.assertEqual(donnees['chiffre_affaires'], 16900)

    def test_nombre_de_commandes_comptees(self):
        self.assertEqual(self.donnees()['nombre_commandes'], 7)

    def test_commandes_par_statut(self):
        # Toutes les commandes créées dans la période, y compris celles qui
        # ne comptent pas (A6 livrée au reversement annulé, A4, A5).
        self.assertEqual(self.donnees()['commandes_par_statut'], {
            'creee': 1, 'confirmee': 3, 'preparation': 1, 'expediee': 1, 'livree': 3, 'annulee': 1,
        })

    def test_panier_moyen(self):
        # 20400 ventes brutes / 7 commandes = 2914,28.
        self.assertEqual(self.donnees()['panier_moyen'], 2914)

    def test_top_cinq_produits_par_ventes_brutes(self):
        top = self.donnees()['top_produits']
        self.assertEqual(top, [
            {'produit': self.produits[2].pk, 'nom': 'Produit 2', 'quantite': 4, 'ventes': 14000},
            {'produit': self.produits[1].pk, 'nom': 'Produit 1', 'quantite': 5, 'ventes': 5000},
            {'produit': self.produits[3].pk, 'nom': 'Produit 3', 'quantite': 1, 'ventes': 500},
            {'produit': self.produits[4].pk, 'nom': 'Produit 4', 'quantite': 1, 'ventes': 400},
            {'produit': self.produits[5].pk, 'nom': 'Produit 5', 'quantite': 1, 'ventes': 300},
        ])

    def test_note_moyenne_et_nombre_d_avis(self):
        # Publiés dans la période : 5, 4, 4. Exclus : masqué, supprimé, publié
        # avant la période, avis d'une autre boutique.
        self.assertEqual(self.donnees()['avis'], {'note_moyenne': 4.33, 'nombre': 3})

    def test_taux_de_retour(self):
        # Livraisons comptées : A1 (retours accepté et remboursé) et A12
        # (retour rejeté) ; A6 livrée est exclue : 1 / 2.
        self.assertEqual(self.donnees()['taux_retour'], 50.0)

    def test_montants_reverses_et_a_venir(self):
        # Versés : 4330 (A1) + 700 (A12). À venir : 760 (A2) + 5620 (A3) +
        # 760 (A9) + 804 (A11). A10 sans reversement, A4 / A6 annulés.
        self.assertEqual(self.donnees()['reversements'], {'verses': 5030, 'a_venir': 7944})

    def test_la_boutique_b_ne_voit_que_ses_chiffres(self):
        donnees = self.donnees(self.vendeur_b)
        self.assertEqual(donnees['chiffre_affaires'], 6000)
        self.assertEqual(donnees['montant_retours'], 0)
        self.assertEqual(donnees['nombre_commandes'], 2)
        self.assertEqual(donnees['panier_moyen'], 3000)
        self.assertEqual(donnees['commandes_par_statut'], {
            'creee': 0, 'confirmee': 1, 'preparation': 0, 'expediee': 0, 'livree': 1, 'annulee': 1,
        })
        self.assertEqual(donnees['top_produits'], [
            {'produit': self.produits[7].pk, 'nom': 'Produit 7', 'quantite': 3, 'ventes': 6000},
        ])
        self.assertEqual(donnees['avis'], {'note_moyenne': 5.0, 'nombre': 1})
        self.assertEqual(donnees['taux_retour'], 0.0)
        self.assertEqual(donnees['reversements'], {'verses': 1520, 'a_venir': 3040})

    def test_un_parametre_boutique_est_ignore(self):
        reponse = self.lire(URL_VENDEUR, self.vendeur_b, boutique=self.boutique_a.pk, **OCTOBRE)
        self.assertEqual(reponse.json()['chiffre_affaires'], 6000)

    def test_vendeur_sans_aucune_vente(self):
        donnees = self.donnees(self.vendeur_d)
        self.assertEqual(donnees['chiffre_affaires'], 0)
        self.assertEqual(donnees['nombre_commandes'], 0)
        self.assertEqual(donnees['panier_moyen'], 0)
        self.assertEqual(donnees['top_produits'], [])
        self.assertEqual(donnees['avis'], {'note_moyenne': None, 'nombre': 0})
        self.assertEqual(donnees['taux_retour'], 0.0)
        self.assertEqual(donnees['reversements'], {'verses': 0, 'a_venir': 0})

    def test_les_montants_sont_des_entiers(self):
        donnees = self.donnees()
        for cle in ('chiffre_affaires', 'montant_retours', 'panier_moyen'):
            self.assertIsInstance(donnees[cle], int)
        for ligne in donnees['top_produits']:
            self.assertIsInstance(ligne['ventes'], int)
        for valeur in donnees['reversements'].values():
            self.assertIsInstance(valeur, int)


class IndicateursAdministrationTests(JeuDeDonnees, AvecApi, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.creer_jeu()

    def donnees(self):
        reponse = self.lire(URL_ADMIN, self.admin, **OCTOBRE)
        self.assertEqual(reponse.status_code, 200, reponse.content)
        return reponse.json()

    def test_volume_des_ventes(self):
        # Boutique A 20400 + boutique B (2000 + 4000) = 26400 de ventes
        # brutes, moins 3500 remboursés.
        donnees = self.donnees()
        self.assertEqual(donnees['montant_retours'], 3500)
        self.assertEqual(donnees['volume_ventes'], 22900)
        self.assertEqual(donnees['nombre_commandes'], 9)

    def test_commissions(self):
        # Commissions et frais fixes des commandes comptées : boutique A 4556,
        # boutique B 1440 ; moins 490 de commission rendue sur le retour
        # remboursé (le frais fixe reste acquis).
        self.assertEqual(self.donnees()['commissions'], 5506)

    def test_commandes_par_statut(self):
        self.assertEqual(self.donnees()['commandes_par_statut'], {
            'creee': 1, 'confirmee': 4, 'preparation': 1, 'expediee': 1, 'livree': 4, 'annulee': 3,
        })

    def test_nouveaux_clients(self):
        # Acheteur (3/10), c2 (31/10 23 h UTC) et c3 (31/10 à Abidjan). Ni
        # avant, ni après, ni les vendeurs, ni les clients d'avis (septembre).
        self.assertEqual(self.donnees()['nouveaux_clients'], 3)

    def test_vendeurs_actifs(self):
        # A et B ont une commande comptée ; C n'a qu'une annulée, D une vente
        # hors période.
        self.assertEqual(self.donnees()['vendeurs_actifs'], 2)

    def test_reversements_en_cours_et_verses(self):
        # Versés : 5030 (A) + 1520 (B1). En cours : 7944 (A) + 3040 (B2).
        self.assertEqual(self.donnees()['reversements'], {'en_cours': 10984, 'verses': 6550})

    def test_avis_signales_en_attente_sans_filtre_de_periode(self):
        self.assertEqual(self.donnees()['avis_signales_en_attente'], 2)
        reponse = self.lire(URL_ADMIN, self.admin, date_debut='2026-01-01', date_fin='2026-01-02')
        self.assertEqual(reponse.json()['avis_signales_en_attente'], 2)

    def test_periode_sans_donnee(self):
        reponse = self.lire(URL_ADMIN, self.admin, date_debut='2026-01-01', date_fin='2026-01-02')
        donnees = reponse.json()
        self.assertEqual((donnees['volume_ventes'], donnees['commissions'], donnees['nombre_commandes']), (0, 0, 0))
        self.assertEqual(donnees['vendeurs_actifs'], 0)
        self.assertEqual(donnees['reversements'], {'en_cours': 0, 'verses': 0})


class PeriodeTests(Constructeur, AvecApi, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.creer_catalogue()
        c = cls.commande
        cls.premiere_seconde = c(cls.boutique_a, Statut.CONFIRMEE, '2026-09-19T00:00:00+00:00', [(1, 1000, 1, 140, 100)])
        cls.veille = c(cls.boutique_a, Statut.CONFIRMEE, '2026-09-18T23:59:59+00:00', [(1, 20000, 1, 0, 0)])
        cls.derniere_seconde = c(cls.boutique_a, Statut.CONFIRMEE, '2026-10-18T23:59:59+00:00', [(1, 200, 1, 0, 0)])
        cls.lendemain = c(cls.boutique_a, Statut.CONFIRMEE, '2026-10-19T00:00:00+00:00', [(1, 30000, 1, 0, 0)])
        # 01 h 00 à +02:00 : 23 h la veille à Abidjan, hors de la période.
        cls.fuseau_voisin = c(cls.boutique_a, Statut.CONFIRMEE, '2026-09-19T01:00:00+02:00', [(1, 400000, 1, 0, 0)])

    def test_defaut_les_trente_derniers_jours_aujourd_hui_compris(self):
        with patch('apps.statistiques.services.aujourd_hui', return_value=date(2026, 10, 18)):
            reponse = self.lire(URL_VENDEUR, self.vendeur_a)
        donnees = reponse.json()
        self.assertEqual(donnees['periode'], {'date_debut': '2026-09-19', 'date_fin': '2026-10-18'})
        self.assertEqual(donnees['nombre_commandes'], 2)
        self.assertEqual(donnees['chiffre_affaires'], 1200)

    def test_bornes_incluses_en_heure_d_abidjan(self):
        donnees = self.lire(URL_VENDEUR, self.vendeur_a, date_debut='2026-09-19', date_fin='2026-10-18').json()
        self.assertEqual(donnees['chiffre_affaires'], 1200)

    def test_un_seul_jour(self):
        donnees = self.lire(URL_VENDEUR, self.vendeur_a, date_debut='2026-10-18', date_fin='2026-10-18').json()
        self.assertEqual((donnees['nombre_commandes'], donnees['chiffre_affaires']), (1, 200))

    def test_date_fin_seule_remonte_de_trente_jours(self):
        donnees = self.lire(URL_VENDEUR, self.vendeur_a, date_fin='2026-10-18').json()
        self.assertEqual(donnees['periode'], {'date_debut': '2026-09-19', 'date_fin': '2026-10-18'})

    def test_date_debut_seule_va_jusqu_a_aujourd_hui(self):
        with patch('apps.statistiques.services.aujourd_hui', return_value=date(2026, 10, 18)):
            donnees = self.lire(URL_VENDEUR, self.vendeur_a, date_debut='2026-10-18').json()
        self.assertEqual(donnees['periode'], {'date_debut': '2026-10-18', 'date_fin': '2026-10-18'})

    def test_periodes_invalides(self):
        cas = {
            'date illisible': {'date_debut': 'hier'},
            'jour inexistant': {'date_debut': '2026-02-30'},
            'fin avant début': {'date_debut': '2026-10-18', 'date_fin': '2026-10-17'},
            'plus de 366 jours': {'date_debut': '2025-01-01', 'date_fin': '2026-10-18'},
        }
        for libelle, parametres in cas.items():
            for url, utilisateur in ((URL_VENDEUR, self.vendeur_a), (URL_ADMIN, self.admin)):
                with self.subTest(libelle, url=url):
                    self.assertEqual(self.lire(url, utilisateur, **parametres).status_code, 400)

    def test_366_jours_acceptes_367_refuses(self):
        # Bornes comprises : du 18/10/2025 au 18/10/2026, 366 jours.
        reponse = self.lire(URL_VENDEUR, self.vendeur_a, date_debut='2025-10-18', date_fin='2026-10-18')
        self.assertEqual(reponse.status_code, 200)
        reponse = self.lire(URL_VENDEUR, self.vendeur_a, date_debut='2025-10-17', date_fin='2026-10-18')
        self.assertEqual(reponse.status_code, 400)


class DroitsTests(JeuDeDonnees, AvecApi, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.creer_jeu()
        cls.vendeur_non_valide = cls.utilisateur('nonvalide@stat.ci', Role.VENDEUR)
        cls.vendeur_sans_boutique = cls.utilisateur('sansboutique@stat.ci', Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        cls.livreur = cls.utilisateur('livreur@stat.ci', Role.LIVREUR)
        cls.staff_seul = cls.utilisateur('staff@stat.ci', Role.CLIENT, is_staff=True)
        cls.super_admin = cls.utilisateur('super@stat.ci', Role.SUPER_ADMIN)

    def test_anonyme_401(self):
        for url in (URL_VENDEUR, URL_ADMIN):
            self.assertEqual(APIClient().get(url).status_code, 401)

    def test_un_vendeur_n_accede_pas_a_l_administration(self):
        self.assertEqual(self.lire(URL_ADMIN, self.vendeur_a, **OCTOBRE).status_code, 403)

    def test_aucun_autre_role_n_accede_a_l_administration(self):
        for utilisateur in (self.acheteur, self.livreur, self.staff_seul, self.vendeur_non_valide):
            with self.subTest(utilisateur.email):
                self.assertEqual(self.lire(URL_ADMIN, utilisateur, **OCTOBRE).status_code, 403)

    def test_administrateurs_acceptes(self):
        for utilisateur in (self.admin, self.super_admin):
            self.assertEqual(self.lire(URL_ADMIN, utilisateur, **OCTOBRE).status_code, 200)

    def test_espace_vendeur_reserve_aux_vendeurs_valides(self):
        for utilisateur in (self.acheteur, self.livreur, self.admin, self.vendeur_non_valide):
            with self.subTest(utilisateur.email):
                self.assertEqual(self.lire(URL_VENDEUR, utilisateur, **OCTOBRE).status_code, 403)

    def test_vendeur_valide_sans_boutique_404(self):
        self.assertEqual(self.lire(URL_VENDEUR, self.vendeur_sans_boutique, **OCTOBRE).status_code, 404)

    def test_lecture_seule(self):
        for url, utilisateur in ((URL_VENDEUR, self.vendeur_a), (URL_ADMIN, self.admin)):
            api = APIClient()
            api.force_authenticate(utilisateur)
            for methode in ('post', 'put', 'patch', 'delete'):
                with self.subTest(url=url, methode=methode):
                    self.assertEqual(getattr(api, methode)(url, {}, format='json').status_code, 405)


class NombreDeRequetesTests(JeuDeDonnees, AvecApi, TestCase):
    """Chaque indicateur est un agrégat SQL : le nombre de requêtes ne dépend
    pas du nombre de commandes."""

    REQUETES_VENDEUR = 8
    REQUETES_ADMINISTRATION = 6

    @classmethod
    def setUpTestData(cls):
        cls.creer_jeu()

    def mesurer(self, url, utilisateur):
        with CaptureQueriesContext(connection) as requetes:
            reponse = self.lire(url, utilisateur, **OCTOBRE)
        self.assertEqual(reponse.status_code, 200)
        return len(requetes)

    def ajouter_des_ventes(self):
        for jour in range(2, 22):
            self.commande(
                self.boutique_a, Statut.LIVREE, f'2026-10-{jour:02d}T10:00:00+00:00',
                [(1, 1000, 1, 140, 100), (3, 500, 2, 140, 200)], (Rev.VERSE, 1000, 1000),
            )
            self.commande(self.boutique_b, Statut.CONFIRMEE, f'2026-10-{jour:02d}T11:00:00+00:00', [(7, 2000, 1, 280, 200)])

    def test_vendeur(self):
        avant = self.mesurer(URL_VENDEUR, self.vendeur_a)
        self.ajouter_des_ventes()
        apres = self.mesurer(URL_VENDEUR, self.vendeur_a)
        self.assertEqual((avant, apres), (self.REQUETES_VENDEUR, self.REQUETES_VENDEUR))

    def test_administration(self):
        avant = self.mesurer(URL_ADMIN, self.admin)
        self.ajouter_des_ventes()
        apres = self.mesurer(URL_ADMIN, self.admin)
        self.assertEqual((avant, apres), (self.REQUETES_ADMINISTRATION, self.REQUETES_ADMINISTRATION))
