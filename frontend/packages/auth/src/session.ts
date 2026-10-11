import { createStore, type StoreApi } from "zustand/vanilla";
import type { Profil, Role } from "./types";

/**
 * `inconnue` : au démarrage, avant de savoir s'il existe une session ;
 * `invite` : pas de session ; `connecte` : profil chargé.
 */
export interface EtatSession {
  etat: "inconnue" | "invite" | "connecte";
  profil: Profil | undefined;
  /** Rôle du profil relu côté serveur, jamais celui du JWT. */
  role: Role | undefined;
}

export type StoreSession = StoreApi<EtatSession>;

const INCONNUE: EtatSession = { etat: "inconnue", profil: undefined, role: undefined };
const INVITE: EtatSession = { etat: "invite", profil: undefined, role: undefined };

/** Un store par portail (un portail = une session). */
export function creerStoreSession(): StoreSession {
  return createStore<EtatSession>(() => INCONNUE);
}

export function marquerConnecte(store: StoreSession, profil: Profil): void {
  store.setState({ etat: "connecte", profil, role: profil.role }, true);
}

export function marquerInvite(store: StoreSession): void {
  store.setState(INVITE, true);
}
