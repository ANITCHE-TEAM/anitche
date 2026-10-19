import uuid
from decimal import Decimal

from rest_framework.test import APIClient, APITestCase

from apps.livraison.models import Livraison
from apps.panier.models import Panier, PanierItem
from apps.utilisateurs.models import AdresseClient, Role, Utilisateur

from .models import Commande, GroupeCommande
from .tests import ADRESSE_LIVRAISON, FRAIS_ABIDJAN, URL_SIMULER, URL_VALIDER, DonneesCycleDeVie

URL_CARNET = "/api/utilisateurs/adresses/"

ADRESSE_CARNET = {
    "libelle": "Maison",
    "destinataire": "Awa Kouassi",
    "telephone": "0707070707",
    "commune": "Cocody",
    "quartier": "Riviera 2",
    "point_de_repere": "Face au carrefour de la pharmacie",
    "indications": "Portail bleu, sonner deux fois",
}
POSITION = {"latitude": Decimal("5.359952"), "longitude": Decimal("-3.986912")}


class DonneesCarnet(DonneesCycleDeVie):
    def creer_carnet(self):
        self.creer_donnees()
        self.adresse = AdresseClient.objects.create(client=self.client_user, **ADRESSE_CARNET, **POSITION)
        self.adresse_etrangere = AdresseClient.objects.create(
            client=self.autre_client, **{**ADRESSE_CARNET, "libelle": "Chez l'autre", "quartier": "Plateau"},
        )

    def remplir_panier(self, client=None, quantite=1):
        panier, _ = Panier.objects.get_or_create(utilisateur=client or self.client_user)
        PanierItem.objects.create(panier=panier, variante=self.variante1, quantite=quantite)

    def valider(self, corps, client=None):
        api = APIClient()
        api.force_authenticate(client or self.client_user)
        return api.post(URL_VALIDER, corps, format="json")

    def aucune_ecriture(self):
        self.assertFalse(Commande.objects.exists())
        self.assertFalse(GroupeCommande.objects.exists())
        self.assertEqual(self.stock(self.variante1), 10)


class CheckoutAvecAdresseDuCarnetTests(DonneesCarnet, APITestCase):
    def setUp(self):
        self.creer_carnet()

    def test_la_commande_recopie_l_adresse_du_carnet(self):
        self.remplir_panier()
        reponse = self.valider({"adresse_id": str(self.adresse.pk)})
        self.assertEqual(reponse.status_code, 201, reponse.data)
        groupe = Commande.objects.get().groupe
        self.assertEqual(groupe.livraison_commune, "Cocody")
        self.assertEqual(groupe.livraison_quartier, "Riviera 2")
        self.assertEqual(groupe.livraison_point_de_repere, "Face au carrefour de la pharmacie")
        self.assertEqual(groupe.livraison_telephone, "0707070707")
        self.assertEqual(groupe.livraison_destinataire, "Awa Kouassi")
        self.assertEqual(groupe.livraison_indications, "Portail bleu, sonner deux fois")
        self.assertEqual((groupe.livraison_latitude, groupe.livraison_longitude), (POSITION["latitude"], POSITION["longitude"]))

    def test_les_montants_sont_ceux_de_la_commune_de_l_adresse(self):
        self.remplir_panier()
        self.valider({"adresse_id": str(self.adresse.pk)})
        commande = Commande.objects.get()
        self.assertEqual(commande.frais_livraison, FRAIS_ABIDJAN)
        self.assertEqual(commande.groupe.livraison_zone, "abidjan")

    def test_le_groupe_n_a_aucun_lien_vers_le_carnet(self):
        champs = {champ.name for champ in GroupeCommande._meta.get_fields()}
        self.assertFalse({"adresse", "adresse_id", "adresse_client"} & champs)

    def test_adresse_d_un_autre_client_404_sans_aucune_ecriture(self):
        self.remplir_panier()
        reponse = self.valider({"adresse_id": str(self.adresse_etrangere.pk)})
        self.assertEqual(reponse.status_code, 404)
        self.aucune_ecriture()
        self.assertEqual(PanierItem.objects.count(), 1)

    def test_adresse_inconnue_404(self):
        self.remplir_panier()
        self.assertEqual(self.valider({"adresse_id": str(uuid.uuid4())}).status_code, 404)
        self.aucune_ecriture()

    def test_identifiant_mal_forme_400(self):
        self.remplir_panier()
        for valeur in ("pas-un-uuid", ["x"], {"a": 1}, None, ""):
            with self.subTest(valeur):
                self.assertEqual(self.valider({"adresse_id": valeur}).status_code, 400)
        self.aucune_ecriture()

    def test_adresse_id_et_adresse_saisie_ensemble_refuses(self):
        self.remplir_panier()
        reponse = self.valider({"adresse_id": str(self.adresse.pk), "adresse_livraison": ADRESSE_LIVRAISON})
        self.assertEqual(reponse.status_code, 400)
        self.assertIn("adresse_id", reponse.data["errors"])
        self.aucune_ecriture()

    def test_ni_adresse_id_ni_adresse_saisie_refuses(self):
        self.remplir_panier()
        self.assertEqual(self.valider({}).status_code, 400)
        self.aucune_ecriture()

    def test_la_simulation_accepte_adresse_id(self):
        self.remplir_panier()
        api = APIClient()
        api.force_authenticate(self.client_user)
        avec_carnet = api.post(URL_SIMULER, {"adresse_id": str(self.adresse.pk)}, format="json")
        saisie = api.post(URL_SIMULER, {"adresse_livraison": {**ADRESSE_LIVRAISON, "commune": "Cocody"}}, format="json")
        self.assertEqual(avec_carnet.status_code, 200, avec_carnet.data)
        self.assertEqual(avec_carnet.data, saisie.data)

    def test_la_simulation_ne_lit_pas_l_adresse_d_un_autre(self):
        self.remplir_panier()
        api = APIClient()
        api.force_authenticate(self.client_user)
        self.assertEqual(api.post(URL_SIMULER, {"adresse_id": str(self.adresse_etrangere.pk)}, format="json").status_code, 404)
        both = api.post(URL_SIMULER, {"adresse_id": str(self.adresse.pk), "adresse_livraison": ADRESSE_LIVRAISON}, format="json")
        self.assertEqual(both.status_code, 400)


class CheckoutSansCarnetTests(DonneesCarnet, APITestCase):
    """Le client qui n'a aucune adresse enregistrée commande comme avant."""

    def setUp(self):
        self.creer_donnees()

    def test_adresse_saisie_sans_carnet_reste_valide(self):
        self.assertFalse(AdresseClient.objects.exists())
        self.remplir_panier()
        reponse = self.valider({"adresse_livraison": ADRESSE_LIVRAISON})
        self.assertEqual(reponse.status_code, 201, reponse.data)
        groupe = Commande.objects.get().groupe
        self.assertEqual(groupe.livraison_quartier, "Angré 8e Tranche")
        self.assertEqual((groupe.livraison_destinataire, groupe.livraison_indications), ("", ""))
        self.assertFalse(AdresseClient.objects.exists())

    def test_l_adresse_saisie_peut_porter_destinataire_et_indications(self):
        self.remplir_panier()
        adresse = {**ADRESSE_LIVRAISON, "destinataire": "Kofi Yao", "indications": "3e étage"}
        self.assertEqual(self.valider({"adresse_livraison": adresse}).status_code, 201)
        groupe = Commande.objects.get().groupe
        self.assertEqual((groupe.livraison_destinataire, groupe.livraison_indications), ("Kofi Yao", "3e étage"))

    def test_une_adresse_saisie_n_ecrit_rien_dans_le_carnet(self):
        AdresseClient.objects.create(client=self.client_user, **ADRESSE_CARNET)
        self.remplir_panier()
        self.valider({"adresse_livraison": ADRESSE_LIVRAISON})
        self.assertEqual(AdresseClient.objects.count(), 1)


class CopieFigeeTests(DonneesCarnet, APITestCase):
    """La commande garde l'adresse telle qu'elle était au checkout."""

    def setUp(self):
        self.creer_carnet()
        self.remplir_panier()
        self.assertEqual(self.valider({"adresse_id": str(self.adresse.pk)}).status_code, 201)
        self.commande = Commande.objects.get()
        self.api = APIClient()
        self.api.force_authenticate(self.client_user)

    def adresse_de_la_commande(self):
        return self.api.get(f"/api/commandes/{self.commande.pk}/").data["adresse_livraison"]

    def test_modifier_l_adresse_ne_change_pas_la_commande(self):
        avant = self.adresse_de_la_commande()
        modification = self.api.patch(
            f"{URL_CARNET}{self.adresse.pk}/",
            {"commune": "Marcory", "quartier": "Zone 4", "telephone": "0505050505", "destinataire": "Autre",
             "indications": "Autre", "point_de_repere": "Ailleurs", "latitude": 5.30, "longitude": -4.00},
            format="json",
        )
        self.assertEqual(modification.status_code, 200, modification.data)
        self.assertEqual(self.adresse_de_la_commande(), avant)
        self.assertEqual(avant["quartier"], "Riviera 2")
        self.assertEqual(avant["destinataire"], "Awa Kouassi")
        groupe = Commande.objects.get().groupe
        self.assertEqual((groupe.livraison_commune, groupe.livraison_telephone), ("Cocody", "0707070707"))

    def test_supprimer_l_adresse_ne_change_pas_la_commande(self):
        avant = self.adresse_de_la_commande()
        self.assertEqual(self.api.delete(f"{URL_CARNET}{self.adresse.pk}/").status_code, 204)
        self.assertFalse(AdresseClient.objects.filter(pk=self.adresse.pk).exists())
        self.assertEqual(self.adresse_de_la_commande(), avant)
        self.assertEqual(Commande.objects.get().groupe.livraison_quartier, "Riviera 2")

    def test_vider_tout_le_carnet_laisse_la_commande_lisible(self):
        AdresseClient.objects.filter(client=self.client_user).delete()
        self.assertEqual(self.adresse_de_la_commande()["commune"], "Cocody")

    def test_une_commande_suivante_utilise_l_adresse_modifiee(self):
        self.api.patch(f"{URL_CARNET}{self.adresse.pk}/", {"quartier": "Zone 4"}, format="json")
        self.remplir_panier()
        self.assertEqual(self.valider({"adresse_id": str(self.adresse.pk)}).status_code, 201)
        quartiers = sorted(Commande.objects.values_list("groupe__livraison_quartier", flat=True))
        self.assertEqual(quartiers, ["Riviera 2", "Zone 4"])

    def test_le_client_voit_destinataire_et_indications_pas_le_libelle(self):
        adresse = self.adresse_de_la_commande()
        self.assertEqual(adresse["destinataire"], "Awa Kouassi")
        self.assertEqual(adresse["indications"], "Portail bleu, sonner deux fois")
        self.assertNotIn("libelle", adresse)
        self.assertNotIn("Maison", self.api.get(f"/api/commandes/{self.commande.pk}/").content.decode())


class LivreurEtVendeurTests(DonneesCarnet, APITestCase):
    """Le livreur lit la copie de la commande ; le carnet reste au client."""

    def setUp(self):
        self.creer_carnet()
        self.remplir_panier()
        self.valider({"adresse_id": str(self.adresse.pk)})
        self.commande = Commande.objects.get()
        self.livreur = Utilisateur.objects.create_user(
            email="livreur@carnet.ci", password="x", nom="L", prenom="Ivreur", role=Role.LIVREUR,
        )
        self.livraison = Livraison.objects.create(commande=self.commande, adresse_livraison="x", livreur=self.livreur)
        self.api_livreur = APIClient()
        self.api_livreur.force_authenticate(self.livreur)

    def lire_livraison(self):
        reponse = self.api_livreur.get(f"/api/livraison/{self.livraison.pk}/")
        self.assertEqual(reponse.status_code, 200, reponse.content)
        return reponse

    def test_le_livreur_voit_la_copie_de_la_commande(self):
        adresse = self.lire_livraison().data["adresse"]
        self.assertEqual(adresse["quartier"], "Riviera 2")
        self.assertEqual(adresse["destinataire"], "Awa Kouassi")
        self.assertEqual(adresse["indications"], "Portail bleu, sonner deux fois")
        self.assertEqual(adresse["telephone"], "0707070707")
        self.assertEqual((adresse["latitude"], adresse["longitude"]), (5.359952, -3.986912))

    def test_le_livreur_voit_toujours_la_copie_apres_modification_et_suppression(self):
        avant = self.lire_livraison().data["adresse"]
        client = APIClient()
        client.force_authenticate(self.client_user)
        client.patch(f"{URL_CARNET}{self.adresse.pk}/", {"quartier": "Zone 4", "telephone": "0101010101"}, format="json")
        self.assertEqual(self.lire_livraison().data["adresse"], avant)
        client.delete(f"{URL_CARNET}{self.adresse.pk}/")
        self.assertEqual(self.lire_livraison().data["adresse"], avant)

    def test_la_livraison_n_expose_ni_le_libelle_ni_l_identifiant_du_carnet(self):
        contenu = self.lire_livraison().content.decode()
        self.assertNotIn(str(self.adresse.pk), contenu)
        self.assertNotIn("Maison", contenu)
        self.assertNotIn("libelle", contenu)

    def test_le_livreur_n_a_aucun_acces_au_carnet_d_un_client(self):
        self.assertEqual(self.api_livreur.get(f"{URL_CARNET}{self.adresse.pk}/").status_code, 404)
        self.assertEqual(self.api_livreur.patch(f"{URL_CARNET}{self.adresse.pk}/", {"quartier": "X"}, format="json").status_code, 404)
        self.assertEqual(self.api_livreur.delete(f"{URL_CARNET}{self.adresse.pk}/").status_code, 404)
        self.assertEqual(self.api_livreur.get(URL_CARNET).data["results"], [])
        self.adresse.refresh_from_db()
        self.assertEqual(self.adresse.quartier, "Riviera 2")

    def test_le_vendeur_ne_voit_ni_destinataire_ni_indications_ni_carnet(self):
        api = APIClient()
        api.force_authenticate(self.vendeur1)
        detail = api.get(f"/api/commandes/vendeur/{self.commande.pk}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["adresse_livraison"]["quartier"], "Riviera 2")
        contenu = detail.content.decode()
        for fragment in ("destinataire", "indications", "Portail bleu", "Awa Kouassi", "Maison"):
            self.assertNotIn(fragment, contenu)
        self.assertEqual(api.get(f"{URL_CARNET}{self.adresse.pk}/").status_code, 404)
