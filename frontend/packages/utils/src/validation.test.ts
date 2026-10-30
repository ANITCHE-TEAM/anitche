import { describe, expect, it } from "vitest";
import { email, montantEntier, telephoneCI } from "./validation";

describe("telephoneCI", () => {
  it("retourne le E.164", () => {
    expect(telephoneCI.parse("07 07 07 07 07")).toBe("+2250707070707");
  });

  it("rejette un numéro étranger avec un message en français", () => {
    const resultat = telephoneCI.safeParse("+33 6 12 34 56 78");
    expect(resultat.success).toBe(false);
    expect(resultat.error?.issues[0]?.message).toContain("10 chiffres");
  });

  it("rejette une valeur qui n'est pas une chaîne", () => {
    expect(telephoneCI.safeParse(707070707).success).toBe(false);
  });
});

describe("email", () => {
  it("accepte une adresse valide", () => {
    expect(email.parse("client@anitche.com")).toBe("client@anitche.com");
  });

  it.each(["", "client", "client@", "@anitche.com", "a b@anitche.com"])("rejette %j", (valeur) => {
    expect(email.safeParse(valeur).success).toBe(false);
  });
});

describe("montantEntier", () => {
  it("accepte un entier ou sa chaîne et retourne un entier", () => {
    expect(montantEntier.parse(15000)).toBe(15000);
    expect(montantEntier.parse("15000")).toBe(15000);
    expect(montantEntier.parse("15000.00")).toBe(15000);
  });

  it.each([0, "0", -5, "15000.50", 1500.5, "abc", "", Number.NaN])("rejette %j", (valeur) => {
    const resultat = montantEntier.safeParse(valeur);
    expect(resultat.success).toBe(false);
    expect(resultat.error?.issues[0]?.message).toContain("FCFA");
  });

  it("rejette null et undefined", () => {
    expect(montantEntier.safeParse(null).success).toBe(false);
    expect(montantEntier.safeParse(undefined).success).toBe(false);
  });
});
