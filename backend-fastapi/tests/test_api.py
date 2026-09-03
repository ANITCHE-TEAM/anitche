import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "version" in data
    # Vérification des headers de sécurité et de latence
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert "X-Process-Time-Ms" in response.headers


def test_root():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "documentation" in data
    assert "endpoints" in data
    assert response.headers.get("X-Content-Type-Options") == "nosniff"



# ==========================================
# 1. Tests Moteur de Recherche
# ==========================================

def test_recherche_produits_catalogue_complet():
    response = client.get("/recherche/produits")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 5
    assert len(data["resultats"]) > 0
    assert "facettes" in data
    assert len(data["facettes"]["categories"]) > 0


def test_recherche_produits_filtre_mot_cle():
    response = client.get("/recherche/produits?q=baoule")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 2
    for p in data["resultats"]:
        texte = f"{p['nom']} {p['description']} {p['categorie_nom']}".lower()
        assert "baoulé" in texte or "baoule" in texte


def test_recherche_produits_filtre_prix_et_tri():
    response = client.get("/recherche/produits?prix_max=20000&tri=prix_asc")
    assert response.status_code == 200
    data = response.json()
    prix_list = [p["prix"] for p in data["resultats"]]
    assert all(p <= 20000.0 for p in prix_list)
    assert prix_list == sorted(prix_list)


def test_recherche_suggestions_autocompletion():
    response = client.get("/recherche/suggestions?q=wax")
    assert response.status_code == 200
    data = response.json()
    assert data["requete"] == "wax"
    assert len(data["suggestions"]) > 0
    assert any("wax" in s["texte"].lower() for s in data["suggestions"])


# ==========================================
# 2. Tests Conseiller Shopping IA
# ==========================================

def test_ia_conseil_mariage_ceremonie():
    payload = {
        "messages": [
            {"role": "user", "contenu": "Je cherche une tenue d'apparat pour un mariage traditionnel à Yamoussoukro."}
        ],
        "occasion": "mariage traditionnel",
        "budget_max": 50000.0,
    }
    response = client.post("/ia/conseil", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert len(data["produits_suggeres"]) > 0
    assert len(data["conseils_style"]) > 0
    # Vérification de suggestion du pagne Baoulé
    noms = [p["nom"].lower() for p in data["produits_suggeres"]]
    assert any("baoulé" in n or "baoule" in n for n in noms)


def test_ia_recommandations_personnalisees():
    payload = {
        "categories_preferees": ["Artisanat & Déco", "Bijoux & Parures"],
        "budget_max": 40000.0,
    }
    response = client.post("/ia/recommandations", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert len(data["recommandations"]) > 0
    assert all(r["prix"] <= 40000.0 for r in data["recommandations"])


# ==========================================
# 3. Tests Scan & Certification QR
# ==========================================

def test_scan_qr_code_direct_valide():
    payload = {"qr_data": "PAS-2026-TIASSALE01"}
    response = client.post("/qr/scan", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["valide"] is True
    assert data["code_passeport"] == "PAS-2026-TIASSALE01"
    assert data["produit_nom"] == "Robe Baoulé Traditionnelle"
    assert data["statut_certification"] == "certifie_authentique"
    assert data["nb_scans"] >= 13


def test_scan_qr_url_complete():
    payload = {"qr_data": "https://anitche.ci/qr/verifier/PAS-2026-BASSAM02"}
    response = client.post("/qr/scan", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["valide"] is True
    assert data["code_passeport"] == "PAS-2026-BASSAM02"
    assert data["boutique_nom"] == "Maroquinerie Bassam"


def test_scan_qr_code_invalide():
    payload = {"qr_data": "QR-INVALIDE-RANDOM"}
    response = client.post("/qr/scan", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["valide"] is False


def test_consulter_passeport_get():
    response = client.get("/qr/passeport/PAS-2026-MASQUE03")
    assert response.status_code == 200
    data = response.json()
    assert data["valide"] is True
    assert data["produit_nom"] == "Masque Baoulé en Bois d'Iroko"


# ==========================================
# 4. Tests Suivi GPS & Télémétrie Livreur
# ==========================================

def test_mise_a_jour_position_gps_et_consultation():
    livraison_id = "550e8400-e29b-41d4-a716-446655440000"
    payload = {
        "livraison_id": livraison_id,
        "livreur_id": 42,
        "latitude": 5.3350,
        "longitude": -4.0020,
        "vitesse_kmh": 32.5,
        "cap_degres": 120.0,
    }

    # 1. Envoi de la position par le livreur
    response_post = client.post("/livraison/position", json=payload)
    assert response_post.status_code == 200
    data_post = response_post.json()
    assert data_post["livraison_id"] == livraison_id
    assert data_post["statut"] == "en_route"
    assert data_post["distance_restante_km"] > 0
    assert data_post["temps_estime_minutes"] > 0

    # 2. Consultation de la position par le client
    response_get = client.get(f"/livraison/position/{livraison_id}")
    assert response_get.status_code == 200
    data_get = response_get.json()
    assert data_get["latitude"] == 5.3350
    assert data_get["longitude"] == -4.0020
    assert data_get["livreur_id"] == 42
