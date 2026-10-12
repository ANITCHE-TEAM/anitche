import { fileURLToPath } from "node:url";
import { ESLint } from "eslint";
import tseslint from "typescript-eslint";
import { describe, expect, it } from "vitest";
import { frontieres, frontieresPaquet } from "./frontieres.js";

const RACINE_CLIENT = fileURLToPath(new URL("../../../apps/client", import.meta.url));

async function messages(code, fichier = "src/app/x.ts") {
  const eslint = new ESLint({
    cwd: RACINE_CLIENT,
    overrideConfigFile: true,
    overrideConfig: [
      { files: ["**/*.{ts,tsx}"], languageOptions: { parser: tseslint.parser } },
      ...frontieres("client", RACINE_CLIENT),
    ],
  });
  const [resultat] = await eslint.lintText(code, { filePath: `${RACINE_CLIENT}/${fichier}` });
  return resultat.messages;
}

const restreints = (liste) => liste.filter((m) => m.ruleId === "no-restricted-imports");
const globales = (liste) => liste.filter((m) => m.ruleId === "no-restricted-globals");
const htmlBrut = (liste) => liste.filter((m) => m.ruleId === "no-restricted-syntax");

describe("frontières entre portails", () => {
  it("accepte un fichier sans import interdit", async () => {
    expect(restreints(await messages('import "react";'))).toEqual([]);
  });

  it("refuse l'import d'un autre portail par son paquet", async () => {
    const liste = restreints(await messages('import { LayoutVendeur } from "@anitche/app-vendeur";'));
    expect(liste).toHaveLength(1);
  });

  it("refuse l'import d'un autre portail par chemin relatif", async () => {
    const liste = restreints(
      await messages('import { LayoutVendeur } from "../../../vendeur/src/mises-en-page/LayoutVendeur";'),
    );
    expect(liste).toHaveLength(1);
  });

  it("refuse l'import d'un portail par le dossier apps", async () => {
    const liste = restreints(await messages('import x from "../../../../apps/admin/src/app/App";'));
    expect(liste).toHaveLength(1);
  });

  it("n'interdit pas le portail courant à lui-même", async () => {
    const liste = restreints(await messages('import x from "../../../client/src/app/App";'));
    expect(liste).toEqual([]);
  });
});

describe("zustand", () => {
  it("est refusé hors d'un store de module", async () => {
    expect(restreints(await messages('import { create } from "zustand";'))).toHaveLength(1);
  });

  it("est accepté dans fonctionnalites/<module>/store.ts", async () => {
    const liste = await messages('import { create } from "zustand";', "src/fonctionnalites/panier/store.ts");
    expect(restreints(liste)).toEqual([]);
  });
});

describe("F1 : un paquet s'importe par son point d'entrée", () => {
  it.each([
    'import { Button } from "@anitche/ui";',
    'import { Button } from "@anitche/ui/button";',
    'import { formaterFcfa } from "@anitche/utils";',
  ])("accepte : %s", async (code) => {
    expect(restreints(await messages(code))).toEqual([]);
  });

  it.each([
    ['import { Button } from "@anitche/ui/src/components/button";', "un fichier source d'un paquet"],
    ['import { cn } from "@anitche/utils/src";', "le dossier src d'un paquet"],
    ['import { cn } from "../../../../packages/ui/src/lib/cn";', "un chemin relatif vers packages/"],
    ['import { cn } from "../../../../ui/src/lib/cn";', "un chemin relatif vers le src d'un paquet"],
  ])("refuse %s (%s)", async (code) => {
    const liste = restreints(await messages(code));
    expect(liste).toHaveLength(1);
    expect(liste[0].message).toMatch(/point d'entrée/);
  });

  it("s'applique aussi à un store de module", async () => {
    const liste = await messages('import { cn } from "@anitche/ui/src/lib/cn";', "src/fonctionnalites/panier/store.ts");
    expect(restreints(liste)).toHaveLength(1);
  });
});

describe("F2 : openapi-fetch et openapi-typescript restent dans api-client", () => {
  it.each([
    'import createClient from "openapi-fetch";',
    'import type { paths } from "openapi-typescript";',
    'import { HttpMethod } from "openapi-typescript-helpers";',
  ])("refuse : %s", async (code) => {
    const liste = restreints(await messages(code));
    expect(liste).toHaveLength(1);
    expect(liste[0].message).toMatch(/@anitche\/api-client/);
  });
});

describe("F3 : pas d'appel réseau hors api.ts et lib/", () => {
  it.each(["fetch", "XMLHttpRequest", "WebSocket"])("refuse %s dans un fichier d'app", async (nom) => {
    const code = nom === "fetch" ? 'fetch("/api/x");' : `new ${nom}("/x");`;
    const liste = globales(await messages(code, "src/app/App.ts"));
    expect(liste).toHaveLength(1);
    expect(liste[0].message).toMatch(/api\.ts/);
  });

  it("refuse fetch dans un composant", async () => {
    expect(globales(await messages('fetch("/api/x");', "src/composants/Liste.tsx"))).toHaveLength(1);
  });

  it("refuse fetch dans le store d'un module", async () => {
    expect(globales(await messages('fetch("/api/x");', "src/fonctionnalites/panier/store.ts"))).toHaveLength(1);
  });

  it.each(["src/fonctionnalites/panier/api.ts", "src/lib/http.ts", "src/lib/reseau/ws.ts"])(
    "accepte les trois dans %s",
    async (fichier) => {
      const code = 'fetch("/x"); new XMLHttpRequest(); new WebSocket("/x");';
      expect(globales(await messages(code, fichier))).toEqual([]);
    },
  );
});

describe("dangerouslySetInnerHTML", () => {
  it("est refusé comme attribut JSX", async () => {
    const code = "export const A = () => <div dangerouslySetInnerHTML={{ __html: x }} />;";
    const liste = htmlBrut(await messages(code, "src/composants/A.tsx"));
    expect(liste.length).toBeGreaterThanOrEqual(1);
    expect(liste[0].message).toMatch(/XSS/);
  });

  it("est refusé dans un objet de props (createElement, spread)", async () => {
    const code = 'export const props = { dangerouslySetInnerHTML: { __html: "x" } };';
    expect(htmlBrut(await messages(code, "src/composants/props.ts"))).toHaveLength(1);
  });

  it("accepte un rendu de texte", async () => {
    const code = "export const A = () => <div>{x}</div>;";
    expect(htmlBrut(await messages(code, "src/composants/A.tsx"))).toEqual([]);
  });

  it("est aussi refusé dans un paquet", async () => {
    const eslint = new ESLint({
      cwd: fileURLToPath(new URL("./fixtures", import.meta.url)),
      overrideConfigFile: true,
      overrideConfig: [
        { files: ["**/*.{ts,tsx}"], languageOptions: { parser: tseslint.parser } },
        ...frontieresPaquet("ui"),
      ],
    });
    const racine = fileURLToPath(new URL("./fixtures", import.meta.url));
    const [resultat] = await eslint.lintText("export const A = () => <p dangerouslySetInnerHTML={{ __html: x }} />;", {
      filePath: `${racine}/src/paquet/A.tsx`,
    });
    expect(htmlBrut(resultat.messages).length).toBeGreaterThanOrEqual(1);
  });
});

describe("frontières des paquets", () => {
  const RACINE_FIXTURES = fileURLToPath(new URL("./fixtures", import.meta.url));

  async function restrictions(paquet, code) {
    const eslint = new ESLint({
      cwd: RACINE_FIXTURES,
      overrideConfigFile: true,
      overrideConfig: [
        { files: ["**/*.ts"], languageOptions: { parser: tseslint.parser } },
        ...frontieresPaquet(paquet),
      ],
    });
    const [resultat] = await eslint.lintText(code, { filePath: `${RACINE_FIXTURES}/src/paquet/x.ts` });
    return restreints(resultat.messages);
  }

  it.each([
    ["utils", 'import { z } from "zod";'],
    ["utils", 'import { cn } from "./cn";'],
    ["ui", 'import { cva } from "class-variance-authority";'],
    ["ui", 'import { cn } from "@anitche/utils";'],
    ["api-client", 'import { QueryClient } from "@tanstack/react-query";'],
    ["api-client", 'import { formaterFcfa } from "@anitche/utils";'],
    ["auth", 'import { create } from "zustand";'],
    ["auth", 'import { Button } from "@anitche/ui/button";'],
    ["auth", 'import { creerClients } from "@anitche/api-client";'],
  ])("%s accepte : %s", async (paquet, code) => {
    expect(await restrictions(paquet, code)).toEqual([]);
  });

  it.each([
    ["utils", 'import { useState } from "react";', "React"],
    ["utils", 'import { createRoot } from "react-dom/client";', "React DOM"],
    ["utils", 'import { creerClients } from "@anitche/api-client";', "un autre paquet"],
    ["utils", 'import { create } from "zustand";', "zustand"],
    ["utils", 'import { useQuery } from "@tanstack/react-query";', "TanStack Query"],
    ["ui", 'import { useSession } from "@anitche/auth";', "auth"],
    ["ui", 'import { creerClients } from "@anitche/api-client";', "api-client"],
    ["ui", 'import { creerClients } from "@anitche/api-client/testing";', "un sous-chemin d'api-client"],
    ["ui", 'import { create } from "zustand";', "zustand"],
    ["ui", 'import { useNavigate } from "react-router";', "react-router"],
    ["ui", 'import { useQuery } from "@tanstack/react-query";', "TanStack Query"],
    ["ui", 'import createClient from "openapi-fetch";', "openapi-fetch"],
    ["api-client", 'import { Button } from "@anitche/ui/button";', "ui"],
    ["api-client", 'import { useSession } from "@anitche/auth";', "auth"],
    ["api-client", 'import { useState } from "react";', "React"],
    ["api-client", 'import { create } from "zustand";', "zustand"],
    ["auth", 'import { App } from "@anitche/app-client";', "un portail par son nom"],
    ["auth", 'import { App } from "../../../apps/client/src/app/App";', "un portail par chemin"],
    ["auth", 'import { cn } from "../../ui/src/lib/cn";', "un autre paquet par chemin"],
  ])("%s refuse : %s (%s)", async (paquet, code) => {
    expect(await restrictions(paquet, code)).toHaveLength(1);
  });

  it("refuse un paquet inconnu plutôt que de ne rien vérifier", () => {
    expect(() => frontieresPaquet("inconnu")).toThrow(/paquet inconnu/);
  });
});

describe("frontières internes d'un portail (fixtures/)", () => {
  const RACINE_FIXTURES = fileURLToPath(new URL("./fixtures", import.meta.url));

  async function violations(fichier) {
    const eslint = new ESLint({
      cwd: RACINE_FIXTURES,
      overrideConfigFile: true,
      overrideConfig: [
        { files: ["**/*.ts"], languageOptions: { parser: tseslint.parser } },
        ...frontieres("client", RACINE_FIXTURES),
      ],
    });
    const [resultat] = await eslint.lintFiles([fichier]);
    return resultat.messages.filter((m) => m.ruleId === "boundaries/dependencies");
  }

  it.each([
    "src/app/ok.ts",
    "src/composants/ok.ts",
    "src/fonctionnalites/panier/usage-interne.ts",
    "src/fonctionnalites/panier/vers-autre-module.ts",
  ])("accepte %s", async (fichier) => {
    expect(await violations(fichier)).toEqual([]);
  });

  it.each([
    ["src/app/vers-interieur-module.ts", "un fichier interne d'un module"],
    ["src/mises-en-page/vers-app.ts", "app/ depuis mises-en-page/"],
    ["src/composants/vers-app.ts", "app/ depuis composants/"],
    ["src/composants/vers-module.ts", "un module depuis composants/"],
    ["src/fonctionnalites/panier/vers-layout.ts", "mises-en-page/ depuis un module"],
  ])("refuse %s (%s)", async (fichier) => {
    expect(await violations(fichier)).toHaveLength(1);
  });
});
