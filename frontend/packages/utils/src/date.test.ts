import { describe, expect, it } from "vitest";
import { formaterDate, formaterDateHeure, formaterRelatif, tempsRestant } from "./date";

const MAINTENANT = new Date("2026-10-07T12:00:00Z");
// 23 h 30 à Abidjan (UTC+0) ; un fuseau local à l'ouest ou à l'est donnerait une autre heure.
const TARD = "2026-10-07T23:30:00Z";

describe("fuseau", () => {
  it("le process de test n'est pas à Abidjan", () => {
    expect(new Date(TARD).getDate()).toBe(7);
    expect(new Date("2026-10-07T03:30:00Z").getDate()).toBe(6);
  });
});

describe("formaterDate", () => {
  it("affiche la date en français, en heure d'Abidjan", () => {
    expect(formaterDate(TARD)).toBe("7 octobre 2026");
    expect(formaterDate("2026-10-07T03:30:00Z")).toBe("7 octobre 2026");
  });

  it("convertit un décalage différent de Z", () => {
    expect(formaterDate("2026-10-08T00:30:00+02:00")).toBe("7 octobre 2026");
  });

  it.each(["2026-10-07", "2026-10-07T12:00:00", "2026-10-07 12:00:00Z", "demain", "", "2026-13-45T00:00:00Z"])(
    "refuse %j",
    (valeur) => {
      expect(() => formaterDate(valeur)).toThrow(RangeError);
    },
  );
});

describe("formaterDateHeure", () => {
  it("affiche date et heure d'Abidjan", () => {
    expect(formaterDateHeure(TARD)).toMatch(/^7 octobre 2026 (à )?23:30$/);
  });

  it("refuse une date sans décalage", () => {
    expect(() => formaterDateHeure("2026-10-07T12:00:00")).toThrow(RangeError);
  });
});

describe("formaterRelatif", () => {
  it("passé et futur, par unité", () => {
    expect(formaterRelatif("2026-10-07T11:55:00Z", MAINTENANT)).toBe("il y a 5 minutes");
    expect(formaterRelatif("2026-10-07T14:00:00Z", MAINTENANT)).toBe("dans 2 heures");
    expect(formaterRelatif("2026-10-06T12:00:00Z", MAINTENANT)).toBe("hier");
    expect(formaterRelatif("2026-10-07T11:59:30Z", MAINTENANT)).toBe("il y a 30 secondes");
  });

  it("refuse une date invalide", () => {
    expect(() => formaterRelatif("2026-10-07", MAINTENANT)).toThrow(RangeError);
  });
});

describe("tempsRestant", () => {
  it("décompose le temps restant", () => {
    expect(tempsRestant("2026-10-07T12:29:30Z", MAINTENANT)).toEqual({ minutes: 29, secondes: 30, expire: false });
  });

  it("arrondit à la seconde supérieure pour ne pas afficher 0:00 avant l'échéance", () => {
    expect(tempsRestant("2026-10-07T12:00:00.400Z", MAINTENANT)).toEqual({ minutes: 0, secondes: 1, expire: false });
  });

  it("expire à l'échéance et après", () => {
    expect(tempsRestant("2026-10-07T12:00:00Z", MAINTENANT)).toEqual({ minutes: 0, secondes: 0, expire: true });
    expect(tempsRestant("2026-10-07T11:00:00Z", MAINTENANT)).toEqual({ minutes: 0, secondes: 0, expire: true });
  });

  it("refuse une échéance sans décalage", () => {
    expect(() => tempsRestant("2026-10-07T12:30:00", MAINTENANT)).toThrow(RangeError);
  });
});
