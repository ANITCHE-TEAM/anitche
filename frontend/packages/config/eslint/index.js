import js from "@eslint/js";
import { defineConfig, globalIgnores } from "eslint/config";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";
import { frontieres, frontieresPaquet } from "./frontieres.js";

/**
 * Config ESLint d'un paquet de packages/.
 * @param {{ nom: "utils" | "ui" | "api-client" | "auth", racine: string, react?: boolean }} options
 *   `racine` : dossier du paquet (`import.meta.dirname` dans son eslint.config.js) ;
 *   `react` : règles des hooks et globales du navigateur (faux pour `utils`, qui n'a ni React ni DOM).
 */
export function configPaquet({ nom, racine, react = true }) {
  return defineConfig([
    globalIgnores(["dist", "src/generes"]),
    js.configs.recommended,
    tseslint.configs.recommendedTypeChecked,
    ...(react ? [reactHooks.configs.flat.recommended] : []),
    {
      languageOptions: {
        globals: react ? globals.browser : {},
        parserOptions: { projectService: true, tsconfigRootDir: racine },
      },
    },
    { files: ["**/*.js"], extends: [tseslint.configs.disableTypeChecked] },
    ...frontieresPaquet(nom),
  ]);
}

/**
 * Config ESLint d'un portail.
 * @param {{ nom: "client" | "vendeur" | "livreur" | "admin", racine: string }} options
 *   `racine` : dossier du portail (`import.meta.dirname` dans son eslint.config.js).
 */
export function configApp({ nom, racine }) {
  return defineConfig([
    globalIgnores(["dist"]),
    js.configs.recommended,
    tseslint.configs.recommendedTypeChecked,
    reactHooks.configs.flat.recommended,
    reactRefresh.configs.vite,
    {
      languageOptions: {
        globals: globals.browser,
        parserOptions: { projectService: true, tsconfigRootDir: racine },
      },
    },
    { files: ["**/*.js"], extends: [tseslint.configs.disableTypeChecked] },
    ...frontieres(nom, racine),
  ]);
}
