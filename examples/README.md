# Examples

Real benchmark reports produced by modelbench. Numbers are **point-in-time snapshots** — the *method* is reusable, the *figures* date quickly as providers update models.

- **[report_2026-08-deepseek-glm-kimi-minimax](report_2026-08-deepseek-glm-kimi-minimax/README.md)** — 15 endpoint/model combos (DeepSeek / Kimi / MiniMax / GLM / Doubao / GPT-5.6 via a gateway), 6 workloads, ~900 samples. Includes the reasoning-effort sweep and an official-direct-vs-hosted comparison.

To reproduce against your own endpoints, edit `config.yaml` and run `uv run python -m modelbench.cli run --report`.
