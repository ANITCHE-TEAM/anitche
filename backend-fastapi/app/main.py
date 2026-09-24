from fastapi import FastAPI
from app.routeurs import recherche, conseiller_ia, scan_qr, suivi_temps_reel

app = FastAPI(title="ANITCHE - Services rapides")

app.include_router(recherche.router, prefix="/recherche", tags=["Recherche"])
app.include_router(conseiller_ia.router, prefix="/ia", tags=["IA"])
app.include_router(scan_qr.router, prefix="/qr", tags=["QR Code"])
app.include_router(suivi_temps_reel.router, prefix="/livraison", tags=["Suivi temps réel"])

@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "fastapi"}