export interface LiveTurnPhaseVisibilityInput {
  runStatus?: unknown;
  phase?: unknown;
  phaseStatus?: unknown;
}

function normalizeText(value: unknown): string {
  return String(value ?? "").trim().toLowerCase();
}

export function isRunActivelyStreaming(runStatus: unknown): boolean {
  return normalizeText(runStatus) === "running";
}

export function shouldShowLiveTurnPhase(input: LiveTurnPhaseVisibilityInput): boolean {
  const runStatus = normalizeText(input.runStatus);
  if (
    runStatus === "completed" ||
    runStatus === "committed" ||
    runStatus === "cancelled" ||
    runStatus === "stopped" ||
    runStatus === "error"
  ) {
    return false;
  }

  const phase = normalizeText(input.phase);
  const phaseStatus = normalizeText(input.phaseStatus);
  if (runStatus === "preview") {
    return phase === "awaiting_approval" || phaseStatus === "waiting_approval";
  }

  return runStatus === "running";
}
