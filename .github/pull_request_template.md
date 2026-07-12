## Summary

<!-- What changed and why. -->

## Checklist

- [ ] `make check` passes (ruff check + format + pytest — the full CI gate)
- [ ] `docs/ARCHITECTURE.md` still matches reality (new module / CLI entry
      point / state-pipeline shape / on-disk path / `projects.toml` key /
      supported-feature change → update it in this PR)
- [ ] `BACKLOG.md` updated if this implements or obsoletes an item
- [ ] If installed behavior changed: verified against a scratch tmux server
      after `make reinstall` (see `.claude/skills/verify`)
