import boundaries from "eslint-plugin-boundaries";

const PORTAILS = ["client", "vendeur", "livreur", "admin"];

const MESSAGE_APP =
  "Un portail n'importe jamais un autre portail : la chose partagée va dans un paquet de packages/.";
const MESSAGE_ZUSTAND =
  "zustand n'est autorisé que dans packages/auth et dans fonctionnalites/<module>/store.ts.";

const ELEMENTS = [
  { type: "app", pattern: "src/app", partialMatch: false },
  { type: "mises-en-page", pattern: "src/mises-en-page", partialMatch: false },
  { type: "composants", pattern: "src/composants", partialMatch: false },
  { type: "lib", pattern: "src/lib", partialMatch: false },
  { type: "fonctionnalite", pattern: "src/fonctionnalites/*", partialMatch: false, capture: ["module"] },
];

const types = (...liste) => ({ types: { anyOf: liste } });

/** Matrice du § 10.1 de architecture_frontend.md. */
const POLITIQUES = [
  {
    from: { element: { type: "mises-en-page" } },
    disallow: { to: { element: { type: "app" } } },
  },
  {
    from: { element: { type: "fonctionnalite" } },
    disallow: { to: { element: types("app", "mises-en-page") } },
  },
  {
    from: { element: types("composants", "lib") },
    disallow: { to: { element: types("app", "mises-en-page", "fonctionnalite") } },
  },
  // Un module n'est atteint de l'extérieur que par son index.
  {
    disallow: {
      to: { element: { type: "fonctionnalite", fileInternalPath: "!index.{ts,tsx}" } },
    },
  },
];

/** Imports interdits à tout fichier d'un portail. */
function motifsApps(nom) {
  const autres = PORTAILS.filter((portail) => portail !== nom);
  return [
    { group: ["@anitche/app-*"], message: MESSAGE_APP },
    {
      group: ["**/apps/**", ...autres.map((portail) => `**/${portail}/src/**`)],
      message: MESSAGE_APP,
    },
  ];
}

/**
 * Frontières d'un portail (`nom`), à placer dans la config ESLint de ce portail.
 * `racine` : dossier du portail ; les chemins `files` et les éléments en sont relatifs.
 */
export function frontieres(nom, racine) {
  const apps = motifsApps(nom);
  return [
    {
      name: `anitche/frontieres-${nom}`,
      files: ["src/**/*.{ts,tsx}"],
      rules: {
        "no-restricted-imports": [
          "error",
          { patterns: [...apps, { group: ["zustand", "zustand/*"], message: MESSAGE_ZUSTAND }] },
        ],
      },
    },
    {
      name: `anitche/frontieres-${nom}-interne`,
      files: ["src/**/*.{ts,tsx}"],
      plugins: { boundaries },
      settings: {
        "boundaries/root-path": racine,
        "boundaries/elements": ELEMENTS,
        "import/resolver": { node: { extensions: [".js", ".ts", ".tsx"] } },
      },
      rules: { "boundaries/dependencies": ["error", { default: "allow", policies: POLITIQUES }] },
    },
    {
      name: `anitche/frontieres-${nom}-store`,
      files: ["src/fonctionnalites/*/store.ts"],
      rules: { "no-restricted-imports": ["error", { patterns: apps }] },
    },
  ];
}
