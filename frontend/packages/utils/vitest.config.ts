import { testConfigPaquet } from "@anitche/config/vitest";
import { defineConfig } from "vitest/config";

// Fuseau du process volontairement différent d'Abidjan : les tests de dates prouvent que
// le résultat ne dépend pas du fuseau de la machine.
process.env.TZ = "America/New_York";

export default defineConfig({ test: testConfigPaquet({ environment: "node" }) });
