/**
 * `exp` (secondes) lu dans la charge utile d'un JWT, **sans vérifier la signature**.
 * Sert uniquement à ordonnancer un rafraîchissement ; jamais à autoriser quoi que ce soit.
 */
export function lireExpiration(jeton: string): number | undefined {
  const charge = jeton.split(".")[1];
  if (charge === undefined) return undefined;
  try {
    const base64 = charge.replace(/-/g, "+").replace(/_/g, "/");
    const exp = (JSON.parse(atob(base64)) as { exp?: unknown }).exp;
    return typeof exp === "number" && Number.isFinite(exp) ? exp : undefined;
  } catch {
    return undefined;
  }
}

/** Vrai si le jeton est illisible ou expire dans moins de `margeSecondes`. */
export function expireBientot(jeton: string, maintenantMs: number, margeSecondes = 60): boolean {
  const exp = lireExpiration(jeton);
  return exp === undefined || exp * 1000 - maintenantMs < margeSecondes * 1000;
}
