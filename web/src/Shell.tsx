import { Add, Asleep, Dashboard, Document, Light, SidePanelClose, SidePanelOpen } from "@carbon/icons-react";
import {
  Header, HeaderGlobalBar, HeaderMenuButton, HeaderName, SideNav, SideNavItems, SideNavLink, SkipToContent, Tag,
} from "@carbon/react";
import { useEffect, useState, type ReactNode } from "react";
import { api, getUser, setUser } from "./api";
import { useTheme } from "./theme";
import { AsOf } from "./ui";

const NAV_KEY = "sra-nav";
const LG = "(min-width: 66rem)";   // Carbon's lg breakpoint: below it the nav overlays the page

function storedNav() {
  try { return window.localStorage.getItem(NAV_KEY) !== "collapsed"; } catch { return true; }
}
const isLg = () => typeof window.matchMedia === "function" && window.matchMedia(LG).matches;

type Llm = { enabled: boolean; model: string | null; reachable: boolean };

/** Header status: the local LLM the run uses, when that was checked, and who is signed in (sent as X-User). */
function StatusBar() {
  const [llm, setLlm] = useState<Llm | null>(null);
  const [checked, setChecked] = useState<Date | null>(null);
  const [user, setU] = useState(getUser());
  useEffect(() => {
    api.llmHealth().then(setLlm).catch(() => setLlm(null)).finally(() => setChecked(new Date()));
  }, []);
  return (
    <HeaderGlobalBar className="sra-status">
      {llm && (llm.enabled === false
        ? <Tag type="gray" size="sm" title="LLM_ENABLED=false: runs are rules only and criterion chat is off">LLM off</Tag>
        : <Tag type={llm.reachable ? "green" : "red"} size="sm" title="Model used for AI-assisted criteria">
            LLM {llm.model}: {llm.reachable ? "ready" : "unavailable"}
          </Tag>)}
      {checked && <AsOf at={checked} className="sra-status__asof" />}
      <input className="sra-status__user" aria-label="Your name or email" placeholder="your.name@company.com" value={user}
        onChange={(e) => { setU(e.target.value); setUser(e.target.value); }} />
    </HeaderGlobalBar>
  );
}

export type NavKey = "home" | "new" | "assessment";

/**
 * The OIP portal frame: Carbon header with a status bar, and a collapsible side nav that slots SRA's pages under
 * OIP's "RELIABILITY (SRE)" group. The footer holds the theme switch and the collapse control.
 */
export default function Shell({ active, current, children }: {
  active: NavKey; current?: { id: string; label: string } | null; children: ReactNode;
}) {
  const { theme, toggle } = useTheme();
  const [expanded, setExpanded] = useState(storedNav);
  const setNav = (v: boolean) => {
    setExpanded(v);
    try { window.localStorage.setItem(NAV_KEY, v ? "expanded" : "collapsed"); } catch { /* ignore */ }
  };
  // Below lg the open nav covers the page, so following a link closes it.
  const followed = () => { if (!isLg()) setExpanded(false); };

  return (
    <div className={`sra-shell ${expanded ? "sra-shell--expanded" : "sra-shell--rail"}`}>
      <Header aria-label="Site Reliability Assessment">
        <SkipToContent />
        <HeaderMenuButton aria-label={expanded ? "Collapse navigation" : "Open navigation"} isActive={expanded}
          aria-expanded={expanded} onClick={() => setNav(!expanded)} isCollapsible />
        <HeaderName href="#/" prefix="SRA">Site Reliability Assessment</HeaderName>
        <StatusBar />
      </Header>
      <SideNav aria-label="Side navigation" isRail expanded={expanded} addMouseListeners={false} addFocusListeners={false}
        onToggle={(e, v) => {
          // Carbon closes the nav when focus leaves it; on a wide screen the nav sits beside the page, so keep it open.
          if (!v && isLg() && typeof e !== "boolean" && e.type === "blur") return;
          setExpanded(v);
        }}
        onOverlayClick={() => setExpanded(false)}>
        <SideNavItems>
          <li className="sra-nav-group" aria-hidden={!expanded}>Reliability (SRE)</li>
          <SideNavLink renderIcon={Dashboard} href="#/" isActive={active === "home"} onClick={followed}>Reliability assessments</SideNavLink>
          <SideNavLink renderIcon={Add} href="#/new" isActive={active === "new"} onClick={followed}>New assessment</SideNavLink>
          {current && (
            <SideNavLink renderIcon={Document} href={`#/a/${current.id}`} isActive={active === "assessment"} onClick={followed}
              className="sra-nav-current" title={current.label}>{current.label}</SideNavLink>
          )}
        </SideNavItems>
        <ul className="sra-nav-footer">
          <SideNavLink element="button" type="button" renderIcon={theme === "dark" ? Light : Asleep} onClick={toggle}
            aria-label={theme === "dark" ? "Switch to the light theme" : "Switch to the dark theme"}>
            {theme === "dark" ? "Light theme" : "Dark theme"}
          </SideNavLink>
          <SideNavLink element="button" type="button" renderIcon={expanded ? SidePanelClose : SidePanelOpen}
            className="sra-nav-collapse" onClick={() => setNav(!expanded)}
            aria-label={expanded ? "Collapse navigation" : "Expand navigation"}>
            {expanded ? "Collapse" : "Expand"}
          </SideNavLink>
        </ul>
      </SideNav>
      <main id="main-content" className="sra-content">{children}</main>
    </div>
  );
}
