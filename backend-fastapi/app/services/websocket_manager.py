import math
import json
from datetime import datetime
from typing import Dict, List, Any
from fastapi import WebSocket
from app.modeles.suivi_temps_reel import PositionGPS, ReponsePositionLivreur


def calculer_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcule la distance géodésique en kilomètres entre deux points GPS (Formule de Haversine)."""
    r = 6371.0  # Rayon moyen de la Terre en km
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(r * c, 2)


class GestionnaireSuiviTempsReel:
    """Gestionnaire de connexions WebSockets et télémétrie GPS des livreurs."""

    def __init__(self):
        # livraison_id -> liste de WebSockets clients abonnés
        self.connexions_actives: Dict[str, List[WebSocket]] = {}
        # livraison_id -> dernière position GPS connue
        self.dernieres_positions: Dict[str, Dict[str, Any]] = {}

    async def connecter(self, livraison_id: str, websocket: WebSocket):
        await websocket.accept()
        if livraison_id not in self.connexions_actives:
            self.connexions_actives[livraison_id] = []
        self.connexions_actives[livraison_id].append(websocket)

        # Envoi immédiat de la dernière position si disponible
        if livraison_id in self.dernieres_positions:
            await websocket.send_text(json.dumps(self.dernieres_positions[livraison_id]))

    def deconnecter(self, livraison_id: str, websocket: WebSocket):
        if livraison_id in self.connexions_actives:
            if websocket in self.connexions_actives[livraison_id]:
                self.connexions_actives[livraison_id].remove(websocket)
            if not self.connexions_actives[livraison_id]:
                del self.connexions_actives[livraison_id]

    async def diffuser_position(self, position: PositionGPS) -> ReponsePositionLivreur:
        # Coordonnées cible de livraison fictive (Abidjan Cocody Ambassades : 5.3421, -3.9876)
        lat_dest, lon_dest = 5.3421, -3.9876
        dist_restante = calculer_distance_km(position.latitude, position.longitude, lat_dest, lon_dest)

        # Estimation du temps : vitesse moyenne 25 km/h en ville
        vitesse = position.vitesse_kmh if position.vitesse_kmh and position.vitesse_kmh > 5.0 else 25.0
        temps_estime_min = max(2, int((dist_restante / vitesse) * 60))

        maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        donnees_position = {
            "type": "mise_a_jour_position",
            "livraison_id": position.livraison_id,
            "livreur_id": position.livreur_id,
            "latitude": position.latitude,
            "longitude": position.longitude,
            "vitesse_kmh": position.vitesse_kmh,
            "cap_degres": position.cap_degres,
            "distance_restante_km": dist_restante,
            "temps_estime_minutes": temps_estime_min,
            "statut": "en_route",
            "derniere_mise_a_jour": maintenant,
        }

        self.dernieres_positions[position.livraison_id] = donnees_position

        # Diffusion temps réel aux clients connectés
        destinataires = self.connexions_actives.get(position.livraison_id, [])
        a_supprimer = []

        for ws in destinataires:
            try:
                await ws.send_text(json.dumps(donnees_position))
            except Exception:
                a_supprimer.append(ws)

        for ws in a_supprimer:
            self.deconnecter(position.livraison_id, ws)

        return ReponsePositionLivreur(
            livraison_id=position.livraison_id,
            livreur_id=position.livreur_id,
            latitude=position.latitude,
            longitude=position.longitude,
            statut="en_route",
            temps_estime_minutes=temps_estime_min,
            distance_restante_km=dist_restante,
            derniere_mise_a_jour=maintenant,
        )

    def obtenir_derniere_position(self, livraison_id: str) -> ReponsePositionLivreur:
        if livraison_id in self.dernieres_positions:
            pos = self.dernieres_positions[livraison_id]
            return ReponsePositionLivreur(
                livraison_id=livraison_id,
                livreur_id=pos.get("livreur_id", 1),
                latitude=pos["latitude"],
                longitude=pos["longitude"],
                statut=pos.get("statut", "en_route"),
                temps_estime_minutes=pos.get("temps_estime_minutes", 15),
                distance_restante_km=pos.get("distance_restante_km", 4.2),
                derniere_mise_a_jour=pos.get("derniere_mise_a_jour", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )

        # Position par défaut initiale (Abidjan Plateau : 5.3200, -4.0150)
        return ReponsePositionLivreur(
            livraison_id=livraison_id,
            livreur_id=1,
            latitude=5.3200,
            longitude=-4.0150,
            statut="en_preparation",
            temps_estime_minutes=25,
            distance_restante_km=6.8,
            derniere_mise_a_jour=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )


gestionnaire_suivi = GestionnaireSuiviTempsReel()
