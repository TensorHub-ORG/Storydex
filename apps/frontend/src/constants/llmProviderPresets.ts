export type LlmProviderType = "openai_compatible" | "openai_responses" | "anthropic_messages";

export interface LlmProviderPreset {
  id: string;
  display: string;
  type: LlmProviderType;
  baseUrl: string;
  apiKeyUrl: string;
  apiKeyPlaceholder: string;
  category: "official" | "aggregator";
}

// Model access varies by account and changes more often than API endpoints, so
// presets leave model selection editable and rely on the existing model fetcher.
export const LLM_PROVIDER_PRESETS: readonly LlmProviderPreset[] = [
  {
    id: "openai",
    display: "OpenAI",
    type: "openai_responses",
    baseUrl: "https://api.openai.com/v1",
    apiKeyUrl: "https://platform.openai.com/api-keys",
    apiKeyPlaceholder: "sk-...",
    category: "official"
  },
  {
    id: "anthropic",
    display: "Anthropic",
    type: "anthropic_messages",
    baseUrl: "https://api.anthropic.com",
    apiKeyUrl: "https://console.anthropic.com/settings/keys",
    apiKeyPlaceholder: "sk-ant-...",
    category: "official"
  },
  {
    id: "deepseek",
    display: "DeepSeek",
    type: "openai_compatible",
    baseUrl: "https://api.deepseek.com/v1",
    apiKeyUrl: "https://platform.deepseek.com/api_keys",
    apiKeyPlaceholder: "sk-...",
    category: "official"
  },
  {
    id: "kimi",
    display: "Kimi",
    type: "openai_compatible",
    baseUrl: "https://api.moonshot.cn/v1",
    apiKeyUrl: "https://platform.moonshot.cn/console/api-keys",
    apiKeyPlaceholder: "sk-...",
    category: "official"
  },
  {
    id: "zhipu",
    display: "智谱 GLM",
    type: "openai_compatible",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    apiKeyUrl: "https://open.bigmodel.cn/usercenter/apikeys",
    apiKeyPlaceholder: "请输入 API Key",
    category: "official"
  },
  {
    id: "bailian",
    display: "阿里云百炼",
    type: "openai_compatible",
    baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    apiKeyUrl: "https://bailian.console.aliyun.com/#/api-key",
    apiKeyPlaceholder: "sk-...",
    category: "official"
  },
  {
    id: "minimax",
    display: "MiniMax",
    type: "openai_compatible",
    baseUrl: "https://api.minimaxi.com/v1",
    apiKeyUrl: "https://platform.minimaxi.com/user-center/basic-information/interface-key",
    apiKeyPlaceholder: "请输入 API Key",
    category: "official"
  },
  {
    id: "siliconflow",
    display: "硅基流动",
    type: "openai_compatible",
    baseUrl: "https://api.siliconflow.cn/v1",
    apiKeyUrl: "https://cloud.siliconflow.cn/account/ak",
    apiKeyPlaceholder: "sk-...",
    category: "aggregator"
  },
  {
    id: "openrouter",
    display: "OpenRouter",
    type: "openai_compatible",
    baseUrl: "https://openrouter.ai/api/v1",
    apiKeyUrl: "https://openrouter.ai/keys",
    apiKeyPlaceholder: "sk-or-...",
    category: "aggregator"
  },
  {
    id: "gemini",
    display: "Google Gemini",
    type: "openai_compatible",
    baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai",
    apiKeyUrl: "https://aistudio.google.com/app/apikey",
    apiKeyPlaceholder: "AIza...",
    category: "official"
  },
  {
    id: "xai",
    display: "xAI",
    type: "openai_compatible",
    baseUrl: "https://api.x.ai/v1",
    apiKeyUrl: "https://console.x.ai/",
    apiKeyPlaceholder: "xai-...",
    category: "official"
  }
];

export function findLlmProviderPreset(
  baseUrl: string,
  type: LlmProviderType
): LlmProviderPreset | undefined {
  const normalizedUrl = normalizeBaseUrl(baseUrl);
  return LLM_PROVIDER_PRESETS.find(
    (preset) => preset.type === type && normalizeBaseUrl(preset.baseUrl) === normalizedUrl
  );
}

function normalizeBaseUrl(value: string): string {
  return String(value || "").trim().replace(/\/+$/, "").toLowerCase();
}
