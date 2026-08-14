use anyhow::Context;
use anyhow::Result;
use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use coomi_engine::ReasoningEffort;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProviderKind {
    OpenAiCompatible,
    OpenAiResponses,
    AnthropicMessages,
    GeminiNative,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RemoteCompactionMode {
    Legacy,
    #[default]
    V2,
}

/// Whether a concrete provider/model combination can honor an explicit
/// reasoning level. This is deliberately separate from whether the model can
/// reason internally: many gateways expose the latter without exposing a
/// client-controlled knob.
#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ReasoningSupport {
    Supported,
    Unsupported,
    #[default]
    Unknown,
}

/// How a selected reasoning level is applied to the upstream request.
///
/// `Native` means the provider exposes a documented/request-level field.
/// `Prompt` is a local soft control for compatible models that do not expose
/// such a field; it must never be presented as native provider support.
#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ReasoningControlMode {
    #[default]
    Auto,
    Native,
    Prompt,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ReasoningCapabilitySource {
    ModelConfig,
    ProviderConfig,
    ModelRule,
    #[default]
    Unknown,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ReasoningWireField {
    pub path: String,
    pub value: Value,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ReasoningLevelCapability {
    pub effort: ReasoningEffort,
    #[serde(default)]
    pub wire_fields: Vec<ReasoningWireField>,
    #[serde(default)]
    pub control: ReasoningControlMode,
    #[serde(default)]
    pub route_sensitive: bool,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ReasoningCapability {
    pub support: ReasoningSupport,
    #[serde(default)]
    pub levels: Vec<ReasoningLevelCapability>,
    pub source: ReasoningCapabilitySource,
    #[serde(default)]
    pub prompt_fallback: bool,
    #[serde(default)]
    pub route_sensitive: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fallback_reason: Option<String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ReasoningRequestPlan {
    pub requested: ReasoningEffort,
    #[serde(default)]
    pub control: ReasoningControlMode,
    #[serde(default)]
    pub sent: bool,
    #[serde(default)]
    pub prompt_applied: bool,
    #[serde(default)]
    pub wire_fields: Vec<ReasoningWireField>,
    pub support: ReasoningSupport,
    pub source: ReasoningCapabilitySource,
    #[serde(default)]
    pub route_sensitive: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fallback_reason: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
pub struct ReasoningProfileSettings {
    #[serde(default, alias = "supports_reasoning_effort")]
    pub supported: Option<bool>,
    #[serde(default)]
    pub levels: Option<Vec<ReasoningEffort>>,
    #[serde(default, alias = "reasoning_effort_map")]
    pub effort_map: BTreeMap<String, String>,
    #[serde(default)]
    pub route_sensitive: Option<bool>,
    /// Defaults to false. Prompt control is a best-effort local instruction,
    /// not proof that the upstream model honored a reasoning level, so it
    /// must be explicitly enabled per profile/provider.
    #[serde(default, alias = "supports_prompt_reasoning")]
    pub prompt_fallback: Option<bool>,
}

impl ProviderKind {
    fn from_config(provider_type: &str, tool_protocol: Option<&str>) -> Result<Self> {
        let provider_type = provider_type.trim();
        // Older coomi-rs builds incorrectly stored the API wire protocol in
        // `tool_protocol`. Keep those files readable, but do not let the new
        // tool-calling preference override the selected API compatibility.
        let legacy_protocol = tool_protocol.filter(|value| {
            matches!(
                value
                    .trim()
                    .to_ascii_lowercase()
                    .replace(['-', ' '], "_")
                    .as_str(),
                "openai_compatible" | "openai_responses" | "anthropic_messages" | "gemini_native"
            )
        });
        let value = if provider_type.is_empty() || provider_type.eq_ignore_ascii_case("generic") {
            legacy_protocol.unwrap_or(provider_type)
        } else {
            provider_type
        }
        .trim()
        .to_ascii_lowercase()
        .replace(['-', ' '], "_");
        match value.as_str() {
            "generic" | "deepseek" | "openai_compatible" | "chat_completions" => {
                Ok(Self::OpenAiCompatible)
            }
            "openai" | "openai_responses" | "responses" => Ok(Self::OpenAiResponses),
            "anthropic" | "anthropic_messages" => Ok(Self::AnthropicMessages),
            "gemini" | "gemini_native" => Ok(Self::GeminiNative),
            other => anyhow::bail!("unsupported provider protocol: {other}"),
        }
    }
}

#[derive(Clone)]
pub struct ProviderConfig {
    pub id: String,
    pub kind: ProviderKind,
    pub display: String,
    pub api_key: String,
    pub base_url: String,
    pub model: String,
    pub fast_model: Option<String>,
    pub capabilities: coomi_engine::ModelCapabilities,
    pub supports_reasoning_effort: Option<bool>,
    pub reasoning_prompt_fallback: Option<bool>,
    pub reasoning_effort_map: BTreeMap<String, String>,
    pub reasoning_profiles: BTreeMap<String, ReasoningProfileSettings>,
    pub remote_compaction_mode: RemoteCompactionMode,
}

impl std::fmt::Debug for ProviderConfig {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ProviderConfig")
            .field("id", &self.id)
            .field("kind", &self.kind)
            .field("display", &self.display)
            .field("api_key", &"[redacted]")
            .field("base_url", &self.base_url)
            .field("model", &self.model)
            .field("fast_model", &self.fast_model)
            .field("capabilities", &self.capabilities)
            .field("supports_reasoning_effort", &self.supports_reasoning_effort)
            .field("reasoning_prompt_fallback", &self.reasoning_prompt_fallback)
            .field("reasoning_effort_map", &self.reasoning_effort_map)
            .field("reasoning_profiles", &self.reasoning_profiles)
            .field("remote_compaction_mode", &self.remote_compaction_mode)
            .finish()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ModelChoice {
    pub selector: String,
    pub provider_id: String,
    pub provider_display: String,
    pub model: String,
    pub is_fast: bool,
}

#[derive(Debug)]
pub struct ProviderRegistry {
    active: String,
    providers: BTreeMap<String, ProviderConfig>,
}

#[derive(Clone, Deserialize, Serialize)]
pub struct ProviderDocument {
    #[serde(default)]
    pub active: String,
    #[serde(default)]
    pub providers: BTreeMap<String, ProviderSettings>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

#[derive(Clone, Deserialize, Serialize)]
pub struct ProviderSettings {
    #[serde(rename = "type", default = "default_provider_type")]
    pub provider_type: String,
    #[serde(default)]
    pub tool_protocol: Option<String>,
    #[serde(default)]
    pub display: String,
    #[serde(default)]
    pub api_key: String,
    #[serde(default)]
    pub base_url: String,
    #[serde(default)]
    pub model: String,
    #[serde(default)]
    pub fast_model: Option<String>,
    #[serde(default)]
    pub context_window: Option<u64>,
    #[serde(default)]
    pub effective_context_window_percent: Option<u8>,
    #[serde(default)]
    pub auto_compact_token_limit: Option<u64>,
    #[serde(default)]
    pub auto_compact_scope: coomi_engine::AutoCompactScope,
    #[serde(default)]
    pub comp_hash: Option<String>,
    #[serde(default)]
    pub max_output_tokens: Option<u64>,
    #[serde(default)]
    pub supports_remote_compaction: Option<bool>,
    #[serde(default)]
    pub remote_compaction_mode: RemoteCompactionMode,
    #[serde(default)]
    pub supports_vision: bool,
    #[serde(default = "default_true")]
    pub supports_native_tools: bool,
    #[serde(default)]
    pub supports_web_search: bool,
    #[serde(default)]
    pub supports_parallel_tool_calls: bool,
    #[serde(default)]
    pub supports_reasoning_effort: Option<bool>,
    #[serde(default, alias = "supports_prompt_reasoning")]
    pub reasoning_prompt_fallback: Option<bool>,
    #[serde(default)]
    pub reasoning_effort_map: BTreeMap<String, String>,
    #[serde(default, alias = "reasoning_models")]
    pub reasoning_profiles: BTreeMap<String, ReasoningProfileSettings>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

fn default_provider_type() -> String {
    "openai_compatible".into()
}

const fn default_true() -> bool {
    true
}

impl ProviderRegistry {
    pub fn load(path: &Path) -> Result<Self> {
        let raw = ProviderDocument::load(path)?;
        raw.validate()?;
        let mut providers = BTreeMap::new();
        for (id, provider) in raw.providers {
            if provider.model.trim().is_empty() {
                anyhow::bail!("provider `{id}` has no model");
            }
            if provider.base_url.trim().is_empty() {
                anyhow::bail!("provider `{id}` has no base_url");
            }
            let kind = ProviderKind::from_config(
                &provider.provider_type,
                provider.tool_protocol.as_deref(),
            )?;
            let display = if provider.display.trim().is_empty() {
                id.clone()
            } else {
                provider.display
            };
            providers.insert(
                id.clone(),
                ProviderConfig {
                    id,
                    kind,
                    display,
                    api_key: provider.api_key,
                    base_url: provider.base_url,
                    model: provider.model,
                    fast_model: provider.fast_model.filter(|value| !value.trim().is_empty()),
                    capabilities: coomi_engine::ModelCapabilities {
                        context_window: provider.context_window.unwrap_or(256_000),
                        effective_context_window_percent: provider
                            .effective_context_window_percent
                            .unwrap_or(95)
                            .clamp(1, 100),
                        auto_compact_token_limit: provider.auto_compact_token_limit,
                        auto_compact_scope: provider.auto_compact_scope,
                        comp_hash: provider.comp_hash,
                        max_output_tokens: provider.max_output_tokens.unwrap_or(8_192),
                        supports_remote_compaction: provider
                            .supports_remote_compaction
                            .unwrap_or(kind == ProviderKind::OpenAiResponses),
                        supports_vision: provider.supports_vision,
                        supports_native_tools: provider.supports_native_tools
                            && provider
                                .tool_protocol
                                .as_deref()
                                .is_none_or(|protocol| !protocol.eq_ignore_ascii_case("disabled")),
                        supports_web_search: provider.supports_web_search,
                        supports_parallel_tool_calls: provider.supports_parallel_tool_calls,
                    },
                    supports_reasoning_effort: provider.supports_reasoning_effort,
                    reasoning_prompt_fallback: provider.reasoning_prompt_fallback,
                    reasoning_effort_map: provider.reasoning_effort_map,
                    reasoning_profiles: provider.reasoning_profiles,
                    remote_compaction_mode: provider.remote_compaction_mode,
                },
            );
        }
        if providers.is_empty() {
            anyhow::bail!("provider file contains no providers")
        }
        let active = if raw.active.is_empty() {
            providers
                .keys()
                .next()
                .cloned()
                .context("provider file contains no providers")?
        } else {
            raw.active
        };
        if !providers.contains_key(&active) {
            anyhow::bail!("active provider `{active}` does not exist")
        }
        Ok(Self { active, providers })
    }

    pub fn active_id(&self) -> &str {
        &self.active
    }

    pub fn choices(&self) -> Vec<ModelChoice> {
        let mut choices = Vec::new();
        for provider in self.providers.values() {
            choices.push(ModelChoice {
                selector: provider.id.clone(),
                provider_id: provider.id.clone(),
                provider_display: provider.display.clone(),
                model: provider.model.clone(),
                is_fast: false,
            });
            if let Some(fast_model) = &provider.fast_model
                && fast_model != &provider.model
            {
                choices.push(ModelChoice {
                    selector: format!("{}:{fast_model}", provider.id),
                    provider_id: provider.id.clone(),
                    provider_display: provider.display.clone(),
                    model: fast_model.clone(),
                    is_fast: true,
                });
            }
        }
        choices
    }

    pub fn resolve(&self, selector: Option<&str>) -> Result<ProviderConfig> {
        let selector = selector.unwrap_or(&self.active).trim();
        if let Some(provider) = self.find_provider(selector) {
            return Ok(provider.clone());
        }

        let choices = self.choices();
        if let Some(choice) = choices
            .iter()
            .find(|choice| choice.selector.eq_ignore_ascii_case(selector))
        {
            let mut provider = self
                .providers
                .get(&choice.provider_id)
                .context("model choice references a missing provider")?
                .clone();
            provider.model = choice.model.clone();
            return Ok(provider);
        }

        let model_matches = choices
            .iter()
            .filter(|choice| choice.model.eq_ignore_ascii_case(selector))
            .collect::<Vec<_>>();
        let active_matches = model_matches
            .iter()
            .copied()
            .filter(|choice| choice.provider_id.eq_ignore_ascii_case(&self.active))
            .collect::<Vec<_>>();
        let choice = if active_matches.len() == 1 {
            active_matches.first().copied()
        } else if model_matches.len() == 1 {
            model_matches.first().copied()
        } else {
            None
        };
        if let Some(choice) = choice {
            let mut provider = self
                .providers
                .get(&choice.provider_id)
                .context("model choice references a missing provider")?
                .clone();
            provider.model = choice.model.clone();
            return Ok(provider);
        }
        if model_matches.len() > 1 {
            let candidates = model_matches
                .iter()
                .map(|choice| choice.selector.as_str())
                .collect::<Vec<_>>()
                .join(", ");
            anyhow::bail!(
                "model selector `{selector}` is ambiguous across providers; use one of: {candidates}"
            );
        }

        if let Some((provider_id, model)) = selector.split_once(':')
            && let Some(provider) = self.find_provider(provider_id)
        {
            let allowed = provider.model.eq_ignore_ascii_case(model)
                || provider
                    .fast_model
                    .as_deref()
                    .is_some_and(|candidate| candidate.eq_ignore_ascii_case(model));
            if allowed {
                let mut provider = provider.clone();
                provider.model = model.to_string();
                return Ok(provider);
            }
            anyhow::bail!(
                "model `{model}` is not declared for provider `{}`",
                provider.id
            )
        }

        anyhow::bail!("model selector `{selector}` is not present in providers.json")
    }

    fn find_provider(&self, id: &str) -> Option<&ProviderConfig> {
        self.providers.get(id).or_else(|| {
            self.providers
                .values()
                .find(|provider| provider.id.eq_ignore_ascii_case(id))
        })
    }
}

impl ProviderDocument {
    pub fn load(path: &Path) -> Result<Self> {
        let bytes = fs::read(path)
            .with_context(|| format!("failed to read provider file {}", path.display()))?;
        serde_json::from_slice(&bytes)
            .with_context(|| format!("invalid provider file {}", path.display()))
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        self.validate()?;
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(path, serde_json::to_vec_pretty(self)?)
            .with_context(|| format!("failed to save provider file {}", path.display()))
    }

    pub fn validate(&self) -> Result<()> {
        anyhow::ensure!(
            !self.providers.is_empty(),
            "at least one provider is required"
        );
        anyhow::ensure!(
            self.active.trim().is_empty() || self.providers.contains_key(&self.active),
            "active provider `{}` does not exist",
            self.active
        );
        for (id, provider) in &self.providers {
            anyhow::ensure!(!id.trim().is_empty(), "provider ID must not be empty");
            anyhow::ensure!(
                !provider.model.trim().is_empty(),
                "provider `{id}` has no model"
            );
            anyhow::ensure!(
                !provider.base_url.trim().is_empty(),
                "provider `{id}` has no base_url"
            );
            ProviderKind::from_config(&provider.provider_type, provider.tool_protocol.as_deref())?;
            for (model, profile) in &provider.reasoning_profiles {
                anyhow::ensure!(
                    !model.trim().is_empty(),
                    "provider `{id}` has an empty reasoning profile key"
                );
                if let Some(levels) = &profile.levels {
                    let mut seen = Vec::new();
                    for level in levels {
                        anyhow::ensure!(
                            *level != ReasoningEffort::Auto,
                            "provider `{id}` reasoning profile `{model}` must not declare auto"
                        );
                        anyhow::ensure!(
                            !seen.iter().any(|value: &&str| *value == level.as_str()),
                            "provider `{id}` reasoning profile `{model}` declares duplicate level `{}`",
                            level.as_str()
                        );
                        seen.push(level.as_str());
                    }
                }
                for (level, value) in &profile.effort_map {
                    anyhow::ensure!(
                        !value.trim().is_empty(),
                        "provider `{id}` reasoning profile `{model}` map `{level}` must not be empty"
                    );
                }
                if profile.supported == Some(false) {
                    anyhow::ensure!(
                        profile.levels.as_ref().is_none_or(Vec::is_empty),
                        "provider `{id}` reasoning profile `{model}` cannot declare levels when supported is false"
                    );
                }
            }
            for (level, value) in &provider.reasoning_effort_map {
                anyhow::ensure!(
                    !value.trim().is_empty(),
                    "provider `{id}` reasoning_effort_map `{level}` must not be empty"
                );
            }
        }
        Ok(())
    }
}

impl Default for ProviderSettings {
    fn default() -> Self {
        Self {
            provider_type: default_provider_type(),
            tool_protocol: Some("auto".into()),
            display: String::new(),
            api_key: String::new(),
            base_url: String::new(),
            model: String::new(),
            fast_model: None,
            context_window: None,
            effective_context_window_percent: None,
            auto_compact_token_limit: None,
            auto_compact_scope: coomi_engine::AutoCompactScope::Total,
            comp_hash: None,
            max_output_tokens: None,
            supports_remote_compaction: None,
            remote_compaction_mode: RemoteCompactionMode::default(),
            supports_vision: false,
            supports_native_tools: true,
            supports_web_search: false,
            supports_parallel_tool_calls: false,
            supports_reasoning_effort: None,
            reasoning_prompt_fallback: None,
            reasoning_effort_map: BTreeMap::new(),
            reasoning_profiles: BTreeMap::new(),
            extra: BTreeMap::new(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn choices_only_include_models_declared_by_providers() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let path = directory.path().join("providers.json");
        fs::write(
            &path,
            r#"{
                "active": "primary",
                "providers": {
                    "primary": {
                        "type": "generic",
                        "display": "Primary",
                        "api_key": "secret",
                        "base_url": "https://example.test/v1",
                        "model": "main-model",
                        "fast_model": "fast-model"
                    }
                }
            }"#,
        )
        .expect("write provider fixture");
        let registry = ProviderRegistry::load(&path).expect("provider registry");
        assert_eq!(registry.choices().len(), 2);
        assert_eq!(
            registry
                .resolve(Some("primary:fast-model"))
                .expect("fast model")
                .model,
            "fast-model"
        );
        assert!(registry.resolve(Some("invented-model")).is_err());
        assert_eq!(
            registry
                .resolve(Some("primary"))
                .expect("primary")
                .capabilities
                .effective_context_window(),
            243_200
        );
    }

    #[test]
    fn tool_protocol_does_not_override_api_kind_and_disabled_tools_are_honored() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let path = directory.path().join("providers.json");
        fs::write(
            &path,
            r#"{
                "active": "primary",
                "providers": {
                    "primary": {
                        "type": "anthropic_messages",
                        "tool_protocol": "disabled",
                        "base_url": "https://example.test",
                        "model": "claude"
                    }
                }
            }"#,
        )
        .expect("write provider fixture");
        let registry = ProviderRegistry::load(&path).expect("provider registry");
        let provider = registry.resolve(None).expect("active provider");
        assert_eq!(provider.kind, ProviderKind::AnthropicMessages);
        assert!(!provider.capabilities.supports_native_tools);
    }

    #[test]
    fn bare_model_selector_prefers_active_provider_and_rejects_remaining_ambiguity() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let path = directory.path().join("providers.json");
        fs::write(
            &path,
            r#"{
                "active": "primary",
                "providers": {
                    "primary": {
                        "type": "generic",
                        "base_url": "https://primary.example.test/v1",
                        "model": "shared-model"
                    },
                    "secondary": {
                        "type": "generic",
                        "base_url": "https://secondary.example.test/v1",
                        "model": "shared-model"
                    },
                    "third": {
                        "type": "generic",
                        "base_url": "https://third.example.test/v1",
                        "model": "other-model",
                        "fast_model": "ambiguous-fast"
                    },
                    "fourth": {
                        "type": "generic",
                        "base_url": "https://fourth.example.test/v1",
                        "model": "ambiguous-fast"
                    }
                }
            }"#,
        )
        .expect("write provider fixture");
        let registry = ProviderRegistry::load(&path).expect("provider registry");

        assert_eq!(
            registry
                .resolve(Some("shared-model"))
                .expect("active provider wins")
                .id,
            "primary"
        );
        let error = registry
            .resolve(Some("ambiguous-fast"))
            .expect_err("non-active duplicate model must be explicit");
        assert!(error.to_string().contains("ambiguous across providers"));
        assert_eq!(
            registry
                .resolve(Some("third:ambiguous-fast"))
                .expect("explicit selector")
                .id,
            "third"
        );
    }
}
