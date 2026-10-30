import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";
import type { Auth } from "./actions";
import { ContexteAuth } from "./contexte";
import { ExigerConnexion, ExigerRole, RedirigerSiConnecte, decider, peutAcceder } from "./gardes";
import { creerStoreSession, type EtatSession } from "./session";
import type { Profil, Role } from "./types";

const TOUS_LES_ROLES: Role[] = ["client", "vendeur", "livreur", "moderateur", "support", "admin", "super_admin"];
const PORTAILS = ["client", "vendeur", "livreur", "admin"] as const;

function connecte(role: Role): EtatSession {
  return { etat: "connecte", role, profil: { role } as Profil };
}
const INCONNUE: EtatSession = { etat: "inconnue", profil: undefined, role: undefined };
const INVITE: EtatSession = { etat: "invite", profil: undefined, role: undefined };

/** Monte `ui` dans un contexte dont la session est `etat`. */
function avec(etat: EtatSession, ui: ReactNode) {
  const session = creerStoreSession();
  session.setState(etat, true);
  return <ContexteAuth value={{ session } as Auth}>{ui}</ContexteAuth>;
}

describe("peutAcceder (table de vérité rôle × rôles admis)", () => {
  for (const role of TOUS_LES_ROLES) {
    for (const admis of TOUS_LES_ROLES) {
      it(`${role} dans [${admis}] → ${String(role === admis)}`, () => {
        expect(peutAcceder({ role }, [admis])).toBe(role === admis);
      });
    }
  }

  it("liste vide : personne", () => {
    for (const role of TOUS_LES_ROLES) expect(peutAcceder({ role }, [])).toBe(false);
  });

  it("sans rôle (profil non chargé) : refusé même si la liste est large", () => {
    expect(peutAcceder({ role: undefined }, TOUS_LES_ROLES)).toBe(false);
  });
});

describe("decider", () => {
  it("inconnue → attendre, quelle que soit la liste", () => {
    expect(decider(INCONNUE)).toBe("attendre");
    expect(decider(INCONNUE, ["client"])).toBe("attendre");
  });

  it("invité → connexion", () => {
    expect(decider(INVITE)).toBe("connexion");
    expect(decider(INVITE, ["client"])).toBe("connexion");
  });

  it("connecté sans liste de rôles → autoriser", () => {
    expect(decider(connecte("client"))).toBe("autoriser");
  });

  for (const role of TOUS_LES_ROLES) {
    for (const portail of PORTAILS) {
      it(`${role} sur un portail réservé à « ${portail} » → ${role === portail ? "autoriser" : "refuser"}`, () => {
        expect(decider(connecte(role), [portail])).toBe(role === portail ? "autoriser" : "refuser");
      });
    }
  }
});

const attente = <p>attente</p>;
const invite = <p>invite</p>;
const refuse = <p>refuse</p>;
const dejaConnecte = <p>connecte</p>;

describe("ExigerConnexion", () => {
  it("rend l'attente, l'invitation ou le contenu selon l'état", () => {
    const garde = (
      <ExigerConnexion attente={attente} invite={invite}>
        <p>secret</p>
      </ExigerConnexion>
    );
    const { rerender } = render(avec(INCONNUE, garde));
    expect(screen.getByText("attente")).toBeInTheDocument();
    expect(screen.queryByText("secret")).not.toBeInTheDocument();

    rerender(avec(INVITE, garde));
    expect(screen.getByText("invite")).toBeInTheDocument();
    expect(screen.queryByText("secret")).not.toBeInTheDocument();

    rerender(avec(connecte("client"), garde));
    expect(screen.getByText("secret")).toBeInTheDocument();
  });
});

describe("ExigerRole", () => {
  const garde = (
    <ExigerRole roles={["vendeur"]} attente={attente} invite={invite} refuse={refuse}>
      <p>secret</p>
    </ExigerRole>
  );

  it("ne montre jamais le contenu à un rôle hors liste", () => {
    render(avec(connecte("client"), garde));
    expect(screen.getByText("refuse")).toBeInTheDocument();
    expect(screen.queryByText("secret")).not.toBeInTheDocument();
  });

  it("montre le contenu au rôle admis", () => {
    render(avec(connecte("vendeur"), garde));
    expect(screen.getByText("secret")).toBeInTheDocument();
  });

  it("invité : l'invitation, pas le refus", () => {
    render(avec(INVITE, garde));
    expect(screen.getByText("invite")).toBeInTheDocument();
    expect(screen.queryByText("refuse")).not.toBeInTheDocument();
  });

  it("session inconnue : l'attente, ni refus ni contenu", () => {
    render(avec(INCONNUE, garde));
    expect(screen.getByText("attente")).toBeInTheDocument();
    expect(screen.queryByText("secret")).not.toBeInTheDocument();
  });
});

describe("RedirigerSiConnecte", () => {
  it("inverse de ExigerConnexion : cache le contenu à un connecté", () => {
    const garde = (
      <RedirigerSiConnecte attente={attente} connecte={dejaConnecte}>
        <p>formulaire</p>
      </RedirigerSiConnecte>
    );
    const { rerender } = render(avec(connecte("client"), garde));
    expect(screen.getByText("connecte")).toBeInTheDocument();
    expect(screen.queryByText("formulaire")).not.toBeInTheDocument();

    rerender(avec(INVITE, garde));
    expect(screen.getByText("formulaire")).toBeInTheDocument();
  });
});
