import type { PresetDocumentPayload, PresetModulePayload } from "@/api/presets";
import templates from "../../../backend/services/writing_guidance.json";

export const writingGuidanceTemplates: PresetModulePayload[] = templates;

export function writingGuidanceFor(document: PresetDocumentPayload): PresetModulePayload[] {
  return writingGuidanceTemplates.map((template) => ({
    ...template,
    ...document.guidanceModules?.find((module) => module.id === template.id)
  }));
}

export function updateWritingGuidance(
  document: PresetDocumentPayload, id: string, patch: Partial<PresetModulePayload>
): PresetDocumentPayload {
  const known = writingGuidanceFor(document).map((module) => module.id === id ? { ...module, ...patch } : module);
  const custom = (document.guidanceModules || []).filter((module) => !writingGuidanceTemplates.some((t) => t.id === module.id));
  return { ...document, guidanceModules: [...known, ...custom] };
}
