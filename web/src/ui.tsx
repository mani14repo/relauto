import { TrashCan } from "@carbon/icons-react";
import { ActionableNotification, InlineNotification, Modal } from "@carbon/react";
import type { ReactNode } from "react";
import type { Result, Risk } from "./api";

/** Status colours are Carbon support tokens (see styles.scss): green met, red gap, yellow insufficient, grey N/A. */
export function ResultBadge({ r }: { r: Result | null | undefined }) {
  const cls = !r ? "r-none" : r === "Insufficient Evidence" ? "r-Insufficient" : r === "Not Applicable" ? "r-NA" : `r-${r}`;
  return <span className={`badge ${cls}`}>{r ?? "Not evaluated"}</span>;
}

export function Severity({ s }: { s: Risk | null | undefined }) {
  return s ? <span className={`sev s-${s}`}>{s}</span> : <span className="muted">—</span>;
}

export function ErrorBox({ error }: { error: string | null }) {
  return error ? (
    <InlineNotification kind="error" lowContrast hideCloseButton role="alert" title="" subtitle={error} className="sra-notice" />
  ) : null;
}

/**
 * An info or warning message in Carbon's inline notification; `onClose` adds a close button. Carbon's inline
 * notification must not contain controls, so a message with a button passes it as `action` (an actionable notification).
 */
export function Notice({ kind = "info", children, onClose, action, className }: {
  kind?: "info" | "warning" | "success"; children: ReactNode; onClose?: () => void;
  action?: { label: string; onClick: () => void }; className?: string;
}) {
  if (action) {
    return (
      <ActionableNotification inline kind={kind} lowContrast role="status" hasFocus={false} title="" subtitle={children}
        actionButtonLabel={action.label} onActionButtonClick={action.onClick}
        hideCloseButton={!onClose} onCloseButtonClick={onClose} onClose={() => false}
        className={`sra-notice ${className ?? ""}`} />
    );
  }
  return (
    <InlineNotification kind={kind} lowContrast role="status" title="" subtitle={children as any}
      hideCloseButton={!onClose} onCloseButtonClick={onClose} onClose={() => false}
      className={`sra-notice ${className ?? ""}`} />
  );
}

export function Bar({ value }: { value: number | null }) {
  return <div className="bar" aria-label={`${value ?? 0}%`}><span style={{ width: `${Math.max(0, Math.min(100, value ?? 0))}%` }} /></div>;
}

export const RATING_COLOR: Record<string, string> = {
  "Not Ready": "var(--sra-gap)", "At Risk": "var(--sra-high)", Conditional: "var(--sra-ie)", Ready: "var(--sra-met)",
};

/** The readiness rating as a status dot and text, so the yellow and green stay readable on the white theme. */
export function Rating({ r, large }: { r: string | null | undefined; large?: boolean }) {
  if (!r) return <span className="muted">—</span>;
  return <span className={`rating ${large ? "rating--lg" : ""}`} style={{ ["--dot" as any]: RATING_COLOR[r] ?? "var(--cds-icon-secondary)" }}>{r}</span>;
}

const clock = (d: Date) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/** "as of 14:02": when the figures on a widget were loaded. */
export function AsOf({ at, className }: { at: Date | null | undefined; className?: string }) {
  if (!at) return null;
  return <span className={`sra-asof ${className ?? ""}`} title={at.toLocaleString()}>as of {clock(at)}</span>;
}

/** A KPI tile: label, a large IBM Plex Mono figure, a line of context, and an optional status edge. */
export function KpiTile({ label, value, status, children, asOf }: {
  label: string; value: ReactNode; status?: string; children?: ReactNode; asOf?: Date | null;
}) {
  return (
    <div className="card kpi-tile" style={status ? { ["--edge" as any]: status } : undefined}>
      <div className="kpi-tile__head"><span className="kpi-tile__label">{label}</span><AsOf at={asOf} /></div>
      <div className="kpi">{value}</div>
      {children}
    </div>
  );
}

/** A small modal "are you sure?" box: Escape or Cancel closes it, the danger button confirms. */
export function ConfirmDialog({ title, body, confirmLabel, busy, onConfirm, onCancel }: {
  title: string; body: string; confirmLabel: string; busy?: boolean; onConfirm: () => void; onCancel: () => void;
}) {
  return (
    <Modal open danger size="xs" modalHeading={title} primaryButtonText={busy ? "Deleting…" : confirmLabel}
      secondaryButtonText="Cancel" primaryButtonDisabled={busy} preventCloseOnClickOutside={busy}
      onRequestSubmit={() => { if (!busy) onConfirm(); }} onRequestClose={() => { if (!busy) onCancel(); }}
      selectorPrimaryFocus=".cds--modal-footer .cds--btn--secondary">
      <p className="confirm-body">{body}</p>
    </Modal>
  );
}

export function TrashIcon() {
  return <TrashCan size={16} aria-hidden="true" />;
}

export const deleteMessage = (name: string) =>
  `This removes "${name}" with its evidence files, results, findings, sign-offs and audit trail. It cannot be undone.`;
