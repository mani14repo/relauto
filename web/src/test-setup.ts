import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// jsdom lacks the browser APIs Carbon's shell and tabs use.
if (!window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: query.includes("min-width: 66rem"), media: query, onchange: null,
    addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  }) as MediaQueryList;
}
if (!("ResizeObserver" in window)) {
  (window as any).ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
}
if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.location.hash = "";
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.className = "";
});
