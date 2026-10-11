import type { ReactNode } from "react";
import { useSession } from "./contexte";
import type { EtatSession } from "./session";
import type { Role } from "./types";

/*
 * Ces gardes sont de l'interface de confort : elles évitent d'afficher un écran inutilisable.
 * Le serveur reste juge de chaque requête (401, 403, 404) ; ne jamais s'en servir comme protection.
 * Le rendu des cas non autorisés (redirection vers /connexion, page 403) est fourni par le portail,
 * ce qui garde ce paquet indépendant du routeur et de tout texte de portail.
 */

export type DecisionGarde = "attendre" | "connexion" | "refuser" | "autoriser";

/** Le rôle (relu depuis le profil serveur) figure dans la liste. Sans rôle connu : refusé. */
export function peutAcceder(session: Pick<EtatSession, "role">, roles: readonly Role[]): boolean {
  return session.role !== undefined && roles.includes(session.role);
}

/** `roles` absent : seule la connexion est exigée. */
export function decider(session: EtatSession, roles?: readonly Role[]): DecisionGarde {
  if (session.etat === "inconnue") return "attendre";
  if (session.etat === "invite") return "connexion";
  if (roles !== undefined && !peutAcceder(session, roles)) return "refuser";
  return "autoriser";
}

export function ExigerConnexion(props: { attente: ReactNode; invite: ReactNode; children: ReactNode }): ReactNode {
  const decision = decider(useSession());
  if (decision === "attendre") return props.attente;
  if (decision === "connexion") return props.invite;
  return props.children;
}

export function ExigerRole(props: {
  roles: readonly Role[];
  attente: ReactNode;
  invite: ReactNode;
  refuse: ReactNode;
  children: ReactNode;
}): ReactNode {
  const decision = decider(useSession(), props.roles);
  if (decision === "attendre") return props.attente;
  if (decision === "connexion") return props.invite;
  if (decision === "refuser") return props.refuse;
  return props.children;
}

export function RedirigerSiConnecte(props: { attente: ReactNode; connecte: ReactNode; children: ReactNode }): ReactNode {
  const { etat } = useSession();
  if (etat === "inconnue") return props.attente;
  if (etat === "connecte") return props.connecte;
  return props.children;
}
