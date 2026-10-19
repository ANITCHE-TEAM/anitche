"""Règles communes aux adresses de livraison : celle saisie au checkout
(apps.commandes) et celles du carnet d'adresses (apps.utilisateurs)."""

import math
import re
from decimal import ROUND_HALF_UP, Decimal

from rest_framework import serializers

# Point de livraison : territoire de la Côte d'Ivoire avec une marge
# (environ 4,3 à 10,7° N et 8,6 à 2,5° O).
LATITUDE_MIN, LATITUDE_MAX = 4.0, 11.0
LONGITUDE_MIN, LONGITUDE_MAX = -9.0, -2.0
# 6 décimales : environ 11 cm, la précision des colonnes qui stockent un point.
PRECISION_COORDONNEE = Decimal("0.000001")


class CoordonneeGpsField(serializers.FloatField):
    """Latitude ou longitude en degrés décimaux (celles de
    navigator.geolocation) : un nombre JSON, jamais une chaîne ni un
    booléen, fini, entre min_value et max_value. Renvoie un Decimal arrondi
    à 6 décimales."""

    default_error_messages = {
        "invalid": "Un nombre est attendu (ex. 5.359952), pas une chaîne.",
        "non_fini": "Valeur non finie refusée (NaN, Infinity).",
        "min_value": "Position hors de la Côte d'Ivoire : valeur minimale {min_value}.",
        "max_value": "Position hors de la Côte d'Ivoire : valeur maximale {max_value}.",
    }

    def to_internal_value(self, data):
        # bool est un int en Python : true n'est pas une coordonnée.
        if isinstance(data, bool) or not isinstance(data, (int, float)):
            self.fail("invalid")
        if isinstance(data, float) and not math.isfinite(data):
            self.fail("non_fini")
        # str() garde la valeur écrite dans le JSON (5.3, pas 5.2999…).
        valeur = Decimal(str(data))
        # Bornes contrôlées avant l'arrondi : quantize() échouerait sur un
        # entier démesuré (1e40). Les validateurs de FloatField, qui portent
        # les bornes dans le schéma OpenAPI, repassent ensuite sans effet.
        if valeur < self.min_value:
            self.fail("min_value", min_value=self.min_value)
        if valeur > self.max_value:
            self.fail("max_value", max_value=self.max_value)
        return valeur.quantize(PRECISION_COORDONNEE, rounding=ROUND_HALF_UP)


def champ_latitude():
    return CoordonneeGpsField(
        min_value=LATITUDE_MIN, max_value=LATITUDE_MAX, required=False, allow_null=True,
        help_text="Facultatif, avec `longitude`. Degrés décimaux, arrondis à 6 décimales.",
    )


def champ_longitude():
    return CoordonneeGpsField(
        min_value=LONGITUDE_MIN, max_value=LONGITUDE_MAX, required=False, allow_null=True,
        help_text="Facultatif, avec `latitude`. Degrés décimaux, arrondis à 6 décimales.",
    )


def valider_telephone(telephone):
    """Numéro joignable pour une livraison : 8 à 15 chiffres, « + » initial
    accepté, espaces, points et tirets retirés. Renvoie le numéro compact."""
    compact = re.sub(r"[\s.\-]", "", telephone)
    if not re.fullmatch(r"\+?\d{8,15}", compact):
        raise serializers.ValidationError("Numéro de téléphone invalide (8 à 15 chiffres, « + » initial accepté).")
    return compact


def valider_position_complete(attrs):
    """Un point GPS est complet ou absent : jamais une latitude sans longitude."""
    latitude, longitude = attrs.get("latitude"), attrs.get("longitude")
    if (latitude is None) != (longitude is None):
        manquante = "longitude" if longitude is None else "latitude"
        raise serializers.ValidationError(
            {manquante: "La latitude et la longitude vont ensemble : envoyez les deux, ou aucune."}
        )
    return attrs
