# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python CLI (`organize.py`) that sorts and renames personal files into a folder tree using a **local** LLM via Ollama. Everything runs on-device (Apple Vision OCR, Ollama on localhost); keeping it that way is a core requirement, which is why `load_config` refuses model names containing `cloud`.

## Running

Dependencies are declared inline (PEP 723 header at the top of `organize.py`). There is no virtualenv, requirements file, test suite or linter config: `uv run` builds a cached env on first use.

```bash
ollama serve                      # must be running; default model: ollama pull qwen3.5:9b
cp config.example.toml config.toml
uv run organize.py plan <folders...> [--into <dest>] [--refresh]   # writes runs/plan-<ts>.csv, moves nothing
uv run organize.py apply [runs/plan-<ts>.csv]                      # default: latest plan in runs/
```

To check a change end-to-end, run `plan` against a scratch copy of some files with `--into <scratch dir>` so nothing real is touched, inspect the CSV, then `apply` it. Needs macOS on Apple Silicon (`ocrmac`).

`config.toml`, `runs/`, and `plan*.csv` / `undo*.csv` hold the user's real paths and are gitignored; don't commit them.

## Architecture

**Two phases, joined by a CSV.** `plan` does all the reading and model calls and writes a CSV (`PLAN_FIELDS`); the user edits it by hand; `apply` only reads `action`/`src`/`dest` and moves files. `apply` never loads the config or calls the model. Any new per-file information must go through the plan CSV.

**Config normalization (`load_config`).** The TOML allows shorthand: a category can be a string or a table, `subfolders` can be a list, a table of strings, or a table of tables. Everything is normalized into one dict shape up front; the rest of the code assumes that shape. The inbox category is always added. Naming settings (`date`, `date_precision`, `filename`) are layered subfolder → category → top level, looked up with `resolve()`. Config errors `sys.exit` with a message naming where in the file the problem is.

**Two model calls per file**, both with `temperature=0`, `think=False`, and structured output from a Pydantic model built at runtime with `create_model`:
1. `classify`: category constrained to a `Literal` of the configured category names, plus confidence. Below `min_confidence` → inbox (the model's pick is kept in `suggested`).
2. `describe`: fields depend on the chosen category (`group` only if `group_by`, `subfolder` as a `Literal` only if `subfolders`, always `date` and `title`). The date rule shown to the model is built from the per-category/per-subfolder `date` descriptions.

Model output is then post-processed rather than trusted: `normalize_date` drops anything unparsable or implausible ("no date is better than a wrong one"), `clean_group` strips legal suffixes, `match_group` folds names onto existing group folders (accent/case-insensitive, prefix, then fuzzy match), and `safe_name` produces ASCII kebab-case filenames. Known groups come from folders on disk (`existing_groups`) plus names seen earlier in the same run, and are fed back into the `describe` prompt.

**Scanning** has two sources feeding one item list in `plan`:
- `iter_files`: new files. Prunes hidden items, dirs containing `.git`, `exclude` names/paths, app bundles, and every category folder (`sorted_dirs`), so re-runs only see new files.
- `iter_sorted`: files already inside category folders that need another pass, i.e. files in a grouped category that aren't at `<group>/<subfolder>` depth yet, or every sorted file with `--refresh`. These keep their category (except Inbox files, which are re-classified) and never descend deeper than the category's own structure, since deeper folders belong to the user.

`DuplicateFinder` indexes everything already sorted, then checks new files by size first and SHA-256 only on size collisions.

## Invariants to preserve

- Nothing is ever deleted or overwritten: `apply` appends `-1`, `-2`… on collisions, and duplicates are only reported, never moved.
- `apply` is idempotent (rows whose `src` is gone are skipped) and writes `runs/undo-<ts>.csv`, flushed after each move. The undo file is created only once something actually moves.
- Run files are never overwritten (`new_run_file` adds a suffix). `latest_plan` sorts on the `plan-DATE-TIME[-N]` name format, so keep it if you change naming.
- Google Drive stubs (`GOOGLE_STUBS`) keep their original name and are skipped if their category has a `destination` outside Drive.
- Heavy extraction libraries are imported lazily inside `extract_text`/`ocr`. Extraction failures return `""`, so the file is still classified from its filename.

The README documents every config key and the plan CSV columns for users, and `config.example.toml` is the reference config. Update both when changing config options or plan columns.
