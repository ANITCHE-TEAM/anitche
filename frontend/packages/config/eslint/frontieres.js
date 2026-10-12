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

const MESSAGE_ENTREE =
  "Importer le paquet par son point d'entrée (@anitche/<paquet>), jamais par ses fichiers ni par un chemin vers packages/.";
const MESSAGE_CLIENT_HTTP =
  "Un portail passe par @anitche/api-client : ni openapi-fetch ni openapi-typescript directement.";
const MESSAGE_RESEAU =
  "Aucun appel réseau dans un composant ou une page : passer par fonctionnalites/<module>/api.ts (ou lib/) et @anitche/api-client.";
const MESSAGE_HTML =
  "dangerouslySetInnerHTML est interdit (risque XSS) : rendre du texte, ou assainir en amont dans un paquet dédié.";

const PAQUETS = ["utils", "ui", "api-client", "auth"];

/** Imports interdits à tout fichier d'un portail. */
function motifsApps(nom) {
  const autres = PORTAILS.filter((portail) => portail !== nom);
  return [
    { group: ["@anitche/app-*"], message: MESSAGE_APP },
    {
      group: ["**/apps/**", ...autres.map((portail) => `**/${portail}/src/**`)],
      message: MESSAGE_APP,
    },
    // F1 : un paquet s'importe par son point d'entrée déclaré (champ `exports`).
    {
      group: ["@anitche/*/src", "@anitche/*/src/**", "**/packages/**", ...PAQUETS.map((p) => `**/${p}/src/**`)],
      message: MESSAGE_ENTREE,
    },
    // F2 : le client HTTP généré reste derrière @anitche/api-client.
    {
      group: ["openapi-fetch", "openapi-fetch/*", "openapi-typescript", "openapi-typescript/*", "openapi-typescript-helpers"],
      message: MESSAGE_CLIENT_HTTP,
    },
  ];
}

const MESSAGE_PAQUET_ORDRE =
  "Sens des dépendances : utils ← api-client ← auth, et ui ← auth ; ui ne connaît ni api-client ni auth.";
const MESSAGE_PAQUET_APP =
  "Un paquet n'importe jamais un portail : la chose partagée descend dans le paquet, pas l'inverse.";
const MESSAGE_PAQUET_FICHIER =
  "Un paquet n'importe un autre paquet que par son nom (@anitche/<paquet>), jamais par un chemin vers ses fichiers.";

/** Interdit dans tout code frontend (portails et paquets). */
const SANS_HTML_BRUT = {
  "no-restricted-syntax": [
    "error",
    { selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']", message: MESSAGE_HTML },
    { selector: "Property[key.name='dangerouslySetInnerHTML']", message: MESSAGE_HTML },
  ],
};

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
        ...SANS_HTML_BRUT,
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
        ...SANS_HTML_BRUT,
      },
    },
    // F3 : le réseau n'est atteint que par api.ts d'un module et par lib/.
    {
      name: `anitche/frontieres-${nom}-reseau`,
      files: ["src/**/*.{ts,tsx}"],
      rules: {
        "no-restricted-globals": [
          "error",
          ...["fetch", "XMLHttpRequest", "WebSocket"].map((name) => ({ name, message: MESSAGE_RESEAU })),
        ],
      },
    },
    {
      name: `anitche/frontieres-${nom}-reseau-autorise`,
      files: ["src/fonctionnalites/*/api.ts", "src/lib/**/*.{ts,tsx}"],
      rules: { "no-restricted-globals": "off" },
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
