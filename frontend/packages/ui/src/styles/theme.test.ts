import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(import.meta.dirname, "..");
const theme = readFileSync(join(import.meta.dirname, "theme.css"), "utf8");

/** Fichiers .ts/.tsx/.css de src/, hors tests. */
function sources(dossier: string): string[] {
  return readdirSync(dossier, { withFileTypes: true }).flatMap((entree) => {
    const chemin = join(dossier, entree.name);
    if (entree.isDirectory()) return sources(chemin);
    return /\.(tsx?|css)$/.test(entree.name) && !/\.test\./.test(entree.name) ? [chemin] : [];
  });
}

function bloc(source: string, ouverture: string): string {
  const debut = source.indexOf(ouverture);
  expect(debut, `${ouverture} introuvable`).toBeGreaterThanOrEqual(0);
  const fin = source.indexOf("\n}", debut);
  return source.slice(debut, fin);
}

const racine = bloc(theme, ":root {");
const definis = new Set([...racine.matchAll(/(--anitche-[\w-]+)\s*:/g)].map((m) => m[1]));

describe("theme.css", () => {
  it("définit des jetons --anitche-* dans :root", () => {
    expect(definis.size).toBeGreaterThan(0);
  });

  it("chaque var(--anitche-…) de @theme inline est définie dans :root", () => {
    const utilises = [...bloc(theme, "@theme inline {").matchAll(/var\((--anitche-[\w-]+)\)/g)].map((m) => m[1]);
    expect(utilises.length).toBeGreaterThan(0);
    expect(utilises.filter((nom) => !definis.has(nom))).toEqual([]);
  });

  it("chaque jeton lu par un composant est défini dans :root", () => {
    const lus = new Set<string>();
    for (const fichier of sources(SRC)) {
      for (const m of readFileSync(fichier, "utf8").matchAll(/var\((--anitche-[\w-]+)\)/g)) lus.add(m[1]!);
    }
    expect([...lus].filter((nom) => !definis.has(nom))).toEqual([]);
  });

  it("les composants n'utilisent que des variables --anitche-* (aucune variable nue non définie)", () => {
    const nues: string[] = [];
    for (const fichier of sources(join(SRC, "components"))) {
      for (const m of readFileSync(fichier, "utf8").matchAll(/var\((--[\w-]+)\)/g)) {
        if (!m[1]!.startsWith("--anitche-")) nues.push(`${fichier}: ${m[1]}`);
      }
    }
    expect(nues).toEqual([]);
  });
});
