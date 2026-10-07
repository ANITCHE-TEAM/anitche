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

const MESSAGE_PAQUET_ORDRE =
  "Sens des dépendances : utils ← api-client ← auth, et ui ← auth ; ui ne connaît ni api-client ni auth.";
const MESSAGE_PAQUET_APP =
  "Un paquet n'importe jamais un portail : la chose partagée descend dans le paquet, pas l'inverse.";
const MESSAGE_PAQUET_FICHIER =
  "Un paquet n'importe un autre paquet que par son nom (@anitche/<paquet>), jamais par un chemin vers ses fichiers.";

const PAQUETS = ["utils", "ui", "api-client", "auth"];

/** Imports interdits à tout paquet : un portail, ou les fichiers d'un autre paquet. */
function horsPerimetre(nom) {
  const voisins = PAQUETS.filter((paquet) => paquet !== nom);
  return [
    { group: ["@anitche/app-*", "**/apps/**"], message: MESSAGE_PAQUET_APP },
    { group: ["**/packages/**", ...voisins.map((paquet) => `**/${paquet}/src/**`)], message: MESSAGE_PAQUET_FICHIER },
  ];
}

const REACT = ["react", "react/*", "react-dom", "react-dom/*"];
const DONNEES_SERVEUR = ["@tanstack/*", "openapi-fetch", "openapi-typescript", "openapi-typescript-helpers"];

/** Imports interdits à chaque paquet (arch § 9.1 : « Ne peut jamais importer »). */
const RESTRICTIONS_PAQUETS = {
  utils: [
    {
      group: [...REACT, "@anitche/*", "zustand", "zustand/*", ...DONNEES_SERVEUR],
      message: "utils reste pur : ni React, ni autre paquet, ni état, ni client HTTP.",
    },
  ],
  ui: [
    {
      group: ["@anitche/api-client", "@anitche/api-client/*", "@anitche/auth", "@anitche/auth/*"],
      message: MESSAGE_PAQUET_ORDRE,
    },
    {
      group: ["zustand", "zustand/*", "react-router", "react-router/*", ...DONNEES_SERVEUR],
      message: "ui ne contient aucune donnée serveur, aucun état global, aucune navigation.",
    },
  ],
  "api-client": [
    {
      group: ["@anitche/ui", "@anitche/ui/*", "@anitche/auth", "@anitche/auth/*"],
      message: MESSAGE_PAQUET_ORDRE,
    },
    {
      group: [...REACT, "zustand", "zustand/*"],
      message: "api-client n'importe pas React (hors @tanstack/react-query) ni l'état global.",
    },
  ],
  auth: [],
};

/**
 * Frontières d'un paquet de `packages/` (`nom` : "utils", "ui", "api-client" ou "auth"),
 * à placer dans la config ESLint de ce paquet.
 */
export function frontieresPaquet(nom) {
  if (!PAQUETS.includes(nom)) {
    throw new Error(`frontieresPaquet : paquet inconnu « ${nom} » (attendu : ${PAQUETS.join(", ")}).`);
  }
  const specifiques = RESTRICTIONS_PAQUETS[nom];
  return [
    {
      name: `anitche/frontieres-paquet-${nom}`,
      files: ["src/**/*.{ts,tsx}"],
      rules: {
        "no-restricted-imports": ["error", { patterns: [...horsPerimetre(nom), ...specifiques] }],
      },
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
