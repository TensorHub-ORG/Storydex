import type { PresetDocumentPayload, PresetModulePayload } from "@/api/presets";

// Storydex's own optional writing guidance. These are editable preset modules,
// not executable skills or rules for judging the author's prose.
const applicability = "仅在小说正文创作、续写和润色时采用以下建议。本次作者要求优先，其次沿用本书文风、视角和角色设定；与之冲突的建议不适用。";

export const writingGuidanceTemplates: PresetModulePayload[] = [
  {
    id: "writing_expression", title: "自然表达与人物声音", slot: "language_mechanics", priority: 30,
    scope: "global", enabledByDefault: true,
    content: `${applicability}\n让用词和句式服务于眼前的事、人物的感受与说话目的。删去没有新增信息的总结、重复的情绪解释和机械排比；保留有作用的心理描写、长句、叙述者评论和必要说明。对话应体现身份、关系和当下意图，不要让所有角色用同一种腔调，也不要靠口头禅或粗话制造差异。调整时读通前后句，保留动作与思路的连接，不把自然表达改成碎句堆叠。不要机械禁用某个词、标点或修辞。`
  },
  {
    id: "writing_scene_focus", title: "场景详略与叙事节奏", slot: "scene_module", priority: 29,
    scope: "global", enabledByDefault: true,
    content: `${applicability}\n把主要笔墨给人物作出选择、相互试探、承担代价或局势变化的过程。重要交锋写出动作、回应及后果；普通路程和重复步骤可以概述。细节应影响感知、气氛或行动，不为增加字数逐步直播，不为去除概述凭空添加微动作或冲突。按场景需要安排快慢，不强迫每段都抒情、每场都反转、每章末都总结主题。`
  },
  {
    id: "writing_causality", title: "因果连贯与信息边界", slot: "self_check", priority: 28,
    scope: "global", enabledByDefault: true,
    content: `${applicability}\n人物行动应接得上已知信息、动机、能力和当时处境；变化需要可见的诱因与代价。区分作者知道、读者已经看到、视角人物能够知道的事，保留悬念所需的信息差。检查时间、位置、物件、关系与前文是否衔接。润色时保持既有事实、动机和因果；不要为了句子顺口新造证据、替人物下结论或把猜测写成事实。无法从资料确定的重要设定保留其不确定性，按作者任务处理。`
  }
];

export function writingGuidanceFor(document: PresetDocumentPayload): PresetModulePayload[] {
  return writingGuidanceTemplates.map((template) => ({
    ...template,
    enabledByDefault: false,
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
