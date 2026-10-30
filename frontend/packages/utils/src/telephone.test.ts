import { describe, expect, it } from "vitest";
import { estTelephoneCI, formaterTelephone, normaliserTelephone } from "./telephone";

describe("normaliserTelephone", () => {
  it.each([
    "0707070707",
    "07 07 07 07 07",
    "07.07.07.07.07",
    "07-07-07-07-07",
    "+225 07 07 07 07 07",
    "+2250707070707",
    "00225 0707070707",
    "2250707070707",
  ])("normalise %j en E.164", (saisie) => {
    expect(normaliserTelephone(saisie)).toBe("+2250707070707");
  });

  it.each([
    ["ancien format à 8 chiffres", "07070707"],
    ["trop court", "070707070"],
    ["trop long", "07070707070"],
    ["indicatif étranger", "+33 6 12 34 56 78"],
    ["indicatif 225 suivi de 8 chiffres", "+22507070707"],
    ["10 chiffres sans 0 initial", "7070707070"],
    ["lettres", "07O7070707"],
    ["vide", ""],
    ["plus au milieu", "07+0707070707"],
  ])("refuse : %s", (_cas, saisie) => {
    expect(() => normaliserTelephone(saisie)).toThrow(RangeError);
  });
});

describe("formaterTelephone", () => {
  it("affiche le numéro national par paires", () => {
    expect(formaterTelephone("+2250707070707")).toBe("07 07 07 07 07");
    expect(formaterTelephone("+2250102030405")).toBe("01 02 03 04 05");
  });

  it.each(["0707070707", "+2250707", "+33612345678", "+225 0707070707", ""])("refuse %j", (valeur) => {
    expect(() => formaterTelephone(valeur)).toThrow(RangeError);
  });
});

describe("estTelephoneCI", () => {
  it("vrai pour un numéro ivoirien, faux sinon", () => {
    expect(estTelephoneCI("07 07 07 07 07")).toBe(true);
    expect(estTelephoneCI("+33 6 12 34 56 78")).toBe(false);
    expect(estTelephoneCI("abc")).toBe(false);
  });
});
