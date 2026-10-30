import { createContext, useContext } from "react";
import { useStore } from "zustand";
import type { Auth } from "./actions";
import type { EtatSession } from "./session";

export const ContexteAuth = createContext<Auth | undefined>(undefined);

/** Les actions et le store de session du portail (`connecter`, `deconnecter`, `obtenirJetonValide`, …). */
export function useAuth(): Auth {
  const auth = useContext(ContexteAuth);
  if (auth === undefined) throw new Error("useAuth doit être appelé sous <SessionProvider>.");
  return auth;
}

/** L'état de session (`inconnue`, `invite`, `connecte`), le profil et le rôle. */
export function useSession(): EtatSession {
  return useStore(useAuth().session);
}
