import js from "@eslint/js";
import { defineConfig, globalIgnores } from "eslint/config";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";
import { frontieres } from "./frontieres.js";

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
