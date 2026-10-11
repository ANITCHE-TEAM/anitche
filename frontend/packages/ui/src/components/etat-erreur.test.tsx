import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { EtatErreur } from "./etat-erreur";

describe("EtatErreur", () => {
  it("est annoncé comme alerte avec son titre et son message", () => {
    render(<EtatErreur titre="Chargement impossible" message="Réessayez plus tard." />);
    const alerte = screen.getByRole("alert");
    expect(alerte).toHaveTextContent("Chargement impossible");
    expect(alerte).toHaveTextContent("Réessayez plus tard.");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("le bouton réessayer appelle onClick", async () => {
    const onClick = vi.fn();
    render(<EtatErreur titre="Échec" reessayer={{ libelle: "Réessayer", onClick }} />);
    await userEvent.click(screen.getByRole("button", { name: "Réessayer" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
