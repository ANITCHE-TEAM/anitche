"""Fiches de livraison des commandes déjà annulées : restées « en attente »
(ou avancées à la main par l'administration) avant l'introduction du
statut « annulée ». Elles passent « annulée », avec une ligne d'historique
et sans code de livraison. Une fiche déjà livrée n'est jamais touchée."""

from django.db import migrations


def annuler_fiches(apps, schema_editor):
    Livraison = apps.get_model("livraison", "Livraison")
    LivraisonHistorique = apps.get_model("livraison", "LivraisonHistorique")
    fiches = Livraison.objects.filter(commande__status="annulee").exclude(status__in=("annulee", "livree"))
    for livraison in fiches:
        LivraisonHistorique.objects.create(
            livraison=livraison,
            ancien_status=livraison.status,
            nouveau_status="annulee",
            role_acteur="systeme",
            commentaire="Commande annulée (reprise des données).",
        )
        livraison.status = "annulee"
        livraison.code_hash = ""
        livraison.code_chiffre = ""
        livraison.save(update_fields=["status", "code_hash", "code_chiffre", "updated_at"])


class Migration(migrations.Migration):

    dependencies = [
        ('livraison', '0002_annulation_code_contestation'),
        ('commandes', '0007_motif_livraison_echouee'),
    ]

    operations = [
        migrations.RunPython(annuler_fiches, migrations.RunPython.noop),
    ]
