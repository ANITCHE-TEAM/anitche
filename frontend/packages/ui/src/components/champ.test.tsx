import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Champ } from "./champ";
import { Input } from "./input";

function monter(props: { erreur?: string; aide?: string }) {
  return render(
    <Champ id="mail" libelle="Courriel" {...props}>
      {(attributs) => <Input {...attributs} />}
    </Champ>,
  );
}

describe("Champ", () => {
  it("relie le libellé au contrôle", () => {
    monter({});
    const controle = screen.getByLabelText("Courriel");
    expect(controle).not.toHaveAttribute("aria-describedby");
    expect(controle).not.toHaveAttribute("aria-invalid");
  });

  it("l'erreur est reliée au contrôle par aria-describedby et marque aria-invalid", () => {
    monter({ erreur: "Courriel invalide" });
    const controle = screen.getByLabelText("Courriel");
    expect(controle).toHaveAttribute("aria-invalid", "true");
    expect(controle).toHaveAccessibleDescription("Courriel invalide");
  });

  it("aide et erreur sont décrites ensemble, l'aide d'abord", () => {
    monter({ aide: "Format : nom@domaine", erreur: "Courriel invalide" });
    expect(screen.getByLabelText("Courriel")).toHaveAccessibleDescription("Format : nom@domaine Courriel invalide");
  });

  it("une erreur vide n'est pas une erreur", () => {
    monter({ erreur: "" });
    const controle = screen.getByLabelText("Courriel");
    expect(controle).not.toHaveAttribute("aria-invalid");
    expect(controle).not.toHaveAttribute("aria-describedby");
  });
});
