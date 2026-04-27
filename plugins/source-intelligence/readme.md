# source-intelligence

Global Hermes plugin for source intake, transcript analysis, signal extraction,
and domain-specific insight projection.

The first migrated connector is the old YouTube trading research pipeline. Its
runtime state now lives under:

```text
~/.hermes/data/source-intelligence/youtube/
```

The plugin boundary is intentionally broader than trading:

```text
source -> normalized source document -> generic insights -> domain adapter
```

Trading consumes the projected `strategy_feedback.json`, but YouTube and source
processing are global capabilities.

