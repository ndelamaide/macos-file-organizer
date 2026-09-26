# Local AI File Organizer

Sort and rename messy files from your laptop and Google Drive into one clean folder tree, using a **local** LLM.
Nothing leaves your machine: text extraction, OCR and classification all run on-device.

It works in two steps, so nothing moves without your review:

1. **plan**: reads each file, asks the model for a category and a clean filename, and writes `plan.csv`
2. **apply**: moves the files according to `plan.csv`, after you've reviewed and edited it

## Requirements

- macOS on Apple Silicon (OCR uses Apple's Vision framework)
- [Homebrew](https://brew.sh)
- 16 GB RAM or more recommended (developed on an M4 with 24 GB)

## Install

```bash
# 1. Tools
brew install uv ollama

# 2. Start Ollama (leave this terminal open)
ollama serve

# 3. In another terminal, download the model (~6 GB)
ollama pull qwen3.5:9b
```

There's no virtualenv to create or activate: the dependencies are declared at the top of `organize.py`, and `uv run` builds an isolated, cached environment from them on first run.

## Configure

```bash
cp config.example.toml config.toml
```

Then edit `config.toml` (it's gitignored, so your settings never get committed):

- `destination`: root folder for sorted files, typically your mirrored Google Drive folder. Each category becomes a subfolder.
- `[categories]`: `"Folder/Subfolder" = "when to use it"`. The model can **only** pick from these, and it relies on the descriptions, so make them specific.
  Give a category its own `destination` to keep it elsewhere, e.g. identity documents in a local-only folder.
- `min_confidence` / `inbox`: files the model is unsure about go to the inbox folder instead of being forced into a category.
- `exclude`: folders never scanned. A bare name (`"node_modules"`) skips every folder with that name; a path starting with `/` or `~` (`"~/Documents/Archive"`) skips that exact folder and everything inside it.
- `model`, `max_chars`, `ocr_languages`: model and text-extraction settings. Cloud models are refused.

## Run

> ⚠️ Make a backup first (e.g. Time Machine). Try it on a **copy** of a folder before pointing it at real data.

```bash
# 1. Plan: scan one or more folders (moves nothing)
uv run organize.py plan ~/Downloads ~/Desktop "~/My Drive"

# 2. Review plan.csv (see below)

# 3. Apply
uv run organize.py apply
```

Use `--into <folder>` to override the destination for one run, e.g. to test on a scratch folder.

### Reviewing plan.csv

| Column | Meaning |
|---|---|
| `action` | `move`, `duplicate` or `skip`. Only `move` rows are executed; change a row to `skip` to leave a file alone. |
| `src` / `dest` | Current path and proposed new path. Edit `dest` freely. |
| `category` | Where the file is going (`Inbox` if confidence was low). |
| `suggested` | The model's first choice, handy for accepting Inbox files. |
| `confidence` | The model's confidence, 0 to 1. |
| `note` | Why a row was skipped, or which file a duplicate matches. |

Existing files are never overwritten: a `-1`, `-2`… suffix is added.
Every move is appended to `undo.csv` (`new path, original path`) so you can revert by hand.

## What gets scanned

- **Skipped entirely:** folders containing a `.git` repo (code belongs on GitHub, not in this tree), hidden files and folders, app bundles, names listed in `exclude`, and category folders that are already sorted. Running it again only picks up new files.
- **Duplicates:** files with identical content (SHA-256) are marked `duplicate` and never moved or deleted. Deal with them yourself.

| Type | How it's read |
|---|---|
| PDF | Text of the first 3 pages; scanned PDFs are OCR'd from the first page |
| Images (png, jpg, heic, tiff) | On-device OCR, French + English |
| Word (.docx) | Paragraph text |
| txt, md, csv, json | Raw text |
| Anything else | Classified from the filename only |

## Google Drive

Install Google Drive for desktop and set **Settings → Preferences → Folders from Drive → Mirror files**, so every file exists locally. Point `destination` at the mirrored `My Drive` folder. Moves sync back to Drive, and local files moved into it get uploaded.

- Let sync finish before switching modes, and expect a large first run to take a while to upload.
- Shared drives can't be mirrored.
- Native Google Docs/Sheets appear as `.gdoc`/`.gsheet` link files with no content. They're sorted by filename only, keep their names, and are never moved out of Drive.
- Google Photos is not part of Drive. Export it with Google Takeout if you want to sort photos of documents.

## Privacy

- The model runs through Ollama on `localhost`. No API keys, no cloud calls.
- `config.toml`, `plan.csv` and `undo.csv` contain your categories, file paths and names. They're all gitignored.
