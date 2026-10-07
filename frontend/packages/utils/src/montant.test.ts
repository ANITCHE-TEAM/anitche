import { describe, expect, it } from "vitest";
import { formaterFcfa, versFcfa } from "./montant";

describe("versFcfa", () => {
  it("convertit une chaîne décimale de l'API en entier", () => {
    expect(versFcfa("65000.00")).toBe(65000);
    expect(versFcfa("0.00")).toBe(0);
    expect(versFcfa("-1500.00")).toBe(-1500);
  });

  it("accepte un entier", () => {
    expect(versFcfa(15000)).toBe(15000);
  });

  it("ne produit jamais -0", () => {
    expect(Object.is(versFcfa("-0.00"), 0)).toBe(true);
  });

  it.each(["65000.50", "65000.01", "abc", "", " 1500", "1e3", "1,5", "0x10"])("refuse la chaîne %j", (valeur) => {
    expect(() => versFcfa(valeur)).toThrow(RangeError);
  });

  it.each([1500.5, Number.NaN, Number.POSITIVE_INFINITY, 2 ** 53])("refuse le nombre %j", (valeur) => {
    expect(() => versFcfa(valeur)).toThrow(RangeError);
  });
});

describe("formaterFcfa", () => {
  it("affiche 65 000 FCFA : milliers fr-FR (U+202F), espace insécable (U+00A0) avant FCFA", () => {
    expect(formaterFcfa("65000.00")).toBe("65 000 FCFA");
  });

  it("n'ajoute pas de séparateur sous 1 000", () => {
    expect(formaterFcfa("500.00")).toBe("500 FCFA");
    expect(formaterFcfa(0)).toBe("0 FCFA");
  });

  it("sépare aussi les millions", () => {
    expect(formaterFcfa(1250000)).toBe("1 250 000 FCFA");
  });

  it("refuse une valeur qui n'est pas un entier de FCFA au lieu de l'arrondir", () => {
    expect(() => formaterFcfa("65000.50")).toThrow(RangeError);
    expect(() => formaterFcfa("abc")).toThrow(RangeError);
  });
});
