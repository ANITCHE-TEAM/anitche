"""Génère les miniatures manquantes des images produit (images envoyées
avant l'ajout des miniatures, ou dont la génération avait échoué).

    python manage.py generer_miniatures
    python manage.py generer_miniatures --lot 50

Idempotente : seules les images sans miniature sont traitées. Par lots,
dans l'ordre des identifiants : la mémoire reste bornée quel que soit le
nombre d'images, et une image qui échoue n'est pas retentée dans la même
exécution.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.catalogue.images import generer_miniature
from apps.catalogue.models import ImageProduit


class Command(BaseCommand):
    help = "Génère les miniatures WebP manquantes des images produit."

    def add_arguments(self, parser):
        parser.add_argument("--lot", type=int, default=100, help="Images lues par requête (défaut : 100).")

    def handle(self, *args, lot=100, **options):
        if lot < 1:
            raise CommandError("--lot doit être au moins 1.")
        sans_miniature = ImageProduit.objects.filter(Q(miniature__isnull=True) | Q(miniature=""))
        dernier, generees, echecs = 0, 0, 0
        while True:
            images = list(sans_miniature.filter(pk__gt=dernier).order_by("pk")[:lot])
            if not images:
                break
            for image in images:
                dernier = image.pk
                if generer_miniature(image):
                    generees += 1
                else:
                    echecs += 1
        self.stdout.write(f"{generees} miniature(s) générée(s), {echecs} échec(s).")
