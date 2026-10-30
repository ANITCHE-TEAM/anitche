import { render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { creerAuth } from "./actions";
import { useSession } from "./contexte";
import { SessionProvider } from "./SessionProvider";
import { creerStockageMemoire, installerFauxReseau, json } from "./test-support";

afterEach(() => {
  vi.unstubAllGlobals();
});

function Etat() {
  const { etat, role } = useSession();
  return <p>{`${etat}:${role ?? "-"}`}</p>;
}

function monter(refresh?: string) {
  return creerAuth({
    portail: "vendeur",
    baseDjango: "http://api.test",
    baseFastapi: "http://api.test/fast",
    stockage: creerStockageMemoire(refresh),
    canal: undefined,
    verrous: undefined,
  });
}

describe("SessionProvider", () => {
  it("passe de inconnue à invité sans session stockée", async () => {
    installerFauxReseau({});
    render(
      <SessionProvider auth={monter()}>
        <Etat />
      </SessionProvider>,
    );

    expect(await screen.findByText("invite:-")).toBeInTheDocument();
  });

  it("charge le profil au démarrage, un seul rafraîchissement même en StrictMode", async () => {
    const reseau = installerFauxReseau({
      "POST /api/utilisateurs/connexion/rafraichir/": () => json(200, { access: "A2", refresh: "R2" }),
      "GET /api/utilisateurs/profil/": () => json(200, { id: 1, role: "vendeur" }),
    });
    render(
      <StrictMode>
        <SessionProvider auth={monter("R1")}>
          <Etat />
        </SessionProvider>
      </StrictMode>,
    );

    expect(await screen.findByText("connecte:vendeur")).toBeInTheDocument();
    expect(reseau.compter("POST /api/utilisateurs/connexion/rafraichir/")).toBe(1);
  });
});
