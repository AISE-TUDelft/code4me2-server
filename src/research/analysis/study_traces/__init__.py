"""Owner-only chat traces: what the agent was asked, thought, said and did.

Kept apart from :mod:`research.analysis.study_analytics` on purpose: analytics
responses never carry content, while a trace returns the stored text (prompts,
reasoning, messages, tool arguments and results) exactly as the study's
telemetry policy and the participant's consent kept it, and ``[REDACTED]``
markers where they did not.
"""
