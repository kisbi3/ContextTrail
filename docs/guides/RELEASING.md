# Releasing

A release is a version bump, a tag, and the `publish` workflow. Nothing is uploaded from a laptop and no PyPI token is stored anywhere: the workflow uses PyPI Trusted Publishing (OIDC).

## One-time setup on pypi.org (project owner)

1. Sign in at https://pypi.org (create the account if needed; enable 2FA, PyPI requires it for new projects).
2. Before the first upload the project does not exist yet, so register a **pending publisher**: https://pypi.org/manage/account/publishing/ → "Add a new pending publisher" with
   - PyPI project name: `contexttrail`
   - Owner: `kisbi3`
   - Repository name: `ContextTrail`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
3. On GitHub, create the environment `pypi` under Settings → Environments (an empty environment is enough; a required reviewer may be added so every publish needs a click).

After the first successful publish the pending publisher becomes the project's trusted publisher automatically.

## Each release

1. Bump `version` in `pyproject.toml` and `__version__` in `src/contexttrail/__init__.py` (the workflow refuses a tag that does not match).
2. Move the `Unreleased` entries in `CHANGELOG.md` under the new version with the date.
3. Commit, push, wait for the `test` workflow to pass.
4. Tag and push the tag:
   ```bash
   git tag -a v0.1.0a5 -m "ContextTrail 0.1.0a5"
   git push origin v0.1.0a5
   ```
5. The `publish` workflow builds the sdist and wheel, checks them, and uploads to PyPI.
6. Create the GitHub release from the tag with the changelog section as notes:
   ```bash
   gh release create v0.1.0a5 --title "ContextTrail 0.1.0a5" --notes-file <(sed -n '/^## 0.1.0a5/,/^## /p' CHANGELOG.md | sed '$d')
   ```

## Checking a build locally

```bash
python -m pip install build twine
python -m build && python -m twine check dist/*
python -m venv /tmp/ct-wheel && /tmp/ct-wheel/bin/pip install dist/contexttrail-*.whl && /tmp/ct-wheel/bin/ct --version
```

The wheel must contain `contexttrail/prompts/*.md` and `contexttrail/assets/*`; the demo (`ct demo --path /tmp/ct-demo --no-tui`) fails without them.
