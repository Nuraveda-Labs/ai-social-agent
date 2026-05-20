# Contributing to AI social Agent

Thanks for thinking about contributing. This agent is one of six in the open-source [Mesh Pilot](https://meshpilot.app) marketing stack, and we welcome PRs that fit its scope: multi-platform social posting and ORM with a human-in-the-loop approval gate before publish.

## Ways to contribute

- **Bug reports** — open an issue on either mirror (GitLab is primary; Codeberg syncs nightly). Include the integration involved (YouTube / Instagram / X / LinkedIn / video providers), the action that failed, and any redacted log output.
- **New integrations** — adding a new provider? Match the existing adapter pattern. Anything that creates an external side effect must surface a proposal through the human-in-the-loop gate before it executes.
- **Pipeline improvements** — keep the proposal → approval → execute → audit chain intact. The HITL gate is the moat.
- **Documentation** — README clarifications, runnable examples, integration recipes.

## Before you open a PR

1. **Open an issue first** for anything beyond a one-line fix.
2. **Preserve the HITL gate.** Any new action must route through an approval surface (Discord by default; web inbox in the Mesh Pilot cockpit). No PRs that bypass it.
3. **Add tests** for new actions and state-machine nodes.
4. **Format + lint** before committing.
5. **One concern per PR.**

## Development setup

```bash
git clone https://gitlab.com/mesh-pilot/ai-social-agent.git
cd ai-social-agent
# follow the README quick-start
```

## Commit style

Conventional commits. Examples: `feat(...)`, `fix(...)`, `docs(...)`.

## License

By contributing you agree your contributions are licensed under [MIT](LICENSE).

## Questions

Open an issue. For private inquiries: `support@meshpilot.app`.
