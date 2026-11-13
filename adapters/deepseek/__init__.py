"""DeepSeek Harness (dsh) adapter (Phase 2.5).

Runs ``dsh --profile headless --json`` inside ``heg-sandbox-deepseek``
images. ``deepseek-ai/deepseek-harness`` (npm ``@deepseek-ai/dsh``) is
DeepSeek's plugin-composed open agent harness; the headless profile runs one
task and exits. Its ``dsh-llm-deepseek`` route honours ``DEEPSEEK_API_KEY`` /
``DEEPSEEK_BASE_URL`` — pointed at the gateway's Anthropic-compatible
``/v1/messages``. See docs/p25-harness-adapters.md (known gaps section).
"""
