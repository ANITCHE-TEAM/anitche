import type { Portail, StockageJetons } from "./types";

/** Clé du jeton de rafraîchissement : préfixée par portail, pour que deux portails d'une même origine ne se mélangent pas. */
export function cleRefresh(portail: Portail): string {
  return `anitche.${portail}.refresh`;
}

/**
 * Adaptateur sur un `Storage` du navigateur. Un stockage inaccessible (navigation privée stricte,
 * quota, accès refusé) ne casse pas l'application : la session vit alors le temps de la page.
 */
export function creerStockage(portail: Portail, support: Pick<Storage, "getItem" | "setItem" | "removeItem">): StockageJetons {
  const cle = cleRefresh(portail);
  return {
    lireRefresh() {
      try {
        return support.getItem(cle) ?? undefined;
      } catch {
        return undefined;
      }
    },
    ecrireRefresh(refresh) {
      try {
        support.setItem(cle, refresh);
      } catch {
        // Voir le commentaire de la fonction.
      }
    },
    effacer() {
      try {
        support.removeItem(cle);
      } catch {
        // Voir le commentaire de la fonction.
      }
    },
  };
}

/**
 * Stockage par défaut d'un portail : `sessionStorage` pour l'admin (fermer l'onglet = se déconnecter),
 * `localStorage` pour les autres (sessions longues sur téléphone).
 */
export function creerStockageParDefaut(portail: Portail): StockageJetons {
  return creerStockage(portail, portail === "admin" ? window.sessionStorage : window.localStorage);
}
