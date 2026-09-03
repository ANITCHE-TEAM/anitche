# 🛡️ Rapport d'Audit & Renforcement du Backend ANITCHE

Ce document présente l'audit approfondi de sécurité, de robustesse transactionnelle, de performance et de résilience réalisé sur les deux composantes backend (**Django 6.0 REST API** et **FastAPI Microservice**).

---

## 1. Synthèse de l'Audit

| Domaine | État Avant Audit | Actions de Renforcement Appliquées | Statut |
| :--- | :--- | :--- | :---: |
| **Sécurité HTTP & Headers** | Headers minimaux par défaut | Activation de `SECURE_BROWSER_XSS_FILTER`, `SECURE_CONTENT_TYPE_NOSNIFF`, `X_FRAME_OPTIONS = 'DENY'`, HSTS 1 an (`SECURE_HSTS_SECONDS = 31536000`), `Referrer-Policy: strict-origin-when-cross-origin`. | 🟢 **Sécurisé** |
| **Protection Anti-Bruteforce & Throttling** | Throttling limité à l'OTP/Login | Mise en place de `AnonRateThrottle` (100/min), `UserRateThrottle` (1000/min), et scopes dédiés pour `paiements` (30/min), `support` (20/min), `otp` (5/min). | 🟢 **Sécurisé** |
| **Validation des Fichiers & Uploads** | Formats non strictement bornés | Création de `ValidateurFichierSecurise` dans [`apps/core/validators.py`](file:///c:/Users/Jordan/Documents/Anitche/backend-django/apps/core/validators.py) avec contrôle strict des extensions, tailles (5 Mo/10 Mo) et MIME. | 🟢 **Sécurisé** |
| **Gestion des Exceptions & Fuite d'Infos** | Tracebacks bruts possibles en 500 | `custom_exception_handler` dans [`config/exceptions.py`](file:///c:/Users/Jordan/Documents/Anitche/backend-django/config/exceptions.py) et `global_exception_handler` FastAPI masquant les traces internes et unifiant les erreurs. | 🟢 **Sécurisé** |
| **Intégrité Concurrente & Verrous SQL** | Risque de double-dépense stock | Verrouillage pessimiste `select_for_update()` dans `apps/commandes/views.py` et décrémentations atomiques `F('quantite_disponible') - qte` dans `apps/catalogue/models.py`. | 🟢 **Sécurisé** |
| **Idempotence & Anti-Rejeu Paiements** | Risque de callbacks dupliqués | Table d'audit `JournalWebhook` avec contrainte d'unicité `(fournisseur, evenement_id)` empêchant tout double crédit ou double validation de commande. | 🟢 **Sécurisé** |
| **Observabilité & Télémétrie Microservice** | Latence non mesurée | Ajout du middleware `ProcessTimeMiddleware` injectant `X-Process-Time-Ms` sur chaque réponse FastAPI. | 🟢 **Optimisé** |

---

## 2. Détail des Renforcements Appliqués

### 🔒 A. Sécurité HTTP & Protection des Cookies
- **Django (`config/settings/base.py` & `prod.py`)** :
  ```python
  SECURE_BROWSER_XSS_FILTER = True
  SECURE_CONTENT_TYPE_NOSNIFF = True
  X_FRAME_OPTIONS = 'DENY'
  SECURE_SSL_REDIRECT = True
  SESSION_COOKIE_SECURE = True
  CSRF_COOKIE_SECURE = True
  SESSION_COOKIE_HTTPONLY = True
  CSRF_COOKIE_HTTPONLY = True
  SECURE_HSTS_SECONDS = 31536000
  SECURE_HSTS_INCLUDE_SUBDOMAINS = True
  SECURE_HSTS_PRELOAD = True
  ```
- **FastAPI (`app/main.py`)** :
  - Ajout du middleware `SecurityHeadersMiddleware` garantissant les en-têtes `X-Content-Type-Options`, `X-Frame-Options`, `X-XSS-Protection` et `Referrer-Policy`.

### ⏱️ B. Throttling & Protection contre le Déni de Service
- **Django REST Framework** :
  ```python
  'DEFAULT_THROTTLE_CLASSES': (
      'rest_framework.throttling.AnonRateThrottle',
      'rest_framework.throttling.UserRateThrottle',
  ),
  'DEFAULT_THROTTLE_RATES': {
      'anon': '100/minute',
      'user': '1000/minute',
      'otp': '5/minute',
      'login': '10/minute',
      'paiements': '30/minute',
      'support': '20/minute',
  }
  ```

### 📁 C. Filtrage d'Uploads & Protection contre les Fichiers Malveillants
- Validateur universel `ValidateurFichierSecurise` protégeant :
  - Les pièces jointes des tickets support (`TicketAttachment`).
  - Les preuves de retours marchandises (`PhotoRetour`).
  - Les photos de catalogue (`ImageProduit`).
  - Les documents d'identité KYC (`DocumentKYC`).

---

## 3. Validation Globale des Tests de Non-Régression

```text
======================================================================
1. BACKEND DJANGO (12 MODULES)
python manage.py test --settings=config.settings.test
Ran 173 tests in 3.516s -> OK (0 erreur, 0 échec)

2. MICROSERVICE FASTAPI (4 SERVICES)
python -m pytest -v
13 passed in 1.04s -> OK (0 erreur, 0 échec)
======================================================================
TOTAL : 186 TESTS AUTOMATISÉS RÉUSSIS À 100%
======================================================================
```
