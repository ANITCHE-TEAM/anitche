"""Fiches de livraison orphelines : fiche non terminée (ni livrée ni
annulée) rattachée à une commande annulée. Une commande annulée ne doit
avoir aucune fiche active ; annuler_commande annule la sienne, y compris
celle qu'une validation de paiement concurrente crée pendant l'annulation
(relue après l'UPDATE de la commande). Aucune action de l'API ne permet
d'annuler une fiche orpheline déjà en base : cette reprise s'en charge.

Même règle que 0003 : chaque fiche orpheline passe « annulée », avec une
ligne d'historique « systeme » et sans code de livraison. Idempotente :
rejouée, elle ne trouve plus rien.

Retour arrière : sans effet (noop). L'état « en attente » d'une fiche
orpheline n'a rien à restaurer, et l'historique garde la trace de la
reprise."""

from django.db import migrations


def annuler_fiches_orphelines(apps, schema_editor):
    Livraison = apps.get_model("livraison", "Livraison")
    LivraisonHistorique = apps.get_model("livraison", "LivraisonHistorique")
    fiches = Livraison.objects.filter(commande__status="annulee").exclude(status__in=("annulee", "livree"))
    for livraison in fiches:
        LivraisonHistorique.objects.create(
            livraison=livraison,
            ancien_status=livraison.status,
            nouveau_status="annulee",
            role_acteur="systeme",
            commentaire="Commande annulée (reprise des données : fiche orpheline).",
        )
        livraison.status = "annulee"
        livraison.code_hash = ""
        livraison.code_chiffre = ""
        livraison.save(update_fields=["status", "code_hash", "code_chiffre", "updated_at"])


class Migration(migrations.Migration):

    dependencies = [
        ('livraison', '0005_communes_du_district_d_abidjan'),
        ('commandes', '0009_position_livraison'),
    ]

    operations = [
        migrations.RunPython(annuler_fiches_orphelines, migrations.RunPython.noop),
    ]
