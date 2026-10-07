import { describe, expect, it, vi } from "vitest";
import {
  ErreurApi,
  appliquerErreursAuFormulaire,
  erreurDepuisCorps,
  erreurDepuisReponse,
  erreurReseau,
  erreursParChamp,
  estErreurApi,
  lireRetryAfter,
} from "./erreurs";
import { erreur } from "./testing";

describe("lireRetryAfter", () => {
  it("lit un nombre de secondes", () => {
    expect(lireRetryAfter("120")).toBe(120);
  });

  it("convertit une date HTTP en écart avec maintenant", () => {
    const maintenant = Date.parse("2026-10-07T10:00:00Z");
    expect(lireRetryAfter("Wed, 07 Oct 2026 10:01:30 GMT", maintenant)).toBe(90);
  });

  it("ne descend jamais sous zéro pour une date passée", () => {
    const maintenant = Date.parse("2026-10-07T10:00:00Z");
    expect(lireRetryAfter("Wed, 07 Oct 2026 09:00:00 GMT", maintenant)).toBe(0);
  });

  it("renvoie undefined pour une valeur absente ou illisible", () => {
    expect(lireRetryAfter(null)).toBeUndefined();
    expect(lireRetryAfter("bientôt")).toBeUndefined();
  });
});

describe("erreurDepuisReponse", () => {
  it("lit l'enveloppe du backend : detail, champs et code machine", async () => {
    const reponse = erreur(403, {
      detail: "Email non vérifié.",
      errors: { code: ["email_non_verifie"], "adresse.commune": ["Obligatoire."] },
    });

    const resultat = await erreurDepuisReponse(reponse, "django");

    expect(resultat).toBeInstanceOf(ErreurApi);
    expect(resultat.statut).toBe(403);
    expect(resultat.source).toBe("django");
    expect(resultat.detail).toBe("Email non vérifié.");
    expect(resultat.code).toBe("email_non_verifie");
    expect(resultat.champs).toEqual({ "adresse.commune": ["Obligatoire."] });
  });

  it("lit Retry-After sur un 429", async () => {
    const resultat = await erreurDepuisReponse(erreur(429, { retryAfter: 30 }), "fastapi");
    expect(resultat.retryAfter).toBe(30);
    expect(resultat.source).toBe("fastapi");
  });

  it("n'interprète pas une page HTML : detail générique, aucun champ", async () => {
    const reponse = new Response("<html><body>502 Bad Gateway</body></html>", {
      status: 502,
      headers: { "Content-Type": "text/html" },
    });

    const resultat = await erreurDepuisReponse(reponse, "django");

    expect(resultat.statut).toBe(502);
    expect(resultat.detail).not.toContain("html");
    expect(resultat.champs).toEqual({});
    expect(resultat.code).toBeUndefined();
  });

  it("ignore un errors mal formé sans lever", () => {
    const resultat = erreurDepuisCorps(400, { detail: "x", errors: { a: "pas une liste", b: [1, 2] } }, new Headers(), "django");
    expect(resultat.champs).toEqual({});
  });
});

describe("erreurReseau", () => {
  it("a le statut 0 et un message affichable", () => {
    const resultat = erreurReseau("django", new TypeError("fetch failed"));
    expect(resultat.statut).toBe(0);
    expect(resultat.detail).toMatch(/réseau/);
    expect(resultat.cause).toBeInstanceOf(TypeError);
  });
});

describe("estErreurApi", () => {
  it("distingue une ErreurApi d'une erreur quelconque", () => {
    expect(estErreurApi(erreurReseau("django"))).toBe(true);
    expect(estErreurApi(new Error("x"))).toBe(false);
    expect(estErreurApi(undefined)).toBe(false);
  });
});

describe("appliquerErreursAuFormulaire", () => {
  const erreur400 = new ErreurApi(400, "django", {
    champs: { email: ["Invalide.", "Autre."], "articles.0.quantite": ["Trop grand."], non_field_errors: ["Stock insuffisant."] },
  });

  it("garde le premier message de chaque champ", () => {
    expect(erreursParChamp(erreur400)).toEqual({
      email: "Invalide.",
      "articles.0.quantite": "Trop grand.",
      non_field_errors: "Stock insuffisant.",
    });
  });

  it("pose chaque champ (clé à points) et non_field_errors sous root", () => {
    const setError = vi.fn();

    const pose = appliquerErreursAuFormulaire({ setError }, erreur400);

    expect(pose).toBe(true);
    expect(setError).toHaveBeenCalledWith("email", { message: "Invalide." });
    expect(setError).toHaveBeenCalledWith("articles.0.quantite", { message: "Trop grand." });
    expect(setError).toHaveBeenCalledWith("root", { message: "Stock insuffisant." });
  });

  it("renvoie false quand l'erreur ne porte sur aucun champ", () => {
    const setError = vi.fn();
    expect(appliquerErreursAuFormulaire({ setError }, new ErreurApi(409, "django"))).toBe(false);
    expect(setError).not.toHaveBeenCalled();
  });
});
