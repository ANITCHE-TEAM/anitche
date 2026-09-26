from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail

from .models import Livraison


@shared_task
def envoyer_code_livraison_email(livraison_id):
    """Envoie au client le code de livraison courant.

    Seul l'identifiant transite par la file de tâches : le code est relu
    (chiffré en base) au moment de l'envoi, et rien n'est envoyé si la
    livraison n'est plus en cours. Toujours envoyé, quelles que soient les
    préférences de notification : sans ce code, le colis ne peut pas être
    remis. (SMS : dette, docs/MODULE_LIVRAISON.md.)
    """
    livraison = Livraison.objects.select_related("commande__client").filter(pk=livraison_id).first()
    if livraison is None or livraison.status != Livraison.Status.EN_COURS or not livraison.code_chiffre:
        return
    commande = livraison.commande
    send_mail(
        subject=f"Votre code de livraison — commande {commande.numero_commande}",
        message=(
            f"Votre colis (commande {commande.numero_commande}) est en cours de livraison.\n\n"
            f"Code de livraison : {livraison.code_chiffre}\n\n"
            "Donnez ce code au livreur uniquement au moment où il vous remet le colis. "
            "Ne le communiquez jamais par téléphone ni avant la remise.\n"
            "Ce code est aussi visible dans l'application, dans le suivi de votre livraison."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[commande.client.email],
        fail_silently=False,
    )
