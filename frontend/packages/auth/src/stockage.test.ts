import { describe, expect, it } from "vitest";
import { cleRefresh, creerStockage } from "./stockage";

function supportMemoire() {
  const donnees = new Map<string, string>();
  return {
    donnees,
    getItem: (cle: string) => donnees.get(cle) ?? null,
    setItem: (cle: string, valeur: string) => void donnees.set(cle, valeur),
    removeItem: (cle: string) => void donnees.delete(cle),
  };
}

describe("cleRefresh", () => {
  it("préfixe la clé par portail", () => {
    expect(cleRefresh("vendeur")).toBe("anitche.vendeur.refresh");
  });
});

describe("creerStockage", () => {
  it("lit, écrit et efface sous la clé du portail", () => {
    const support = supportMemoire();
    const stockage = creerStockage("livreur", support);

    expect(stockage.lireRefresh()).toBeUndefined();
    stockage.ecrireRefresh("R1");
    expect(support.donnees.get("anitche.livreur.refresh")).toBe("R1");
    expect(stockage.lireRefresh()).toBe("R1");
    stockage.effacer();
    expect(stockage.lireRefresh()).toBeUndefined();
  });

  it("deux portails sur la même origine ne partagent pas leur session", () => {
    const support = supportMemoire();
    const client = creerStockage("client", support);
    const vendeur = creerStockage("vendeur", support);

    client.ecrireRefresh("R-CLIENT");
    expect(vendeur.lireRefresh()).toBeUndefined();
    vendeur.effacer();
    expect(client.lireRefresh()).toBe("R-CLIENT");
  });

  it("un stockage qui lève (accès refusé) ne casse rien", () => {
    const refuse = {
      getItem: () => {
        throw new DOMException("denied", "SecurityError");
      },
      setItem: () => {
        throw new DOMException("quota", "QuotaExceededError");
      },
      removeItem: () => {
        throw new DOMException("denied", "SecurityError");
      },
    };
    const stockage = creerStockage("client", refuse);

    expect(stockage.lireRefresh()).toBeUndefined();
    expect(() => stockage.ecrireRefresh("R1")).not.toThrow();
    expect(() => stockage.effacer()).not.toThrow();
  });
});
