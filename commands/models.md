---
description: List Codex models available on your account, with valid reasoning efforts
allowed-tools: mcp__plugin_codex-delegate_codex-delegate__codex_models
---

# Codex Models

Call `codex_models()` and present the catalog.

It is read live from the Codex CLI, so it reflects what your account can
actually use today — including models released after this plugin was written,
and excluding any that OpenAI has since retired.

Present as a table: model, efforts, default effort. Note the configured default
(`configured_default`) and where the catalog came from (`source: live` means it
came from the CLI; `fallback` means the CLI could not be queried and these are
this plugin's last-known-good values).

Worth calling out to the user when relevant:

- **Effort support is per-model.** `gpt-5.6-luna` has no `ultra`; the 5.4/5.5
  family tops out at `xhigh`. Asking for an unsupported effort is rejected up
  front rather than failing mid-run.
- **`max` and `ultra` are GPT-5.6 only.** `ultra` coordinates four agents in
  parallel — substantially slower and costlier, worth it only for genuinely
  hard problems.
- **Use the full slug.** The bare `gpt-5.6` alias does not resolve under
  ChatGPT-account auth.
