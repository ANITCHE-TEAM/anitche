import { describe, expect, it } from "vitest";
import { ErreurApi } from "./erreurs";
import { creerQueryClient, delaiRelance, doitRelancerLecture } from "./query-client";

const reseau = new ErreurApi(0, "django");
const http = (statut: number) => new ErreurApi(statut, "django");

describe("doitRelancerLecture", () => {
  it("relance deux fois une panne réseau, pas trois", () => {
    expect(doitRelancerLecture(0, reseau)).toBe(true);
    expect(doitRelancerLecture(1, reseau)).toBe(true);
    expect(doitRelancerLecture(2, reseau)).toBe(false);
  });

  it("relance une fois un 5xx", () => {
    expect(doitRelancerLecture(0, http(500))).toBe(true);
    expect(doitRelancerLecture(0, http(503))).toBe(true);
    expect(doitRelancerLecture(1, http(500))).toBe(false);
  });

  it.each([400, 401, 403, 404, 409, 429])("ne relance jamais un %i", (statut) => {
    expect(doitRelancerLecture(0, http(statut))).toBe(false);
  });

  it("ne relance pas une erreur de code", () => {
    expect(doitRelancerLecture(0, new TypeError("x"))).toBe(false);
  });
});

describe("delaiRelance", () => {
  it("double puis plafonne à 5 s", () => {
    expect([0, 1, 2, 3, 10].map(delaiRelance)).toEqual([1000, 2000, 4000, 5000, 5000]);
  });
});

describe("creerQueryClient", () => {
  it("ne relance jamais une mutation", () => {
    const options = creerQueryClient().getDefaultOptions();
    expect(options.mutations?.retry).toBe(false);
    expect(options.queries?.retry).toBe(doitRelancerLecture);
  });
});
