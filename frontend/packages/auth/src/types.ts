import type { Role, SchemaDjango } from "@anitche/api-client";

export type { Role };

export type Portail = "client" | "vendeur" | "livreur" | "admin";

export type Profil = SchemaDjango<"Profil">;

export interface Jetons {
  access: string;
  refresh: string;
}

/** Où le jeton de rafraîchissement est conservé. L'accès reste en mémoire. */
export interface StockageJetons {
  lireRefresh(): string | undefined;
  ecrireRefresh(refresh: string): void;
  effacer(): void;
}

/**
 * Message échangé entre les onglets d'un même portail.
 * `precedent` : le refresh consommé par la rotation, pour que seul un onglet qui tenait ce refresh
 * (un onglet dupliqué en sessionStorage) adopte le nouveau.
 */
export type MessageAuth =
  | { type: "jetons"; access: string; refresh: string; precedent?: string }
  | { type: "deconnexion" };

/** Diffusion entre onglets (BroadcastChannel). Un canal ne reçoit jamais ses propres messages. */
export interface Canal {
  envoyer(message: MessageAuth): void;
  ecouter(rappel: (message: MessageAuth) => void): () => void;
  fermer(): void;
}

/** Sous-ensemble de `navigator.locks` utilisé par le coordinateur. */
export interface Verrous {
  request<T>(nom: string, travail: () => Promise<T>): Promise<T>;
}

/** La session ne peut pas (ou plus) être prolongée : l'utilisateur doit se reconnecter. */
export class ErreurSession extends Error {
  constructor(message = "Session expirée.", options?: { cause?: unknown }) {
    super(message, options);
    this.name = "ErreurSession";
  }
}
