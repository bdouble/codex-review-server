---
description: List Codex models available on your account, with valid reasoning efforts
allowed-tools: mcp__plugin_codex-delegate_codex-delegate__codex_models
---

# Codex Models

Call `codex_models()` and present the catalog.

It is read live from the Codex CLI, so it reflects what your account can
actually use today — including models released after this plugin was written,
and excluding any that OpenAI has since retired.

Present as a table: model, what it's for (`description`), efforts, default
effort. Note the configured default (`configured_default`) and where the
catalog came from (`source: live` means it came from the CLI; `fallback` means
the CLI could not be queried and these are this plugin's last-known-good
values).

Worth calling out to the user when relevant:

- **Effort support is per-model.** The Luna models have no `ultra`; `gpt-5.5`
  tops out at `xhigh`. Asking for an unsupported effort is
  rejected up front rather than failing mid-run.
- **`ultra` coordinates several agents in parallel** — substantially slower and
  costlier, worth it only for genuinely hard problems.
- **The catalog is per-account.** Access-gated models such as
  `gpt-daybreak-blue-latest`, the defensive-security model, appear only where
  they have been approved. A model missing from the list is not a bug. When
  one is listed, it is also the default for any request that names no model.
- **Use the full slug.** The bare `gpt-5.6` alias does not resolve under
  ChatGPT-account auth.
