"""Third-party integrations.

Each module exposes a thin, async-friendly wrapper around a single provider.
Heavy provider SDKs (Google APIs, etc.) are imported lazily inside functions
so that unrelated parts of the agent don't pay the import cost.
"""
