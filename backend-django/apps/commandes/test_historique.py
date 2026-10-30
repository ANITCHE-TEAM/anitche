"""Historique des statuts de commande (règle de gestion §6).

Chaque point d'entrée qui change le statut d'une commande produit sa ligne
CommandeHistorique ; un garde-fou statique échoue si du code de production
écrit `Commande.status` (ou l'historique) ailleurs que dans
apps.commandes.services.
"""
import ast
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib import admin
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.livraison.models import Livraison
from apps.paiements.models import Paiement
from apps.paiements.services import valider_paiement
from apps.utilisateurs.models import Role, Utilisateur

from .admin import CommandeHistoriqueAdmin, CommandeHistoriqueInline
from .models import Commande, CommandeHistorique
from .services import (
    TRANSITIONS,
    TransitionImpossible,
    annuler_commande,
    changer_statut_commande,
    expirer_commandes_impayees,
)
from .tests import DonneesCycleDeVie

Statut = Commande.Status
Source = CommandeHistorique.Source

APPS = Path(__file__).resolve().parents[1]
SERVICES_COMMANDES = APPS / "commandes" / "services.py"


class HistoriqueTestCase(DonneesCycleDeVie, APITestCase):
    def setUp(self):
        self.creer_donnees()
        self.commander((self.variante1, 1))
        self.commande = Commande.objects.get()
        self.livreur = Utilisateur.objects.create_user(
            email="livreur@hist.ci", password="x", nom="L", prenom="Ivreur", role=Role.LIVREUR,
        )

    def lignes(self, commande=None):
        return list(
            CommandeHistorique.objects.filter(commande=commande or self.commande)
            .values_list("ancien_statut", "nouveau_statut", "source", "motif")
        )

    def derniere(self):
        return CommandeHistorique.objects.filter(commande=self.commande).last()

    def assertHistoriqueCoherent(self, commande=None):
        """Chaque ligne part du statut atteint par la précédente, et la dernière
        est le statut actuel."""
        commande = commande or self.commande
        commande.refresh_from_db()
        lignes = list(CommandeHistorique.objects.filter(commande=commande))
        self.assertTrue(lignes)
        self.assertEqual(lignes[0].ancien_statut, "")
        for precedente, suivante in zip(lignes, lignes[1:]):
            self.assertEqual(suivante.ancien_statut, precedente.nouveau_statut)
        self.assertEqual(lignes[-1].nouveau_statut, commande.status)

    def payer_et_valider(self):
        """Paiement validé par le fournisseur : confirme la commande et crée sa livraison."""
        paiement = self.payer(self.commande, statut=Paiement.Statut.EN_ATTENTE)
        self.assertTrue(valider_paiement(paiement))
        return paiement

    def preparer(self):
        self.en_tant_que(self.vendeur1)
        return self.client.post(f"/api/commandes/vendeur/{self.commande.pk}/preparation/")

    def changer_livraison(self, statut):
        livraison = Livraison.objects.get(commande=self.commande)
        self.en_tant_que(self.livreur)
        return self.client.patch(
            f"/api/livraison/{livraison.pk}/statut/", {"status": statut, "code": livraison.code_chiffre},
        )

    def assigner_livreur(self):
        Livraison.objects.filter(commande=self.commande).update(livreur=self.livreur)


class PointsDEntreeTests(HistoriqueTestCase):
    """Un test par endroit où le statut d'une commande change."""

    def test_creation_au_checkout(self):
        self.assertEqual(self.lignes(), [("", "creee", "client", "")])
        self.assertEqual(self.derniere().auteur, self.client_user)

    def test_creation_d_un_checkout_multi_boutiques(self):
        self.commander((self.variante1, 1), (self.variante2, 1))
        for commande in Commande.objects.exclude(pk=self.commande.pk):
            self.assertEqual(self.lignes(commande), [("", "creee", "client", "")])

    def test_paiement_valide_confirme(self):
        self.payer_et_valider()
        self.assertEqual(self.lignes()[1:], [("creee", "confirmee", "paiement", "")])
        self.assertIsNone(self.derniere().auteur)

    def test_notification_de_paiement_rejouee_n_ajoute_rien(self):
        paiement = self.payer_et_valider()
        self.assertFalse(valider_paiement(paiement))
        self.assertEqual(len(self.lignes()), 2)

    def test_vendeur_passe_en_preparation(self):
        self.payer_et_valider()
        self.assertEqual(self.preparer().status_code, 200)
        self.assertEqual(self.lignes()[2:], [("confirmee", "preparation", "vendeur", "")])
        self.assertEqual(self.derniere().auteur, self.vendeur1)

    def test_livraison_expediee_puis_livree(self):
        self.payer_et_valider()
        self.preparer()
        self.assigner_livreur()
        self.assertEqual(self.changer_livraison("expediee").status_code, 200)
        self.assertEqual(self.changer_livraison("en_cours").status_code, 200)
        self.assertEqual(self.changer_livraison("livree").status_code, 200)
        self.assertEqual(self.lignes()[3:], [
            ("preparation", "expediee", "livraison", ""),
            ("expediee", "livree", "livraison", ""),
        ])
        self.assertEqual(self.derniere().auteur, self.livreur)
        self.assertHistoriqueCoherent()

    def test_annulation_par_le_client(self):
        self.en_tant_que(self.client_user)
        self.assertEqual(self.client.post(f"/api/commandes/{self.commande.pk}/annuler/").status_code, 200)
        self.assertEqual(self.lignes()[1:], [("creee", "annulee", "client", "client")])
        self.assertEqual(self.derniere().auteur, self.client_user)

    def test_annulation_par_l_administration(self):
        self.payer_et_valider()
        self.preparer()
        self.en_tant_que(self.admin)
        r = self.client.post(f"/api/commandes/administration/{self.commande.pk}/annuler/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.lignes()[3:], [("preparation", "annulee", "admin", "administration")])
        self.assertEqual(self.derniere().auteur, self.admin)

    def test_expiration_des_commandes_impayees(self):
        Commande.objects.filter(pk=self.commande.pk).update(created_at=timezone.now() - timedelta(minutes=31))
        self.assertEqual(expirer_commandes_impayees(), 1)
        self.assertEqual(self.lignes()[1:], [("creee", "annulee", "systeme", "expiration")])
        self.assertIsNone(self.derniere().auteur)

    def test_boutique_indisponible_au_paiement(self):
        from apps.vendeurs.models import Boutique

        Boutique.objects.filter(pk=self.boutique1.pk).update(est_suspendue=True)
        self.en_tant_que(self.client_user)
        r = self.client.post(
            "/api/paiements/initier/", {"commande_id": str(self.commande.pk), "methode": "wave"}, format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.lignes()[1:], [("creee", "annulee", "systeme", "boutique_indisponible")])
        self.assertIsNone(self.derniere().auteur)

    def test_abandon_de_la_livraison_echouee(self):
        self.payer_et_valider()
        self.preparer()
        self.assigner_livreur()
        self.changer_livraison("expediee")
        self.changer_livraison("en_cours")
        echec = self.client.patch(
            f"/api/livraison/{Livraison.objects.get(commande=self.commande).pk}/statut/",
            {"status": "echouee", "commentaire": "Client injoignable"},
        )
        self.assertEqual(echec.status_code, 200)
        self.en_tant_que(self.admin)
        r = self.client.post(
            f"/api/livraison/{Livraison.objects.get(commande=self.commande).pk}/abandonner/",
            {"commentaire": "Trois échecs"}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.lignes()[-1], ("expediee", "annulee", "admin", "livraison_echouee"))
        self.assertEqual(self.derniere().auteur, self.admin)
        self.assertHistoriqueCoherent()

    def test_parcours_complet_coherent(self):
        self.payer_et_valider()
        self.preparer()
        self.assigner_livreur()
        for statut in ("expediee", "en_cours", "livree"):
            self.changer_livraison(statut)
        self.assertEqual([ligne[1] for ligne in self.lignes()],
                         ["creee", "confirmee", "preparation", "expediee", "livree"])
        self.assertHistoriqueCoherent()


class FonctionUniqueTests(HistoriqueTestCase):
    def forcer(self, statut):
        Commande.objects.filter(pk=self.commande.pk).update(status=statut)
        self.commande.refresh_from_db()

    def test_la_table_des_transitions_couvre_tous_les_statuts(self):
        self.assertEqual(set(TRANSITIONS), set(Statut.values))

    def test_chaque_transition_permise_ecrit_une_ligne(self):
        for ancien, suivants in TRANSITIONS.items():
            for nouveau in suivants:
                with self.subTest(ancien=ancien, nouveau=nouveau):
                    self.forcer(ancien)
                    avant = CommandeHistorique.objects.count()
                    changer_statut_commande(self.commande, nouveau, set(Statut), source=Source.SYSTEME)
                    self.assertEqual(CommandeHistorique.objects.count(), avant + 1)
                    ligne = self.derniere()
                    self.assertEqual((ligne.ancien_statut, ligne.nouveau_statut), (ancien, nouveau))
                    self.assertEqual(self.commande.status, nouveau)

    def test_une_transition_refusee_n_ecrit_rien(self):
        # Y compris vers le statut déjà atteint.
        for ancien, suivants in TRANSITIONS.items():
            for nouveau in set(Statut.values) - set(suivants):
                with self.subTest(ancien=ancien, nouveau=nouveau):
                    self.forcer(ancien)
                    avant = CommandeHistorique.objects.count()
                    with self.assertRaises(TransitionImpossible):
                        changer_statut_commande(self.commande, nouveau, set(Statut), source=Source.SYSTEME)
                    self.assertEqual(CommandeHistorique.objects.count(), avant)
                    self.assertEqual(self.commande.status, ancien)

    def test_la_ligne_part_du_statut_reel_meme_si_l_instance_est_perimee(self):
        instance = Commande.objects.get(pk=self.commande.pk)
        Commande.objects.filter(pk=self.commande.pk).update(status=Statut.CONFIRMEE)
        changer_statut_commande(instance, Statut.PREPARATION, {Statut.CONFIRMEE}, source=Source.VENDEUR)
        self.assertEqual(self.lignes()[-1][:2], ("confirmee", "preparation"))

    def test_double_annulation_n_ajoute_qu_une_ligne(self):
        self.en_tant_que(self.client_user)
        url = f"/api/commandes/{self.commande.pk}/annuler/"
        self.assertEqual(self.client.post(url).status_code, 200)
        self.assertEqual(self.client.post(url).status_code, 409)
        self.assertEqual(len(self.lignes()), 2)

    def test_la_ligne_est_annulee_avec_le_reste_de_l_operation(self):
        with mock.patch("apps.paiements.services.traiter_paiements_apres_annulation", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                annuler_commande(self.commande, Commande.MotifAnnulation.CLIENT, acteur=self.client_user)
        self.commande.refresh_from_db()
        self.assertEqual(self.commande.status, Statut.CREEE)
        self.assertEqual(len(self.lignes()), 1)
        self.assertHistoriqueCoherent()


class LectureTests(HistoriqueTestCase):
    def setUp(self):
        super().setUp()
        self.autre_vendeur = self.vendeur2
        self.payer_et_valider()
        self.preparer()
        self.url_client = f"/api/commandes/{self.commande.pk}/historique/"
        self.url_vendeur = f"/api/commandes/vendeur/{self.commande.pk}/historique/"
        self.url_admin = f"/api/commandes/administration/{self.commande.pk}/historique/"

    def get(self, utilisateur, url):
        self.en_tant_que(utilisateur)
        return self.client.get(url)

    def test_le_client_voit_l_historique_de_sa_commande(self):
        r = self.get(self.client_user, self.url_client)
        self.assertEqual(r.status_code, 200)
        lignes = r.data["results"]
        self.assertEqual([(ligne["ancien_statut"], ligne["nouveau_statut"], ligne["source"]) for ligne in lignes], [
            ("", "creee", "client"), ("creee", "confirmee", "paiement"), ("confirmee", "preparation", "vendeur"),
        ])

    def test_le_client_ne_voit_aucune_identite(self):
        self.en_tant_que(self.admin)
        self.client.post(f"/api/commandes/administration/{self.commande.pk}/annuler/")
        for utilisateur, url in ((self.client_user, self.url_client), (self.vendeur1, self.url_vendeur)):
            with self.subTest(utilisateur.email):
                r = self.get(utilisateur, url)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.data["results"][-1]["source"], "admin")
                for ligne in r.data["results"]:
                    self.assertNotIn("auteur", ligne)
                contenu = str(r.data)
                for secret in (self.admin.email, self.vendeur1.email, self.client_user.email):
                    self.assertNotIn(secret, contenu)

    def test_un_autre_client_n_a_pas_acces(self):
        self.assertEqual(self.get(self.autre_client, self.url_client).status_code, 404)

    def test_le_vendeur_voit_l_historique_de_sa_commande(self):
        r = self.get(self.vendeur1, self.url_vendeur)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data["results"]), 3)

    def test_un_autre_vendeur_n_a_pas_acces(self):
        self.assertEqual(self.get(self.autre_vendeur, self.url_vendeur).status_code, 404)

    def test_l_administration_voit_tout_avec_l_auteur(self):
        r = self.get(self.admin, self.url_admin)
        self.assertEqual(r.status_code, 200)
        auteurs = [ligne["auteur"] for ligne in r.data["results"]]
        self.assertEqual(auteurs, [self.client_user.pk, None, self.vendeur1.pk])

    def test_routes_reservees_a_leur_role(self):
        for utilisateur, url in (
            (self.client_user, self.url_vendeur), (self.client_user, self.url_admin),
            (self.vendeur1, self.url_admin), (self.admin, self.url_vendeur),
        ):
            with self.subTest(utilisateur.email, url=url):
                self.assertEqual(self.get(utilisateur, url).status_code, 403)

    def test_le_vendeur_et_l_administration_ne_passent_pas_par_la_route_client(self):
        for utilisateur in (self.vendeur1, self.admin):
            with self.subTest(utilisateur.email):
                self.assertEqual(self.get(utilisateur, self.url_client).status_code, 404)

    def test_anonyme_refuse(self):
        self.client.force_authenticate(None)
        for url in (self.url_client, self.url_vendeur, self.url_admin):
            with self.subTest(url):
                self.assertEqual(self.client.get(url).status_code, 401)

    def test_sans_n_plus_1(self):
        self.en_tant_que(self.admin)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(self.url_admin)
        self.assertLessEqual(len(ctx.captured_queries), 6)


class AdminDjangoTests(HistoriqueTestCase):
    def test_historique_en_lecture_seule_et_jamais_supprimable(self):
        requete = RequestFactory().get("/")
        requete.user = Utilisateur(email="su@hist.ci", is_active=True, is_staff=True, is_superuser=True)
        for modele_admin in (
            admin.site._registry[CommandeHistorique],
            CommandeHistoriqueInline(Commande, admin.site),
        ):
            with self.subTest(type(modele_admin).__name__):
                self.assertFalse(modele_admin.has_add_permission(requete))
                self.assertFalse(modele_admin.has_change_permission(requete))
                self.assertFalse(modele_admin.has_delete_permission(requete))
        self.assertIsInstance(admin.site._registry[CommandeHistorique], CommandeHistoriqueAdmin)


# =====================================================================
# GARDE-FOU : AUCUNE ÉCRITURE DU STATUT EN DEHORS DU SERVICE
# =====================================================================

ECRITURES_QUI_PRENNENT_UN_STATUT = {
    "update", "create", "get_or_create", "update_or_create", "bulk_update", "bulk_create",
}


def _est_commande(noeud):
    """Un nom ou un attribut qui désigne une commande (`commande`, `livraison.commande`)."""
    if isinstance(noeud, ast.Name):
        return "commande" in noeud.id.lower() and "historique" not in noeud.id.lower()
    if isinstance(noeud, ast.Attribute):
        return "commande" in noeud.attr.lower() and "historique" not in noeud.attr.lower()
    return False


def _mentionne_le_modele(noeud, nom):
    return any(isinstance(n, ast.Name) and n.id == nom for n in ast.walk(noeud))


def ecritures_interdites(source):
    """Lignes de `source` qui écrivent `Commande.status` ou CommandeHistorique."""
    arbre = ast.parse(source)
    trouvees = []
    for noeud in ast.walk(arbre):
        if isinstance(noeud, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            cibles = noeud.targets if isinstance(noeud, ast.Assign) else [noeud.target]
            if any(isinstance(c, ast.Attribute) and c.attr == "status" and _est_commande(c.value) for c in cibles):
                trouvees.append(noeud.lineno)
        elif isinstance(noeud, ast.Call):
            fonction = noeud.func
            mots_cles = {mot.arg for mot in noeud.keywords}
            if isinstance(fonction, ast.Name) and fonction.id in ("Commande", "CommandeHistorique"):
                trouvees.append(noeud.lineno)  # instanciation directe
            elif isinstance(fonction, ast.Name) and fonction.id == "setattr" and len(noeud.args) >= 2:
                cible, nom = noeud.args[:2]
                if _est_commande(cible) and isinstance(nom, ast.Constant) and nom.value == "status":
                    trouvees.append(noeud.lineno)
            elif isinstance(fonction, ast.Attribute):
                sur_historique = _mentionne_le_modele(fonction.value, "CommandeHistorique")
                if sur_historique and fonction.attr in ECRITURES_QUI_PRENNENT_UN_STATUT | {"delete"}:
                    trouvees.append(noeud.lineno)
                sur_commande = _mentionne_le_modele(fonction.value, "Commande") or _est_commande(fonction.value)
                if sur_commande and fonction.attr in ECRITURES_QUI_PRENNENT_UN_STATUT:
                    # Les colonnes autres que le statut (montants, motif…) restent libres.
                    cree_une_commande = fonction.attr == "create" and _mentionne_le_modele(fonction.value, "Commande")
                    liste_de_champs = fonction.attr == "bulk_update" and "status" in ast.dump(noeud)
                    if "status" in mots_cles or cree_une_commande or liste_de_champs:
                        trouvees.append(noeud.lineno)
                if (fonction.attr == "save" and _est_commande(fonction.value)
                        and any(mot.arg == "update_fields" and "status" in ast.dump(mot.value) for mot in noeud.keywords)):
                    trouvees.append(noeud.lineno)
    if "commandes_commande" in source or "commandes_commandehistorique" in source:
        trouvees.append(0)  # SQL brut sur les tables
    return sorted(set(trouvees))


class GardeFouStatutTests(APITestCase):
    def fichiers_de_production(self):
        for chemin in sorted(APPS.rglob("*.py")):
            relatif = chemin.relative_to(APPS)
            if chemin == SERVICES_COMMANDES or "migrations" in relatif.parts:
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
        self.assertEqual(
            fautifs, {},
            "Le statut d'une commande et son historique ne s'écrivent que par "
            "apps.commandes.services (changer_statut_commande, creer_commande).",
        )

    def test_le_garde_fou_detecte_les_ecritures_interdites(self):
        for extrait in (
            "commande.status = 'livree'",
            "livraison.commande.status = Commande.Status.LIVREE",
            "Commande.objects.filter(pk=1).update(status='livree')",
            "Commande.objects.filter(pk=1).update(**{'status': 'x'}, status='y')",
            "commandes.update(status='annulee')",
            "Commande.objects.bulk_update(lot, ['status'])",
            "Commande.objects.create(client=c, status='livree')",
            "Commande.objects.create(client=c)",
            "Commande(client=c)",
            "commande.save(update_fields=['status'])",
            "setattr(commande, 'status', 'livree')",
            "CommandeHistorique.objects.create(commande=c)",
            "CommandeHistorique.objects.filter(commande=c).delete()",
            "cursor.execute('UPDATE commandes_commande SET status = 1')",
        ):
            with self.subTest(extrait):
                self.assertTrue(ecritures_interdites(extrait), extrait)

    def test_le_garde_fou_laisse_passer_le_code_legitime(self):
        for extrait in (
            "livraison.status = 'livree'",
            "ticket.status = Status.IN_PROGRESS",
            "Livraison.objects.filter(commande=commande).update(status='annulee')",
            "Commande.objects.filter(pk=1).update(montant_total=1)",
            "commande.save(update_fields=['montant_total'])",
            "CommandeHistorique.objects.filter(commande=commande)",
            "if commande.status == Commande.Status.LIVREE: pass",
        ):
            with self.subTest(extrait):
                self.assertEqual(ecritures_interdites(extrait), [], extrait)
