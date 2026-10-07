import { QueryClient } from "@tanstack/react-query";
import { estErreurApi } from "./erreurs";

const RELANCES_RESEAU = 2;
const RELANCES_SERVEUR = 1;

/**
 * Relance d'une lecture : réseau (statut 0) 2 fois, 5xx 1 fois, jamais sur 4xx (429 compris),
 * jamais sur une erreur qui n'est pas une `ErreurApi` (bug de code).
 */
export function doitRelancerLecture(nombreEchecs: number, erreur: unknown): boolean {
  if (!estErreurApi(erreur)) return false;
  if (erreur.statut === 0) return nombreEchecs < RELANCES_RESEAU;
  if (erreur.statut >= 500) return nombreEchecs < RELANCES_SERVEUR;
  return false;
}

/** Délai croissant entre deux relances : 1 s, 2 s, plafonné à 5 s. */
export function delaiRelance(indexRelance: number): number {
  return Math.min(1000 * 2 ** indexRelance, 5000);
}

export function creerQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: doitRelancerLecture, retryDelay: delaiRelance },
      mutations: { retry: false },
    },
  });
}
