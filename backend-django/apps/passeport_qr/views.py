from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny

from .models import PasseportProduit
from .serializers import (
    PasseportPublicSerializer,
    PasseportVendeurSerializer,
    CreerPasseportSerializer,
)
from apps.utilisateurs.models import Role


class PasseportPublicVerificationView(APIView):
    """Consultation publique et vérification d'authenticité d'un produit via son code passeport."""

    permission_classes = [AllowAny]

    def get(self, request, code_passeport):
        code = code_passeport.strip().upper()
        passeport = PasseportProduit.objects.filter(code_passeport=code, est_actif=True).select_related("produit", "variante", "boutique").first()

        if not passeport:
            return Response(
                {"detail": f"Passeport numérique introuvable pour le code '{code}'."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Enregistrement du scan
        ip = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR"))
        user_agent = request.META.get("HTTP_USER_AGENT", "")
        passeport.enregistrer_scan(ip=ip, user_agent=user_agent)

        return Response(PasseportPublicSerializer(passeport).data, status=status.HTTP_200_OK)


class PasseportVendeurListCreateView(APIView):
    """Espace vendeur : lister et créer les passeports numériques des produits de sa boutique."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if user.role in (Role.ADMIN, Role.SUPER_ADMIN):
            qs = PasseportProduit.objects.all().select_related("produit", "variante", "boutique")
        else:
            qs = PasseportProduit.objects.filter(boutique__proprietaire=user).select_related("produit", "variante", "boutique")

        return Response(PasseportVendeurSerializer(qs, many=True).data, status=status.HTTP_200_OK)

    def post(self, request):
        serializer = CreerPasseportSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        passeport = PasseportProduit.objects.create(
            produit=data["_produit"],
            variante=data["_variante"],
            boutique=data["_boutique"],
            numero_lot=data["numero_lot"],
            origine_geographique=data["origine_geographique"],
            materiaux_utilises=data["materiaux_utilises"],
            date_fabrication=data.get("date_fabrication"),
            artisan_createur=data["artisan_createur"],
            statut_certification=data["statut_certification"],
        )

        return Response(PasseportVendeurSerializer(passeport).data, status=status.HTTP_201_CREATED)


class PasseportVendeurDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Détail, mise à jour ou désactivation d'un passeport par le vendeur propriétaire."""

    permission_classes = [IsAuthenticated]
    serializer_class = PasseportVendeurSerializer

    def get_queryset(self):
        user = self.request.user
        if user.role in (Role.ADMIN, Role.SUPER_ADMIN):
            return PasseportProduit.objects.all().select_related("produit", "variante", "boutique")
        return PasseportProduit.objects.filter(boutique__proprietaire=user).select_related("produit", "variante", "boutique")
