import { afterAll, afterEach, beforeAll } from "vitest";
import { serveur } from "./serveur";

beforeAll(() => {
  serveur.listen({ onUnhandledFrame: "error" });
});
afterEach(() => {
  serveur.resetHandlers();
});
afterAll(() => {
  serveur.close();
});
