from rest_framework import serializers

from .services import PERIODE_MAX_JOURS, Periode, periode_par_defaut


class FiltrePeriodeSerializer(serializers.Serializer):
    """`date_debut` et `date_fin` (AAAA-MM-JJ), jours entiers inclus en
    Africa/Abidjan. Sans borne : les 30 derniers jours, aujourd'hui compris."""

    date_debut = serializers.DateField(required=False, help_text="Premier jour inclus. Défaut : 29 jours avant `date_fin`.")
    date_fin = serializers.DateField(required=False, help_text="Dernier jour inclus. Défaut : aujourd'hui (Abidjan).")

    def validate(self, attrs):
        periode = periode_par_defaut(attrs.get('date_debut'), attrs.get('date_fin'))
        if periode.date_fin < periode.date_debut:
            raise serializers.ValidationError({'date_fin': "`date_fin` doit être postérieure ou égale à `date_debut`."})
        if (periode.date_fin - periode.date_debut).days >= PERIODE_MAX_JOURS:
            raise serializers.ValidationError(
                {'date_fin': f"La période ne peut pas dépasser {PERIODE_MAX_JOURS} jours."}
            )
        return {'periode': periode}


def lire_periode(query_params):
    filtre = FiltrePeriodeSerializer(data=query_params)
    filtre.is_valid(raise_exception=True)
    return filtre.validated_data['periode']


# ---------------------------------------------------------------------
# Schéma OpenAPI des réponses
# ---------------------------------------------------------------------

class PeriodeSerializer(serializers.Serializer):
    date_debut = serializers.DateField()
    date_fin = serializers.DateField()

    @classmethod
    def depuis(cls, periode: Periode):
        return cls({'date_debut': periode.date_debut, 'date_fin': periode.date_fin}).data


class CommandesParStatutSerializer(serializers.Serializer):
    creee = serializers.IntegerField()
    confirmee = serializers.IntegerField()
    preparation = serializers.IntegerField()
    expediee = serializers.IntegerField()
    livree = serializers.IntegerField()
    annulee = serializers.IntegerField()


class TopProduitSerializer(serializers.Serializer):
    produit = serializers.IntegerField()
    nom = serializers.CharField()
    quantite = serializers.IntegerField()
    ventes = serializers.IntegerField(help_text="Ventes brutes (XOF) des commandes comptées.")


class AvisBoutiqueSerializer(serializers.Serializer):
    note_moyenne = serializers.FloatField(allow_null=True, help_text="`null` sans avis publié dans la période.")
    nombre = serializers.IntegerField()


class ReversementsVendeurSerializer(serializers.Serializer):
    verses = serializers.IntegerField(help_text="Montants versés au vendeur (XOF).")
    a_venir = serializers.IntegerField(help_text="Montants nets pas encore versés (XOF).")


class ReversementsAdministrationSerializer(serializers.Serializer):
    en_cours = serializers.IntegerField(help_text="Montants nets pas encore versés (XOF).")
    verses = serializers.IntegerField(help_text="Montants versés aux vendeurs (XOF).")


class StatistiquesVendeurSerializer(serializers.Serializer):
    periode = PeriodeSerializer()
    chiffre_affaires = serializers.IntegerField(help_text="Ventes brutes moins les retours remboursés (XOF).")
    montant_retours = serializers.IntegerField(help_text="Valeur des articles retournés et remboursés, déduite (XOF).")
    nombre_commandes = serializers.IntegerField(help_text="Commandes comptées : payées, non annulées.")
    commandes_par_statut = CommandesParStatutSerializer(help_text="Toutes les commandes créées dans la période.")
    panier_moyen = serializers.IntegerField(help_text="Ventes brutes ÷ commandes comptées (XOF).")
    top_produits = TopProduitSerializer(many=True)
    avis = AvisBoutiqueSerializer()
    taux_retour = serializers.FloatField(help_text="Pourcentage de commandes livrées avec un retour accepté.")
    reversements = ReversementsVendeurSerializer()


class StatistiquesAdministrationSerializer(serializers.Serializer):
    periode = PeriodeSerializer()
    volume_ventes = serializers.IntegerField(help_text="Ventes brutes moins les retours remboursés (XOF).")
    montant_retours = serializers.IntegerField(help_text="Valeur des articles retournés et remboursés, déduite (XOF).")
    commissions = serializers.IntegerField(
        help_text="Commissions et frais fixes des articles vendus, moins la commission rendue sur les retours (XOF).",
    )
    nombre_commandes = serializers.IntegerField(help_text="Commandes comptées : payées, non annulées.")
    commandes_par_statut = CommandesParStatutSerializer(help_text="Toutes les commandes créées dans la période.")
    nouveaux_clients = serializers.IntegerField()
    vendeurs_actifs = serializers.IntegerField(help_text="Boutiques avec au moins une commande comptée.")
    reversements = ReversementsAdministrationSerializer()
    avis_signales_en_attente = serializers.IntegerField(help_text="Signalements ouverts, sans filtre de période.")
