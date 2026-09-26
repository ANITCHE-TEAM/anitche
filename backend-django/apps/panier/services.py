"""Opérations sur le panier.

Centralisées ici pour que l'API panier et le checkout (module commandes)
partagent exactement les mêmes règles : un seul panier par compte ou par
session, une seule ligne par variante, pas d'ajout d'un article indisponible.
"""

from django.db import transaction
from rest_framework.exceptions import ValidationError

from .models import Panier, PanierItem


def get_panier_existant(request, queryset=None):
    """Cherche un panier existant SANS jamais en créer un nouveau.

    Utilisée pour toute lecture (GET) : consulter un panier ne doit
    jamais avoir d'effet de bord en base. Retourne None si aucun panier
    n'existe encore pour cet utilisateur/cette session.
    """
    paniers = queryset if queryset is not None else Panier.objects.all()
    if request.user.is_authenticated:
        return paniers.filter(utilisateur=request.user).first()

    session_key = request.session.session_key
    if not session_key:
        return None
    return paniers.filter(session_key=session_key).first()


def get_or_create_panier(request):
    """Récupère le panier de l'utilisateur connecté, ou celui du visiteur
    anonyme via sa session. Crée le panier s'il n'existe pas encore.

    Réservée aux actions d'écriture réelles (ajouter un article) : sans
    cette distinction, les vues en AllowAny créaient un nouveau Panier en
    base sur un simple GET, y compris pour un client qui ne conserve pas
    ses cookies (bot, script sans session persistée) — une ligne Panier
    orpheline à chaque requête, sans limite (A04:2025 — Unrestricted
    Resource Consumption).

    CONCURRENCE : les contraintes uniques de Panier garantissent un seul
    panier par compte/session ; en cas de création simultanée, get_or_create
    rattrape l'IntegrityError et relit le panier créé par l'autre requête.
    """
    if request.user.is_authenticated:
        panier, _ = Panier.objects.get_or_create(utilisateur=request.user)
        return panier

    if not request.session.session_key:
        request.session.create()

    panier, _ = Panier.objects.get_or_create(
        session_key=request.session.session_key
    )
    return panier


def _verifier_stock(variante, quantite):
    stock = getattr(variante, 'stock', None)
    if stock is None or not stock.est_en_stock(quantite):
        disponible = stock.quantite_disponible if stock else 0
        raise ValidationError(
            f"Stock insuffisant : {disponible} disponible(s), {quantite} demandé(s)."
        )


def _verifier_disponibilite(ligne):
    motif = ligne.motif_indisponibilite
    if motif:
        raise ValidationError(
            {"variante": [f"Cet article n'est plus disponible à la vente. {motif}"]}
        )


def ajouter_article(panier, variante, quantite):
    """Ajoute `quantite` unités de `variante`, ou incrémente la ligne existante.

    Retourne la ligne créée ou mise à jour. CONCURRENCE : le verrou sur la
    ligne Panier sérialise les ajouts d'un même panier — deux ajouts
    simultanés de la même variante ne créent plus deux lignes, et la
    quantité cumulée est vérifiée contre le stock sur une valeur à jour.
    """
    with transaction.atomic():
        Panier.objects.select_for_update().filter(pk=panier.pk).exists()

        _verifier_disponibilite(PanierItem(panier=panier, variante=variante))

        ligne = (
            PanierItem.objects.select_for_update()
            .filter(panier=panier, variante=variante)
            .first()
        )
        quantite_totale = quantite + (ligne.quantite if ligne else 0)
        _verifier_stock(variante, quantite_totale)

        if ligne:
            ligne.quantite = quantite_totale
            ligne.save(update_fields=['quantite'])
            return ligne
        return PanierItem.objects.create(panier=panier, variante=variante, quantite=quantite)


def modifier_quantite(ligne, nouvelle_quantite):
    """Change la quantité d'une ligne existante.

    Une baisse est toujours permise, sans contrôle : elle rapproche le panier
    d'un état valide (le checkout revérifie le stock sous verrou). Une hausse
    exige que l'article soit encore disponible à la vente, puis que le stock
    couvre la nouvelle quantité.
    """
    with transaction.atomic():
        ligne = PanierItem.objects.avec_details().select_for_update(of=('self',)).get(pk=ligne.pk)

        if nouvelle_quantite > ligne.quantite:
            _verifier_disponibilite(ligne)
            _verifier_stock(ligne.variante, nouvelle_quantite)

        ligne.quantite = nouvelle_quantite
        ligne.save(update_fields=['quantite'])
        return ligne
