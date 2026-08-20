from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/utilisateurs/', include('apps.utilisateurs.urls')),
    path('api/vendeurs/', include('apps.vendeurs.urls')),
    path("api/support/", include("apps.support.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    # Le if settings.DEBUG est important : c'est justement pourquoi dev.py met DEBUG = True — ça n'active ce mécanisme qu'en développement. En prod (DEBUG = False), ce bloc ne s'exécute jamais, ce qui est voulu.