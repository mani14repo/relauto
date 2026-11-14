import { GlobalTheme } from "@carbon/react";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

/** As in the OIP portal: Carbon g100 (dark) by default, white (light) as the alternative, remembered per browser. */
export type ThemeName = "dark" | "light";
export const THEME_KEY = "sra-theme";
const CARBON = { dark: "g100", light: "white" } as const;

export function storedTheme(): ThemeName {
  try { return window.localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark"; } catch { return "dark"; }
}

/** Sets <html data-theme> and the matching Carbon zone class, so every token (and portalled popovers) follow it. */
export function applyTheme(t: ThemeName) {
  const html = document.documentElement;
  html.dataset.theme = t;
  html.classList.remove("cds--g100", "cds--white");
  html.classList.add(`cds--${CARBON[t]}`);
  html.style.colorScheme = t;
}

const Ctx = createContext<{ theme: ThemeName; toggle: () => void }>({ theme: "dark", toggle: () => {} });

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<ThemeName>(storedTheme);
  useEffect(() => {
    applyTheme(theme);
    try { window.localStorage.setItem(THEME_KEY, theme); } catch { /* private window: the choice lasts this visit */ }
  }, [theme]);
  const toggle = useCallback(() => setTheme((t) => (t === "dark" ? "light" : "dark")), []);
  return (
    <Ctx.Provider value={{ theme, toggle }}>
      <GlobalTheme theme={CARBON[theme]}>{children}</GlobalTheme>
    </Ctx.Provider>
  );
}

export const useTheme = () => useContext(Ctx);
