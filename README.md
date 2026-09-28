# Local AI File Organizer

Sort and rename messy files from your laptop and Google Drive into one clean folder tree, using a **local** LLM.
Nothing leaves your machine: text extraction, OCR and classification all run on-device.

It works in two steps, so nothing moves without your review:

1. **plan**: reads each file, asks the model for a category and a clean filename, and writes a plan to `runs/`
2. **apply**: moves the files according to that plan, after you've reviewed and edited it

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
- Grouping, per category:
  - `group_by = "employer"` makes one folder per employer (or bank, insurer…), named from the document. Existing folders are reused, and names are normalized so `Acme SA` and `ACME` land in the same folder. Rename a group folder by hand and future files follow.
  - `subfolders` makes fixed document-type folders inside each group, e.g. `Contracts`, `Payslips`.
- File names and dates, at the top level, per category or per subfolder:
  - `filename`: a template with `{date}`, `{title}`, `{group}`, `{subfolder}`. Default `{date}-{title}`.
  - `date`: **which** date matters for that kind of document, in plain words. It's read from the content, never from the file's creation date, and left out of the name if the document doesn't show it.
  - `date_precision`: `year`, `month` or `day`.
  - `title_language`: language for `{title}` (default: the document's own language).

  ```toml
  [categories."Work/Employment"]
  description = "Employment contracts, payslips, work certificates"
  group_by = "employer"

  [categories."Work/Employment".subfolders.Payslips]
  description = "Monthly payslip (fiche de paie, bulletin de salaire)"
  date = "the pay period: the month the salary is for, not the payment or print date"
  date_precision = "month"
  filename = "{date}-payslip"
  # -> Work/Employment/Acme/Payslips/2024-03-payslip.pdf
  ```

  `config.example.toml` has date rules for taxes (tax year), bank statements (statement period), travel (travel date), insurance and more.
- `min_confidence` / `inbox`: files the model is unsure about go to the inbox folder instead of being forced into a category.
- `exclude`: folders never scanned. A bare name (`"node_modules"`) skips every folder with that name; a path starting with `/` or `~` (`"~/Documents/Archive"`) skips that exact folder and everything inside it.
- `model`, `max_chars`, `ocr_languages`: model and text-extraction settings. Cloud models are refused.

## Run

> ⚠️ Make a backup first (e.g. Time Machine). Try it on a **copy** of a folder before pointing it at real data.

```bash
# 1. Plan: scan one or more folders (moves nothing)
uv run organize.py plan ~/Downloads ~/Desktop "~/My Drive"
#    -> writes runs/plan-20260926-165700.csv

# 2. Review the plan (see below)

# 3. Apply the latest plan...
uv run organize.py apply
#    ...or a specific one
uv run organize.py apply runs/plan-20260926-165700.csv
```

Use `--into <folder>` to override the destination for one run, e.g. to test on a scratch folder.

Use `--refresh` to re-date and rename files you've already sorted, e.g. after changing date rules or filename templates:

```bash
uv run organize.py plan "~/My Drive" --refresh
```

Sorted files keep their category (the folder they're in); only their group, subfolder, date and name are recomputed. Files in the Inbox are re-classified from scratch. Files that are already right, including numbered copies such as `name-2.pdf` next to `name.pdf`, are left out of the plan.

Every run gets its own timestamped file in `runs/` (next to the script), so nothing is ever overwritten: `plan-<timestamp>.csv` for each plan and `undo-<timestamp>.csv` for each apply that moved something.

Each file takes two model calls: one for the category, then one for the details (date, title, group, subfolder) with the rules for that category.
The model sees the file's current name and folder as well as its content. A name that already says what the document is (`Rapport de stage Nestlé.pdf`) is reused for `{title}` instead of being rewritten; unclear names (`scan_0034.pdf`) get a new title.

### Reviewing a plan

| Column | Meaning |
|---|---|
| `action` | `move`, `keep`, `duplicate` or `skip`. Only `move` rows are executed; change a row to `skip` to leave a file alone, or a `keep` row to `move` to sort it anyway. |
| `src` / `dest` | Current path and proposed new path. Edit `dest` freely. For `keep` rows, `dest` is where the file would go in its category. |
| `category` | Where the file is going (`Inbox` if confidence was low). |
| `group` / `subfolder` | The employer, bank… and document type, for grouped categories. |
| `date` | The document date used in the name. Empty means none was found: check these. |
| `suggested` | The model's first choice, handy for accepting Inbox files. |
| `confidence` | The model's confidence, 0 to 1. |
| `note` | Why a row was skipped, or which file a duplicate matches. |

Existing files are never overwritten: a `-1`, `-2`… suffix is added.
Each apply writes its moves to its own `runs/undo-<timestamp>.csv` (`new_path, original_path`), so you can revert any run by hand. Applying the same plan twice is safe: rows whose source is already gone are skipped.

## What gets scanned

- **Skipped entirely:** folders containing a `.git` repo (code belongs on GitHub, not in this tree), hidden files and folders, app bundles, names listed in `exclude`, and category folders that are already sorted. Running it again only picks up new files (unless you pass `--refresh`).
- **Regrouping:** the one exception is grouped categories. If you add `group_by` or `subfolders` to a category after sorting into it, the next `plan` also picks up the files sitting directly in that category folder (e.g. `Work/Employment/payslip.pdf`) and moves them into `<group>/<subfolder>/`, with a new name. Their category is kept. This only happens when the category folder is inside one of the folders you scan, and they're marked `already sorted` in the plan.
- **Left in place:** files in a folder you organized yourself inside `destination` (e.g. `My Drive/EPFL/Master/Machine Learning/`) stay there when the model judges that folder a meaningful home for them. They're marked `keep` in the plan. Generic folders (`Documents`, `Scans`, `Misc`…) shouldn't count, the top of `destination` never does, and files outside `destination` (Downloads, Desktop…) are always sorted. Kept files are checked again on every run; add their folder to `exclude` to skip them for good.
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
- `config.toml` and everything in `runs/` contain your categories, file paths and names. They're all gitignored.
