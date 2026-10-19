"""Circuit de décision et de contestation des retours : transitions et
rôles, échéances et tâche, historique, reversement pendant le circuit,
notifications, migration (docs/MODULE_RETOURS.md)."""

import ast
import importlib
import threading
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.apps import apps as registre_apps
from django.conf import settings
from django.contrib import admin
from django.db import connection
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from django.utils.module_loading import import_string

from apps.catalogue.models import Produit, VarianteProduit
from apps.commandes.models import Commande, CommandeItem
from apps.fidelite.services import gains_echus, ouvrir_gain
from apps.livraison.models import Livraison
from apps.notifications.models import Notification
from apps.paiements import reversements
from apps.paiements.models import Paiement, Reversement
from apps.utilisateurs.models import Role, StatutKYC, Utilisateur
from apps.vendeurs.models import Boutique

from . import services
from .admin import RetourHistoriqueAdmin, RetourHistoriqueInline
from .models import DemandeRetour, RetourHistorique, RetourItem
from .signals import date_lisible, retour_status_change
from .tests import URL_RETOURS, RetoursCycleBase, image_png

Statut = DemandeRetour.Statut
Source = RetourHistorique.Source
MOTIF = "Article intact au déballage, aucun défaut visible."
CONTESTATION = "Le défaut est visible sur les photos jointes au dossier."


class CircuitBase(RetoursCycleBase):
    def setUp(self):
        super().setUp()
        self.moderateur = Utilisateur.objects.create_user(
            email="moderateur.retours@anitche.ci", password="TestPassword123!", nom="M", prenom="O",
            role=Role.MODERATEUR,
        )

    def au_statut(self, statut, commande=None, **champs):
        """Demande au statut voulu, avec les dates que le circuit y aurait posées."""
        maintenant = timezone.now()
        dates = {}
        if statut in services.STATUTS_REFUS:
            dates["date_limite_contestation"] = maintenant + timedelta(hours=72)
        if statut in (Statut.EN_MODERATION, Statut.REFUSE_MODERATION, Statut.EN_ARBITRAGE):
            dates["date_moderation"] = maintenant
        if statut == Statut.EN_ARBITRAGE:
            dates["date_arbitrage"] = maintenant
        dates.update(champs)
        demande = DemandeRetour.objects.create(
            commande=commande or self.commande1, client=self.client1, boutique=self.boutique1,
            description="Défaut constaté", montant_remboursement=Decimal("20000"),
        )
        RetourItem.objects.create(demande_retour=demande, commande_item=self.item1, quantite=1)
        DemandeRetour.objects.filter(pk=demande.pk).update(statut=statut, **dates)
        demande.refresh_from_db()
        return demande

    def statut_de(self, demande):
        demande.refresh_from_db()
        return demande.statut

    def etapes(self, demande, action=None):
        qs = RetourHistorique.objects.filter(demande_retour=demande)
        return qs.filter(action=action) if action else qs


# =====================================================================
# TRANSITIONS ET RÔLES
# =====================================================================

class CircuitTransitionsTests(CircuitBase):
    def test_refus_du_vendeur_contestable_pendant_72_heures(self):
        demande = self.au_statut(Statut.DEMANDE)
        avant = timezone.now()
        r = self.agir(demande.id, self.vendeur1, "rejeter", reponse=MOTIF)
        self.assertEqual((r.status_code, r.data["statut"]), (200, "refuse_vendeur"))
        demande.refresh_from_db()
        self.assertAlmostEqual((demande.date_limite_contestation - avant).total_seconds(), 72 * 3600, delta=5)

    def test_vendeur_ne_repond_plus_apres_son_echeance(self):
        demande = self.au_statut(Statut.DEMANDE, date_limite_vendeur=timezone.now() - timedelta(minutes=1))
        for action in ("approuver", "rejeter"):
            with self.subTest(action=action):
                r = self.agir(demande.id, self.vendeur1, action, reponse=MOTIF)
                self.assertEqual(r.status_code, 400)
                self.assertIn("modération", str(r.data))
        self.assertEqual(self.statut_de(demande), Statut.DEMANDE)

    def test_vendeur_repond_a_l_echeance_exacte(self):
        demande = self.au_statut(Statut.DEMANDE)
        with patch("apps.retours.services.timezone.now", return_value=demande.date_limite_vendeur):
            r = self.agir(demande.id, self.vendeur1, "approuver")
        self.assertEqual((r.status_code, r.data["statut"]), (200, "approuve"))

    def test_client_conteste_le_refus_du_vendeur(self):
        demande = self.au_statut(Statut.REFUSE_VENDEUR)
        r = self.agir(demande.id, self.client1, "contester", reponse=CONTESTATION)
        self.assertEqual((r.status_code, r.data["statut"]), (200, "en_moderation"))
        demande.refresh_from_db()
        self.assertIsNotNone(demande.date_moderation)
        self.assertIsNone(demande.date_limite_contestation)

    def test_contestation_apres_le_delai_refusee(self):
        demande = self.au_statut(Statut.REFUSE_VENDEUR, date_limite_contestation=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.agir(demande.id, self.client1, "contester", reponse=CONTESTATION).status_code, 400)
        self.assertEqual(self.statut_de(demande), Statut.REFUSE_VENDEUR)

    def test_contestation_sans_motif_suffisant_refusee(self):
        demande = self.au_statut(Statut.REFUSE_VENDEUR)
        for motif in ("", "   ", "Pas juste"):
            with self.subTest(motif=motif):
                self.assertEqual(self.agir(demande.id, self.client1, "contester", reponse=motif).status_code, 400)
        self.assertEqual(self.statut_de(demande), Statut.REFUSE_VENDEUR)

    def test_seul_le_client_conteste(self):
        for statut, utilisateur in ((Statut.REFUSE_VENDEUR, self.vendeur1), (Statut.REFUSE_MODERATION, self.vendeur1),
                                    (Statut.REFUSE_MODERATION, self.moderateur)):
            with self.subTest(statut=statut, utilisateur=utilisateur.email):
                demande = self.au_statut(statut)
                self.assertEqual(self.agir(demande.id, utilisateur, "contester", reponse=CONTESTATION).status_code, 403)

    def test_moderateur_decide_d_un_dossier_en_moderation(self):
        approuvee = self.au_statut(Statut.EN_MODERATION)
        r = self.agir(approuvee.id, self.moderateur, "approuver", reponse="Défaut confirmé par les photos.")
        self.assertEqual((r.status_code, r.data["statut"]), (200, "approuve"))
        refusee = self.au_statut(Statut.EN_MODERATION)
        r = self.agir(refusee.id, self.moderateur, "rejeter", reponse="Usure normale, pas un défaut.")
        self.assertEqual((r.status_code, r.data["statut"]), (200, "refuse_moderation"))
        refusee.refresh_from_db()
        self.assertIsNotNone(refusee.date_limite_contestation)

    def test_moderateur_decide_avec_un_motif(self):
        demande = self.au_statut(Statut.EN_MODERATION)
        for action in ("approuver", "rejeter"):
            with self.subTest(action=action):
                self.assertEqual(self.agir(demande.id, self.moderateur, action).status_code, 400)

    def test_moderateur_hors_de_son_dossier(self):
        for statut, attendu in ((Statut.DEMANDE, 404), (Statut.REFUSE_VENDEUR, 404), (Statut.EN_ARBITRAGE, 403)):
            demande = self.au_statut(statut)
            for action in ("approuver", "rejeter"):
                with self.subTest(statut=statut, action=action):
                    self.assertEqual(self.agir(demande.id, self.moderateur, action, reponse=MOTIF).status_code, attendu)
            self.assertEqual(self.statut_de(demande), statut)

    def test_moderateur_sans_pouvoir_operationnel(self):
        for statut, action in ((Statut.APPROUVE, "en_transit"), (Statut.EN_TRANSIT, "receptionner"),
                               (Statut.RECEPTIONNE, "rembourser"), (Statut.REMBOURSE, "cloturer"),
                               (Statut.EN_MODERATION, "annuler")):
            with self.subTest(statut=statut, action=action):
                demande = self.au_statut(statut, date_moderation=timezone.now())
                self.assertEqual(self.agir(demande.id, self.moderateur, action).status_code, 403)
                self.assertEqual(self.statut_de(demande), statut)

    def test_vendeur_ne_decide_pas_en_recours(self):
        for statut in (Statut.EN_MODERATION, Statut.EN_ARBITRAGE):
            demande = self.au_statut(statut)
            for action in ("approuver", "rejeter"):
                with self.subTest(statut=statut, action=action):
                    self.assertEqual(self.agir(demande.id, self.vendeur1, action, reponse=MOTIF).status_code, 403)
            self.assertEqual(self.statut_de(demande), statut)

    def test_arbitrage_apres_le_refus_de_la_moderation(self):
        demande = self.au_statut(Statut.REFUSE_MODERATION)
        r = self.agir(demande.id, self.client1, "contester", reponse=CONTESTATION)
        self.assertEqual((r.status_code, r.data["statut"]), (200, "en_arbitrage"))
        demande.refresh_from_db()
        self.assertIsNotNone(demande.date_arbitrage)
        r = self.agir(demande.id, self.admin, "approuver", reponse="Défaut avéré.")
        self.assertEqual((r.status_code, r.data["statut"]), (200, "approuve"))
        rejetee = self.au_statut(Statut.EN_ARBITRAGE)
        r = self.agir(rejetee.id, self.admin, "rejeter", reponse="Dommage causé par le client.")
        self.assertEqual((r.status_code, r.data["statut"]), (200, "rejete"))

    def test_administration_ne_decide_pas_au_niveau_du_vendeur(self):
        demande = self.au_statut(Statut.DEMANDE)
        for action in ("approuver", "rejeter"):
            with self.subTest(action=action):
                self.assertEqual(self.agir(demande.id, self.admin, action, reponse=MOTIF).status_code, 403)
        self.assertEqual(self.statut_de(demande), Statut.DEMANDE)

    def test_administration_n_intervient_qu_apres_la_moderation(self):
        demande = self.au_statut(Statut.EN_MODERATION)
        for action in ("approuver", "rejeter"):
            with self.subTest(action=action):
                self.assertEqual(self.agir(demande.id, self.admin, action, reponse=MOTIF).status_code, 403)
        self.assertEqual(self.statut_de(demande), Statut.EN_MODERATION)

    def test_client_annule_depuis_chaque_statut_de_decision(self):
        for statut in (Statut.DEMANDE, Statut.APPROUVE, Statut.REFUSE_VENDEUR, Statut.EN_MODERATION,
                       Statut.REFUSE_MODERATION, Statut.EN_ARBITRAGE):
            with self.subTest(statut=statut), patch("apps.retours.services.reprendre_reversement") as reprendre:
                demande = self.au_statut(statut)
                r = self.agir(demande.id, self.client1, "annuler")
                self.assertEqual((r.status_code, r.data["statut"]), (200, "annule"))
                reprendre.assert_called_once()

    def test_vendeur_revient_seulement_sur_sa_propre_approbation(self):
        demande = self.au_statut(Statut.DEMANDE)
        self.assertEqual(self.agir(demande.id, self.vendeur1, "approuver").status_code, 200)
        r = self.agir(demande.id, self.vendeur1, "rejeter", reponse=MOTIF)
        self.assertEqual((r.status_code, r.data["statut"]), (200, "refuse_vendeur"))

        moderee = self.au_statut(Statut.EN_MODERATION)
        self.assertEqual(self.agir(moderee.id, self.moderateur, "approuver", reponse="Défaut confirmé.").status_code, 200)
        self.assertEqual(self.agir(moderee.id, self.vendeur1, "rejeter", reponse=MOTIF).status_code, 403)
        self.assertEqual(self.statut_de(moderee), Statut.APPROUVE)

    def test_tiers_recoit_404_sur_toutes_les_actions(self):
        demande = self.au_statut(Statut.REFUSE_VENDEUR)
        for tiers in (self.client2, self.vendeur2, self.moderateur):
            for action in services.ACTIONS_UTILISATEUR:
                with self.subTest(tiers=tiers.email, action=action):
                    self.assertEqual(self.agir(demande.id, tiers, action, reponse=CONTESTATION).status_code, 404)

    def test_actions_systeme_fermees_a_l_api(self):
        demande = self.au_statut(Statut.DEMANDE, date_limite_vendeur=timezone.now() - timedelta(minutes=1))
        for action in ("escalader", "clore_refus"):
            with self.subTest(action=action):
                self.assertEqual(self.agir(demande.id, self.admin, action).status_code, 400)
        self.assertEqual(self.statut_de(demande), Statut.DEMANDE)

    def test_un_statut_de_depart_par_action(self):
        for action, regles in services.TRANSITIONS.items():
            departs = [statut for regle in regles for statut in regle.depuis]
            with self.subTest(action=action):
                self.assertEqual(len(departs), len(set(departs)))


class ModerationVisibiliteTests(CircuitBase):
    def test_moderateur_voit_les_dossiers_moderes_sans_email_du_client(self):
        jamais = self.au_statut(Statut.DEMANDE)
        moderee = self.au_statut(Statut.EN_MODERATION)
        self.client.force_authenticate(user=self.moderateur)
        self.assertEqual(self.client.get(f"{URL_RETOURS}{jamais.id}/").status_code, 404)
        r = self.client.get(f"{URL_RETOURS}{moderee.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.data["client_email"])
        self.client.force_authenticate(user=self.vendeur1)
        self.assertEqual(self.client.get(f"{URL_RETOURS}{moderee.id}/").data["client_email"], self.client1.email)


# =====================================================================
# ÉCHÉANCES ET TÂCHE
# =====================================================================

class EcheancesTests(CircuitBase):
    def test_echeance_du_vendeur_posee_a_la_creation(self):
        for heures in (48, 12):
            with self.subTest(heures=heures), override_settings(RETOUR_DELAI_REPONSE_VENDEUR_HEURES=heures):
                r = self.demander()
                self.assertEqual(r.status_code, 201, r.data)
                demande = DemandeRetour.objects.get(pk=r.data["id"])
                ecart = (demande.date_limite_vendeur - demande.date_creation).total_seconds()
                self.assertAlmostEqual(ecart, heures * 3600, delta=2)

    def test_escalade_apres_l_echeance(self):
        demande = self.au_statut(Statut.DEMANDE)
        resultat = services.appliquer_echeances(demande.date_limite_vendeur + timedelta(seconds=1))
        self.assertEqual(resultat[:2], (1, 0))
        demande.refresh_from_db()
        self.assertEqual(demande.statut, Statut.EN_MODERATION)
        self.assertIsNotNone(demande.date_moderation)
        etape = self.etapes(demande).get()
        self.assertEqual((etape.ancien_statut, etape.nouveau_statut, etape.action, etape.source, etape.auteur),
                         (Statut.DEMANDE, Statut.EN_MODERATION, "escalader", Source.SYSTEME, None))

    def test_rien_avant_l_echeance(self):
        demande = self.au_statut(Statut.DEMANDE)
        resultat = services.appliquer_echeances(demande.date_limite_vendeur - timedelta(hours=1))
        self.assertEqual(resultat[:2], (0, 0))
        self.assertEqual(self.statut_de(demande), Statut.DEMANDE)

    def test_sans_effet_si_la_demande_a_change_entre_temps(self):
        apres = timezone.now() + timedelta(days=30)
        for statut in (Statut.APPROUVE, Statut.EN_MODERATION, Statut.REJETE):
            with self.subTest(statut=statut):
                demande = self.au_statut(statut, date_limite_vendeur=timezone.now(),
                                         date_limite_contestation=timezone.now())
                self.assertIsNone(services.appliquer_echeance(demande.pk, apres))
                self.assertEqual(self.statut_de(demande), statut)
                self.assertFalse(self.etapes(demande).exists())

    def test_refus_non_contestes_rendus_definitifs(self):
        passe = timezone.now() - timedelta(minutes=1)
        echus = [self.au_statut(statut, date_limite_contestation=passe)
                 for statut in (Statut.REFUSE_VENDEUR, Statut.REFUSE_MODERATION)]
        en_cours = self.au_statut(Statut.REFUSE_VENDEUR)
        with patch("apps.retours.services.reprendre_reversement") as reprendre:
            self.assertEqual(services.appliquer_echeances()[:2], (0, 2))
        self.assertEqual(reprendre.call_count, 2)
        for demande in echus:
            demande.refresh_from_db()
            self.assertEqual(demande.statut, Statut.REJETE)
            self.assertIsNotNone(demande.date_cloture)
            self.assertEqual(self.etapes(demande).get().action, "clore_refus")
        self.assertEqual(self.statut_de(en_cours), Statut.REFUSE_VENDEUR)

    def test_deux_passages_une_seule_transition(self):
        demande = self.au_statut(Statut.DEMANDE)
        apres = demande.date_limite_vendeur + timedelta(seconds=1)
        self.assertIsNotNone(services.appliquer_echeance(demande.pk, apres))
        self.assertIsNone(services.appliquer_echeance(demande.pk, apres))
        self.assertEqual(services.appliquer_echeances(apres)[:2], (0, 0))
        self.assertEqual(self.etapes(demande).count(), 1)
        self.assertEqual(Notification.objects.filter(destinataire=self.vendeur1, metadata__statut="en_moderation").count(), 1)

    def test_une_demande_en_erreur_n_empeche_pas_les_suivantes(self):
        premiere, seconde = self.au_statut(Statut.DEMANDE), self.au_statut(Statut.DEMANDE)
        apres = max(premiere.date_limite_vendeur, seconde.date_limite_vendeur) + timedelta(seconds=1)
        appliquer = services._appliquer
        appels = []

        def en_erreur_une_fois(*args, **kwargs):
            appels.append(1)
            if len(appels) == 1:
                raise RuntimeError("panne simulée")
            return appliquer(*args, **kwargs)

        with patch("apps.retours.services._appliquer", side_effect=en_erreur_une_fois), \
                self.assertLogs("apps.retours.services", level="ERROR"):
            self.assertEqual(services.appliquer_echeances(apres)[:2], (1, 0))
        self.assertEqual(sorted([self.statut_de(premiere), self.statut_de(seconde)]),
                         sorted([Statut.DEMANDE, Statut.EN_MODERATION]))

    def test_tache_planifiee_toutes_les_15_minutes(self):
        entree = settings.CELERY_BEAT_SCHEDULE["retours-appliquer-echeances"]
        self.assertEqual(entree["task"], "apps.retours.tasks.appliquer_echeances_retours")
        self.assertEqual(entree["schedule"].minute, set(range(2, 60, 15)))
        self.assertEqual(import_string(entree["task"])(), "0 demande(s) escaladée(s), 0 refus clos, 0 relance(s).")

    def test_delais_par_defaut(self):
        self.assertEqual(
            (settings.RETOUR_DELAI_JOURS, settings.RETOUR_DELAI_REPONSE_VENDEUR_HEURES,
             settings.RETOUR_DELAI_CONTESTATION_HEURES),
            (7, 48, 72),
        )

    def test_rappel_au_vendeur_une_seule_fois_dans_les_24_dernieres_heures(self):
        demande = self.au_statut(Statut.DEMANDE)
        limite = demande.date_limite_vendeur
        self.assertEqual(services.appliquer_echeances(limite - timedelta(hours=25))[2], 0)
        self.assertEqual(services.appliquer_echeances(limite - timedelta(hours=23))[2], 1)
        self.assertEqual(services.appliquer_echeances(limite - timedelta(hours=22))[2], 0)
        self.assertEqual(Notification.objects.filter(destinataire=self.vendeur1, titre__startswith="Rappel").count(), 1)
        self.assertEqual(self.statut_de(demande), Statut.DEMANDE)

    def test_alerte_administration_dossier_sans_decision_apres_48_heures(self):
        for statut in (Statut.EN_MODERATION, Statut.EN_ARBITRAGE):
            with self.subTest(statut=statut):
                Notification.objects.all().delete()
                demande = self.au_statut(statut)
                entree = demande.date_arbitrage or demande.date_moderation
                self.assertEqual(services.appliquer_echeances(entree + timedelta(hours=47))[2], 0)
                self.assertEqual(services.appliquer_echeances(entree + timedelta(hours=49))[2], 1)
                self.assertEqual(services.appliquer_echeances(entree + timedelta(hours=50))[2], 0)
                self.assertEqual(Notification.objects.filter(destinataire=self.admin,
                                                             titre="Retour en attente de décision").count(), 1)
                self.assertEqual(self.statut_de(demande), statut)


class EcheancesConcurrenceTests(TransactionTestCase):
    """Le vendeur qui répond à l'échéance et la tâche qui escalade : un seul
    des deux l'emporte."""

    def setUp(self):
        self.client1 = Utilisateur.objects.create_user(email="c.ec@anitche.ci", password="TestPassword123!",
                                                       nom="C", prenom="C", role=Role.CLIENT)
        self.vendeur = Utilisateur.objects.create_user(email="v.ec@anitche.ci", password="TestPassword123!", nom="V",
                                                       prenom="V", role=Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        boutique = Boutique.objects.create(proprietaire=self.vendeur, nom="Boutique EC", est_active=True)
        produit = Produit.objects.create(boutique=boutique, nom="P", prix_base=Decimal("20000"))
        variante = VarianteProduit.objects.create(produit=produit, nom="M", prix=Decimal("20000"))
        commande = Commande.objects.create(boutique=boutique, client=self.client1, montant_total=Decimal("20000"),
                                           status=Commande.Status.LIVREE)
        CommandeItem.objects.create(commande=commande, variante=variante, nom_produit="P",
                                    prix_unitaire=Decimal("20000"), quantite=1)
        self.demande = DemandeRetour.objects.create(commande=commande, client=self.client1, boutique=boutique,
                                                    description="Défaut constaté",
                                                    montant_remboursement=Decimal("20000"))

    @skipUnless(connection.vendor == "postgresql", "select_for_update exige PostgreSQL.")
    def test_vendeur_et_tache_simultanes_une_seule_transition(self):
        apres = self.demande.date_limite_vendeur + timedelta(seconds=1)
        tracer = services._tracer

        def tracer_lentement(*args, **kwargs):
            time.sleep(0.3)  # élargit la fenêtre entre la lecture et l'écriture
            return tracer(*args, **kwargs)

        barriere = threading.Barrier(2)
        resultats = {}

        def vendeur():
            try:
                barriere.wait()
                services.traiter(self.demande.pk, self.vendeur, "approuver")
                resultats["vendeur"] = "fait"
            except services.RetourRefuse as refus:
                resultats["vendeur"] = refus.code_http
            finally:
                connection.close()

        def tache():
            try:
                barriere.wait()
                resultats["tache"] = services.appliquer_echeance(self.demande.pk, apres)
            finally:
                connection.close()

        with patch("apps.retours.services._tracer", side_effect=tracer_lentement):
            fils = [threading.Thread(target=vendeur), threading.Thread(target=tache)]
            for fil in fils:
                fil.start()
            for fil in fils:
                fil.join()

        self.demande.refresh_from_db()
        self.assertEqual(RetourHistorique.objects.filter(demande_retour=self.demande).count(), 1)
        if resultats["vendeur"] == "fait":
            self.assertEqual((self.demande.statut, resultats["tache"]), (Statut.APPROUVE, None))
        else:
            self.assertEqual((self.demande.statut, resultats["vendeur"]), (Statut.EN_MODERATION, 403))

    def test_signal_emis_apres_le_commit(self):
        dans_une_transaction = []

        def ecouteur(**kwargs):
            dans_une_transaction.append(connection.in_atomic_block)

        retour_status_change.connect(ecouteur)
        try:
            services.appliquer_echeance(self.demande.pk, self.demande.date_limite_vendeur + timedelta(seconds=1))
        finally:
            retour_status_change.disconnect(ecouteur)
        self.assertEqual(dans_une_transaction, [False])


# =====================================================================
# HISTORIQUE
# =====================================================================

class HistoriqueTests(CircuitBase):
    def test_chaque_etape_tracee(self):
        r = self.demander()
        demande = DemandeRetour.objects.get(pk=r.data["id"])
        self.agir(demande.id, self.vendeur1, "rejeter", reponse=MOTIF)
        self.agir(demande.id, self.client1, "contester", reponse=CONTESTATION)
        self.agir(demande.id, self.moderateur, "approuver", reponse="Défaut confirmé.")
        attendu = [
            ("", Statut.DEMANDE, "creer", Source.CLIENT, self.client1.pk, ""),
            (Statut.DEMANDE, Statut.REFUSE_VENDEUR, "rejeter", Source.VENDEUR, self.vendeur1.pk, MOTIF),
            (Statut.REFUSE_VENDEUR, Statut.EN_MODERATION, "contester", Source.CLIENT, self.client1.pk, CONTESTATION),
            (Statut.EN_MODERATION, Statut.APPROUVE, "approuver", Source.MODERATION, self.moderateur.pk,
             "Défaut confirmé."),
        ]
        self.assertEqual(
            list(self.etapes(demande).values_list("ancien_statut", "nouveau_statut", "action", "source",
                                                  "auteur", "commentaire")),
            attendu,
        )

    def test_action_refusee_sans_trace(self):
        demande = self.au_statut(Statut.DEMANDE, date_limite_vendeur=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.agir(demande.id, self.vendeur1, "approuver").status_code, 400)
        self.assertEqual(self.agir(demande.id, self.client1, "approuver").status_code, 403)
        self.assertFalse(self.etapes(demande).exists())

    def test_echec_apres_la_transition_n_enregistre_rien(self):
        demande = self.au_statut(Statut.RECEPTIONNE)
        with patch("apps.retours.services.rembourser_retour", side_effect=RuntimeError("panne simulée")):
            with self.assertRaises(RuntimeError):
                services.traiter(demande.pk, self.vendeur1, "rembourser")
        self.assertEqual(self.statut_de(demande), Statut.RECEPTIONNE)
        self.assertFalse(self.etapes(demande).exists())

    def test_admin_django_historique_en_lecture_seule(self):
        requete = RequestFactory().get("/")
        requete.user = Utilisateur(email="su@retours.ci", is_active=True, is_staff=True, is_superuser=True)
        for modele_admin in (admin.site._registry[RetourHistorique], RetourHistoriqueInline(DemandeRetour, admin.site)):
            with self.subTest(type(modele_admin).__name__):
                self.assertFalse(modele_admin.has_add_permission(requete))
                self.assertFalse(modele_admin.has_change_permission(requete))
                self.assertFalse(modele_admin.has_delete_permission(requete))
        self.assertIsInstance(admin.site._registry[RetourHistorique], RetourHistoriqueAdmin)


APPS = Path(__file__).resolve().parents[1]
SERVICES_RETOURS = APPS / "retours" / "services.py"
ECRITURES = {"update", "create", "get_or_create", "update_or_create", "bulk_update", "bulk_create"}


def _est_demande(noeud):
    """Un nom ou un attribut qui désigne une demande de retour."""
    nom = noeud.id if isinstance(noeud, ast.Name) else noeud.attr if isinstance(noeud, ast.Attribute) else ""
    nom = nom.lower()
    return ("demande" in nom or nom == "retour") and "historique" not in nom


def _mentionne(noeud, nom):
    return any(isinstance(n, ast.Name) and n.id == nom for n in ast.walk(noeud))


def ecritures_interdites(source):
    """Lignes de `source` qui écrivent DemandeRetour.statut ou RetourHistorique."""
    trouvees = []
    for noeud in ast.walk(ast.parse(source)):
        if isinstance(noeud, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            cibles = noeud.targets if isinstance(noeud, ast.Assign) else [noeud.target]
            if any(isinstance(c, ast.Attribute) and c.attr == "statut" and _est_demande(c.value) for c in cibles):
                trouvees.append(noeud.lineno)
        elif isinstance(noeud, ast.Call):
            fonction = noeud.func
            mots_cles = {mot.arg for mot in noeud.keywords}
            if isinstance(fonction, ast.Name) and fonction.id in ("DemandeRetour", "RetourHistorique"):
                trouvees.append(noeud.lineno)
            elif isinstance(fonction, ast.Name) and fonction.id == "setattr" and len(noeud.args) >= 2:
                cible, nom = noeud.args[:2]
                if _est_demande(cible) and isinstance(nom, ast.Constant) and nom.value == "statut":
                    trouvees.append(noeud.lineno)
            elif isinstance(fonction, ast.Attribute):
                if _mentionne(fonction.value, "RetourHistorique") and fonction.attr in ECRITURES | {"delete"}:
                    trouvees.append(noeud.lineno)
                sur_demande = _mentionne(fonction.value, "DemandeRetour") or _est_demande(fonction.value)
                if sur_demande and fonction.attr in ECRITURES:
                    cree = fonction.attr == "create" and _mentionne(fonction.value, "DemandeRetour")
                    liste = fonction.attr == "bulk_update" and "statut" in ast.dump(noeud)
                    if "statut" in mots_cles or cree or liste:
                        trouvees.append(noeud.lineno)
                if (fonction.attr == "save" and _est_demande(fonction.value)
                        and any(m.arg == "update_fields" and "statut" in ast.dump(m.value) for m in noeud.keywords)):
                    trouvees.append(noeud.lineno)
    if "retours_demanderetour" in source or "retours_retourhistorique" in source:
        trouvees.append(0)  # SQL brut sur les tables
    return sorted(set(trouvees))


class GardeFouStatutTests(TestCase):
    def fichiers_de_production(self):
        for chemin in sorted(APPS.rglob("*.py")):
            relatif = chemin.relative_to(APPS)
            if chemin == SERVICES_RETOURS or "migrations" in relatif.parts:
                continue
            if chemin.name == "tests.py" or chemin.name.startswith("test_") or chemin.name == "factories.py":
                continue
            yield chemin

    def test_aucun_code_de_production_n_ecrit_le_statut_hors_du_service(self):
        fautifs = {}
        for chemin in self.fichiers_de_production():
            lignes = ecritures_interdites(chemin.read_text(encoding="utf-8"))
            if lignes:
                fautifs[str(chemin.relative_to(APPS))] = lignes
        self.assertEqual(fautifs, {}, "Le statut d'un retour et son historique ne s'écrivent que par "
                                      "apps.retours.services.")

    def test_le_garde_fou_detecte_les_ecritures_interdites(self):
        for extrait in (
            "demande.statut = 'approuve'",
            "self.demande_retour.statut = DemandeRetour.Statut.REJETE",
            "DemandeRetour.objects.filter(pk=1).update(statut='rejete')",
            "demandes.update(statut='annule')",
            "DemandeRetour.objects.bulk_update(lot, ['statut'])",
            "DemandeRetour.objects.create(client=c)",
            "DemandeRetour(client=c)",
            "demande.save(update_fields=['statut'])",
            "setattr(demande, 'statut', 'rejete')",
            "RetourHistorique.objects.create(demande_retour=d)",
            "RetourHistorique.objects.filter(demande_retour=d).delete()",
            "cursor.execute('UPDATE retours_demanderetour SET statut = 1')",
        ):
            with self.subTest(extrait):
                self.assertTrue(ecritures_interdites(extrait), extrait)

    def test_le_garde_fou_laisse_passer_le_code_legitime(self):
        for extrait in (
            "paiement.statut = 'valide'",
            "reversement.statut = Statut.SUSPENDU",
            "Remboursement.objects.filter(retour=demande).update(statut='effectue')",
            "DemandeRetour.objects.filter(pk=1).update(date_relance=None)",
            "demande.save(update_fields=['date_relance'])",
            "RetourHistorique.objects.filter(demande_retour=demande)",
            "if demande.statut == DemandeRetour.Statut.DEMANDE: pass",
        ):
            with self.subTest(extrait):
                self.assertEqual(ecritures_interdites(extrait), [], extrait)


# =====================================================================
# REVERSEMENT ET FIDÉLITÉ PENDANT LE CIRCUIT
# =====================================================================

class ArgentCircuitTests(CircuitBase):
    def commande_payee(self):
        """Commande livrée et payée, reversement en rétractation (date de
        disponibilité dans 7 jours), une ligne de 20 000."""
        commande = Commande.objects.create(boutique=self.boutique1, client=self.client1,
                                           montant_total=Decimal("20000"), status=Commande.Status.LIVREE)
        article = CommandeItem.objects.create(commande=commande, variante=self.variante1, nom_produit="Robe",
                                              prix_unitaire=Decimal("20000"), quantite=1)
        Livraison.objects.create(commande=commande, status=Livraison.Status.LIVREE, date_livraison=timezone.now())
        paiement = Paiement.objects.create(client=self.client1, fournisseur="simule", montant=Decimal("20000"),
                                           statut=Paiement.Statut.VALIDE, date_validation=timezone.now())
        paiement.commandes.add(commande)
        reversements.creer_reversement(commande)
        reversements.ouvrir_retractation(commande)
        return commande, article

    def retour_sur(self, commande, article, statut):
        demande = DemandeRetour.objects.create(commande=commande, client=self.client1, boutique=self.boutique1,
                                               description="Défaut constaté", montant_remboursement=Decimal("20000"))
        RetourItem.objects.create(demande_retour=demande, commande_item=article, quantite=1)
        reversements.suspendre_reversement(commande)
        maintenant = timezone.now()
        DemandeRetour.objects.filter(pk=demande.pk).update(
            statut=statut, date_limite_contestation=maintenant + timedelta(hours=72), date_moderation=maintenant,
            date_arbitrage=maintenant,
        )
        return demande

    def statut_reversement(self, commande):
        return Reversement.objects.get(commande=commande).statut

    def test_refus_du_vendeur_ou_de_la_moderation_garde_le_reversement_suspendu(self):
        for statut, utilisateur in ((Statut.DEMANDE, self.vendeur1), (Statut.EN_MODERATION, self.moderateur)):
            with self.subTest(statut=statut):
                commande, article = self.commande_payee()
                demande = self.retour_sur(commande, article, statut)
                self.assertEqual(self.agir(demande.id, utilisateur, "rejeter", reponse=MOTIF).status_code, 200)
                self.assertEqual(self.statut_reversement(commande), Reversement.Statut.SUSPENDU)

    def test_decision_definitive_reprend_le_reversement(self):
        scenarios = {
            "clore_refus": lambda d: services.appliquer_echeance(d.pk, timezone.now() + timedelta(days=4)),
            "rejeter (admin)": lambda d: self.agir(d.id, self.admin, "rejeter", reponse=MOTIF),
            "annuler": lambda d: self.agir(d.id, self.client1, "annuler"),
        }
        departs = {"clore_refus": Statut.REFUSE_VENDEUR, "rejeter (admin)": Statut.EN_ARBITRAGE,
                   "annuler": Statut.EN_MODERATION}
        for nom, appliquer in scenarios.items():
            with self.subTest(nom):
                commande, article = self.commande_payee()
                demande = self.retour_sur(commande, article, departs[nom])
                self.assertEqual(self.statut_reversement(commande), Reversement.Statut.SUSPENDU)
                appliquer(demande)
                self.assertIn(self.statut_de(demande), (Statut.REJETE, Statut.ANNULE))
                self.assertEqual(self.statut_reversement(commande), Reversement.Statut.EN_RETRACTATION)

    def test_nouveaux_statuts_restent_ouverts(self):
        commande, article = self.commande_payee()
        ouvrir_gain(commande)
        plus_tard = timezone.now() + timedelta(days=8)
        demande = self.retour_sur(commande, article, Statut.DEMANDE)
        for statut in (Statut.REFUSE_VENDEUR, Statut.EN_MODERATION, Statut.REFUSE_MODERATION, Statut.EN_ARBITRAGE):
            with self.subTest(statut=statut):
                DemandeRetour.objects.filter(pk=demande.pk).update(statut=statut)
                self.assertTrue(reversements.retour_ouvert(commande))
                self.assertFalse(gains_echus(plus_tard).filter(commande=commande).exists())
        DemandeRetour.objects.filter(pk=demande.pk).update(statut=Statut.REJETE)
        self.assertTrue(gains_echus(plus_tard).filter(commande=commande).exists())


# =====================================================================
# NOTIFICATIONS ET PHOTOS
# =====================================================================

class NotificationsCircuitTests(CircuitBase):
    def notifies(self, demande):
        return set(Notification.objects.filter(metadata__retour_id=str(demande.id))
                   .values_list("destinataire__email", flat=True))

    def test_escalade_previent_client_vendeur_et_moderateurs_actifs(self):
        second = Utilisateur.objects.create_user(email="mod2.retours@anitche.ci", password="TestPassword123!",
                                                 nom="M", prenom="D", role=Role.MODERATEUR)
        Utilisateur.objects.create_user(email="mod3.retours@anitche.ci", password="TestPassword123!", nom="M",
                                        prenom="I", role=Role.MODERATEUR, is_active=False)
        demande = self.au_statut(Statut.DEMANDE)
        services.appliquer_echeances(demande.date_limite_vendeur + timedelta(seconds=1))
        self.assertEqual(self.notifies(demande), {self.client1.email, self.vendeur1.email, self.moderateur.email,
                                                  second.email})
        self.assertEqual(
            Notification.objects.get(destinataire=self.moderateur).lien_redirection,
            f"/moderation/retours/{demande.id}",
        )

    def test_contestation_vers_l_arbitrage_previent_l_administration(self):
        demande = self.au_statut(Statut.REFUSE_MODERATION)
        self.assertEqual(self.agir(demande.id, self.client1, "contester", reponse=CONTESTATION).status_code, 200)
        alerte = Notification.objects.get(destinataire=self.admin)
        self.assertEqual(alerte.lien_redirection, f"/administration/retours/{demande.id}")
        self.assertIn(CONTESTATION, alerte.message)
        self.assertNotIn(self.moderateur.email, self.notifies(demande))

    def test_contestation_vers_la_moderation_previent_les_moderateurs(self):
        demande = self.au_statut(Statut.REFUSE_VENDEUR)
        self.agir(demande.id, self.client1, "contester", reponse=CONTESTATION)
        self.assertEqual(self.notifies(demande), {self.client1.email, self.vendeur1.email, self.moderateur.email})

    def test_notification_en_echec_sans_500(self):
        demande = self.au_statut(Statut.DEMANDE)
        with patch("apps.notifications.services.ServiceNotification.notifier_utilisateur",
                   side_effect=RuntimeError("panne simulée")), self.assertLogs("apps.retours.signals", "ERROR"):
            r = self.agir(demande.id, self.vendeur1, "rejeter", reponse=MOTIF)
        self.assertEqual((r.status_code, r.data["statut"]), (200, "refuse_vendeur"))

    def test_photos_pendant_la_contestation(self):
        for statut, attendu in ((Statut.REFUSE_VENDEUR, 201), (Statut.EN_MODERATION, 201),
                                (Statut.REFUSE_MODERATION, 201), (Statut.EN_ARBITRAGE, 201), (Statut.REJETE, 400)):
            with self.subTest(statut=statut):
                demande = self.au_statut(statut)
                self.client.force_authenticate(user=self.client1)
                r = self.client.post(f"{URL_RETOURS}{demande.id}/photos/", {"image": image_png()}, format="multipart")
                self.assertEqual(r.status_code, attendu)


# =====================================================================
# MIGRATION DES DEMANDES EXISTANTES
# =====================================================================

MIGRATION_ECHEANCES = importlib.import_module("apps.retours.migrations.0006_echeances_demandes_existantes")


class MigrationEcheancesTests(CircuitBase):
    @skipUnless(connection.vendor == "postgresql", "DDL transactionnel de PostgreSQL.")
    def test_echeance_comptee_a_partir_de_la_migration(self):
        contrainte = next(c for c in DemandeRetour._meta.constraints
                          if c.name == "retour_demande_avec_echeance_vendeur")
        ancienne = self.au_statut(Statut.DEMANDE)
        rejetee = self.au_statut(Statut.REJETE)
        # État d'avant la migration : pas d'échéance (contrainte retirée le
        # temps du test, dans la transaction du test ; les clés étrangères
        # différées sont vérifiées d'abord, sinon PostgreSQL refuse l'ALTER).
        with connection.cursor() as curseur:
            curseur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        with connection.schema_editor() as editeur:
            editeur.remove_constraint(DemandeRetour, contrainte)
        DemandeRetour.objects.filter(pk__in=(ancienne.pk, rejetee.pk)).update(
            date_creation=timezone.now() - timedelta(days=10), date_limite_vendeur=None,
        )
        avant = timezone.now()
        MIGRATION_ECHEANCES.poser_echeances(registre_apps, None)
        with connection.schema_editor() as editeur:
            editeur.add_constraint(DemandeRetour, contrainte)
        ancienne.refresh_from_db()
        self.assertGreaterEqual(ancienne.date_limite_vendeur, avant + timedelta(hours=48))
        rejetee.refresh_from_db()
        self.assertIsNone(rejetee.date_limite_vendeur)
        self.assertEqual(self.agir(rejetee.id, self.client1, "contester", reponse=CONTESTATION).status_code, 400)
