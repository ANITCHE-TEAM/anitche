import { useEffect, type ReactNode } from "react";
import type { Auth } from "./actions";
import { ContexteAuth } from "./contexte";

/**
 * Monte la session d'un portail et la démarre une fois (profil relu au chargement).
 * `auth` vient de `creerAuth()`, créé une fois par le portail hors de React.
 */
export function SessionProvider({ auth, children }: { auth: Auth; children: ReactNode }): ReactNode {
  useEffect(() => {
    // Une panne au démarrage laisse la session « inconnue » : le portail peut rappeler `auth.demarrer()`.
    void auth.demarrer().catch(() => undefined);
  }, [auth]);

  return <ContexteAuth value={auth}>{children}</ContexteAuth>;
}
