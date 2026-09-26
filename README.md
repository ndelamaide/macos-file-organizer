# Local AI File Organizer

Sort and rename messy folders (Downloads, Desktop, a mirrored Google Drive…) with a **local** LLM.
Nothing leaves your machine: text extraction, OCR and classification all run on-device.

It works in two steps, so nothing moves without your review:

1. **plan**: reads each file, asks the model for a category and a clean filename, and writes `plan.csv`
2. **apply**: moves the files according to `plan.csv` (after you've reviewed and edited it)

## Requirements

- macOS on Apple Silicon (OCR uses Apple's Vision framework)
- [Homebrew](https://brew.sh)
- ~16 GB RAM or more recommended (tested target: M4, 24 GB)

## Install

```bash
# 1. Tools
brew install uv ollama

# 2. Start Ollama (leave this terminal open)
ollama serve

# 3. In another terminal, download the model (~6 GB)
ollama pull qwen3.5:9b

# 4. Declare the script's dependencies (one-time; uv manages the environment)
uv add --script organize.py ollama pydantic pypdf python-docx ocrmac
```

There's no virtualenv to create or activate: `uv run` builds an isolated, cached environment from the dependencies declared at the top of `organize.py`.

## Configure

Edit the config block at the top of `organize.py`:

- `CATEGORIES`: the folders you want. The model can **only** choose from this list.
- `MODEL`: any local Ollama model. Use a bigger one (e.g. `mistral-small3.2:24b`) if results are sloppy. Never use a `-cloud` tag, or your data leaves the machine.
- `MAX_CHARS`: how much of each file's text is sent to the model.

## Run

> ⚠️ Make a backup first (e.g. Time Machine). Try it on a **copy** of a folder before pointing it at real data.

```bash
# 1. Generate the plan (moves nothing)
uv run organize.py plan ~/Downloads

# 2. Review plan.csv: edit destinations, delete rows you don't want moved

# 3. Apply the plan
uv run organize.py apply ~/Downloads
```

Files are moved into `<folder>/<Category>/<new-name>.<ext>`. Existing files are never overwritten (a `-1`, `-2`… suffix is added).
Every move is appended to `undo.csv` (`new path, original path`) so you can revert by hand.

## Supported files

| Type | How it's read |
|---|---|
| PDF | Text of the first 3 pages (`pypdf`) |
| Images (png, jpg, heic, tiff) | On-device OCR, French + English (`ocrmac`) |
| Word (.docx) | Paragraph text (`python-docx`) |
| txt, md, csv, json | Raw text |
| Anything else | Classified from the filename only |

Scanned PDFs without a text layer are currently classified by filename only.

## Google Drive

Install Google Drive for desktop and set **Settings → Preferences → Folders from Drive → Mirror files**, so every file exists locally. Then run the organizer on the mirrored `My Drive` folder; moves sync back to Drive.

- Let sync finish before switching modes.
- Shared drives can't be mirrored.
- Native Google Docs/Sheets appear as `.gdoc`/`.gsheet` link files with no content, so they're sorted by filename only.

## Privacy

- The model runs through Ollama on `localhost`. No API keys, no cloud calls.
- `plan.csv` and `undo.csv` contain your file paths and names. Keep them out of git (see `.gitignore`).