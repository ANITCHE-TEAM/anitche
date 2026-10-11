import { describe, expect, it } from "vitest";
import { expireBientot, lireExpiration } from "./jwt";
import { fabriquerJeton } from "./test-support";

const MAINTENANT = Date.parse("2026-10-11T10:00:00Z");

describe("lireExpiration", () => {
  it("lit exp dans la charge utile", () => {
    const jeton = fabriquerJeton(900, MAINTENANT);
    expect(lireExpiration(jeton)).toBe(MAINTENANT / 1000 + 900);
  });

  it.each([["vide", ""], ["un seul segment", "abc"], ["charge non base64", "a.%%%.c"], ["charge sans exp", `a.${btoa("{}")}.c`]])(
    "%s : undefined",
    (_nom, jeton) => {
      expect(lireExpiration(jeton)).toBeUndefined();
    },
  );
});

describe("expireBientot", () => {
  it("faux au-delà de la marge, vrai en deçà", () => {
    expect(expireBientot(fabriquerJeton(120, MAINTENANT), MAINTENANT)).toBe(false);
    expect(expireBientot(fabriquerJeton(59, MAINTENANT), MAINTENANT)).toBe(true);
    expect(expireBientot(fabriquerJeton(-5, MAINTENANT), MAINTENANT)).toBe(true);
  });

  it("un jeton illisible est considéré expiré (on rafraîchit plutôt que d'envoyer n'importe quoi)", () => {
    expect(expireBientot("pas-un-jwt", MAINTENANT)).toBe(true);
  });
});
