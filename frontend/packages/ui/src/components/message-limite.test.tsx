import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MessageLimite } from "./message-limite";

const message = (restant: number) => `Réessayez dans ${restant} s`;

/** Fait passer `secondes` secondes une par une : React replanifie le minuteur après chaque rendu. */
function passer(secondes: number) {
  for (let i = 0; i < secondes; i += 1) {
    act(() => {
      vi.advanceTimersByTime(1000);
    });
  }
}

describe("MessageLimite", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("décompte seconde par seconde et appelle surFin une seule fois à 0", () => {
    const surFin = vi.fn();
    render(<MessageLimite secondes={3} message={message} surFin={surFin} />);
    expect(screen.getByRole("status")).toHaveTextContent("Réessayez dans 3 s");

    passer(1);
    expect(screen.getByRole("status")).toHaveTextContent("Réessayez dans 2 s");
    expect(surFin).not.toHaveBeenCalled();

    passer(2);
    expect(screen.getByRole("status")).toHaveTextContent("Réessayez dans 0 s");
    expect(surFin).toHaveBeenCalledTimes(1);

    passer(5);
    expect(surFin).toHaveBeenCalledTimes(1);
  });

  it("un nouveau délai relance le compte", () => {
    const { rerender } = render(<MessageLimite secondes={5} message={message} />);
    passer(2);
    expect(screen.getByRole("status")).toHaveTextContent("Réessayez dans 3 s");

    rerender(<MessageLimite secondes={10} message={message} />);
    expect(screen.getByRole("status")).toHaveTextContent("Réessayez dans 10 s");
  });

  it("0 seconde au départ : pas de surFin", () => {
    const surFin = vi.fn();
    render(<MessageLimite secondes={0} message={message} surFin={surFin} />);
    passer(3);
    expect(screen.getByRole("status")).toHaveTextContent("Réessayez dans 0 s");
    expect(surFin).not.toHaveBeenCalled();
  });

  it("démonté : plus de minuteur actif", () => {
    const surFin = vi.fn();
    const { unmount } = render(<MessageLimite secondes={2} message={message} surFin={surFin} />);
    unmount();
    passer(5);
    expect(surFin).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });
});
