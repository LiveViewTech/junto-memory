# Contributing

## Branches

`main` is the released line. Other installs pull it: the maintainer's work deployment and anyone running junto-memory. Keep it deployable.

- **Do new work on a branch:** `feat/<topic>` for features, `fix/<topic>` for fixes, `docs/<topic>` for documentation.
- **Push the branch early and often.** Pushing is a safety net, not a release.
- **Merge to `main` only when the work is proven:** tests and lint pass, and the change has run clean on a live deployment. For the maintainers, that deployment is the home box.
- **Merge through a pull request.** `main` is protected: it needs a pull request and green CI (lint, test, docker).
- **Exception:** a small, urgent fix to something broken in production may be pushed straight to `main` by a maintainer, noted afterwards.

## Before you push

```bash
ruff check src/
PYTHONPATH=src python -m pytest tests -q
```

CI runs the same lint and tests, plus a Docker build.

## Wire shapes

Other projects depend on the exact shapes junto-memory returns, so treat them as fixed:
- Don't rename fields or change casing in tool responses.
- Before changing a tool signature or wire protocol, document the existing one first.
- Spec changes that cross components go through the spec owner.

## License

MIT. By contributing, you agree your contributions are licensed under the same terms.
