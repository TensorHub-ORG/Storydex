export interface TurnPhaseChecklistInput {
  phase?: unknown;
  label?: unknown;
  status?: unknown;
}

export interface PendingWriteChecklistInput {
  hasPendingWrite?: unknown;
  pendingWriteExpired?: unknown;
}

export interface TurnPhaseChecklistItem {
  id: string;
  title: string;
  status: string;
}

function text(value: unknown): string {
  return String(value ?? "").trim();
}

export function buildTurnPhaseChecklistItem(input: TurnPhaseChecklistInput): TurnPhaseChecklistItem | null {
  const phase = text(input.phase);
  if (!phase) {
    return null;
  }
  const status = text(input.status) || "in_progress";
  const label = text(input.label) || defaultPhaseTitle(phase);
  return {
    id: `turn-phase-${phase}`,
    title: label,
    status,
  };
}

export function buildPendingWriteChecklistItem(input: PendingWriteChecklistInput): TurnPhaseChecklistItem | null {
  if (!input.hasPendingWrite || input.pendingWriteExpired) {
    return null;
  }
  return {
    id: "pending-write-preview",
    title: "等待用户审批",
    status: "waiting_approval",
  };
}

function defaultPhaseTitle(phase: string): string {
  if (phase === "planning") {
    return "规划本轮执行";
  }
  if (phase === "awaiting_approval") {
    return "等待用户审批";
  }
  return phase;
}
