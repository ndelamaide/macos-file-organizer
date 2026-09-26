# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "ocrmac",
#     "ollama",
#     "pydantic",
#     "pypdf",
#     "python-docx",
# ]
# ///
"""
Local AI file organizer (Ollama). Nothing leaves your machine.

Usage:
  uv run organize.py plan  ~/Downloads      # writes plan.csv, moves nothing
  # -> open plan.csv, fix/delete rows you don't like
  uv run organize.py apply ~/Downloads      # moves files per plan.csv, logs undo.csv

Settings are read from config.toml next to this script
(copy config.example.toml to get started).
"""
import csv, re, shutil, sys, tomllib
from pathlib import Path
from typing import Literal

import ollama
from pydantic import BaseModel, create_model

CONFIG_PATH = Path(__file__).with_name("config.toml")
SKIP = {".DS_Store"}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"No config found at {CONFIG_PATH}\n"
            "Create it with: cp config.example.toml config.toml"
        )
    with open(CONFIG_PATH, "rb") as fh:
        cfg = tomllib.load(fh)
    cfg.setdefault("model", "qwen3.5:9b")
    cfg.setdefault("max_chars", 3000)
    cfg.setdefault("ocr_languages", ["fr-FR", "en-US"])
    if not cfg.get("categories"):
        sys.exit("config.toml: 'categories' must be a non-empty list")
    if cfg["model"].endswith("-cloud") or ":cloud" in cfg["model"]:
        sys.exit("config.toml: cloud models would send your files off-device; use a local model")
    return cfg


CFG = load_config()

Decision = create_model(
    "Decision",
    category=(Literal[tuple(CFG["categories"])], ...),
    new_name=(str, ...),
    confidence=(float, ...),
)


def extract_text(p: Path) -> str:
    ext = p.suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            pages = PdfReader(p).pages[:3]
            return "\n".join(pg.extract_text() or "" for pg in pages)
        if ext in {".png", ".jpg", ".jpeg", ".heic", ".tiff"}:
            from ocrmac import ocrmac  # Apple Vision OCR, on-device
            res = ocrmac.OCR(str(p), language_preference=CFG["ocr_languages"]).recognize()
            return " ".join(t for t, _, _ in res)
        if ext == ".docx":
            import docx
            return "\n".join(par.text for par in docx.Document(p).paragraphs)
        if ext in {".txt", ".md", ".csv", ".json"}:
            return p.read_text(errors="ignore")
    except Exception as e:
        print(f"  ! could not read {p.name}: {e}")
    return ""  # .gdoc/.gsheet stubs, audio, etc. -> classified by filename only


def classify(p: Path) -> BaseModel:
    text = extract_text(p)[: CFG["max_chars"]]
    prompt = (
        f"Classify this file into exactly one category: {', '.join(CFG['categories'])}.\n"
        "Also propose a short new filename (no extension), lowercase, words joined "
        "by hyphens, prefixed with YYYY-MM-DD if a document date is clear. "
        "Give confidence 0-1.\n\n"
        f"Filename: {p.name}\nContent:\n{text or '(no readable content)'}"
    )
    r = ollama.chat(
        model=CFG["model"],
        messages=[{"role": "user", "content": prompt}],
        format=Decision.model_json_schema(),
        options={"temperature": 0},
    )
    return Decision.model_validate_json(r.message.content)


def safe_name(s: str) -> str:
    return re.sub(r"[^a-z0-9\-_]", "", s.lower().replace(" ", "-"))[:80] or "file"


def plan(root: Path):
    files = [f for f in root.rglob("*")
             if f.is_file() and f.name not in SKIP and not f.name.startswith(".")]
    with open("plan.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["src", "dest", "category", "confidence"])
        for i, f in enumerate(files, 1):
            print(f"[{i}/{len(files)}] {f.name}")
            d = classify(f)
            dest = root / d.category / (safe_name(d.new_name) + f.suffix.lower())
            w.writerow([f, dest, d.category, round(d.confidence, 2)])
    print("Wrote plan.csv. Review it, then run: apply")


def apply(root: Path):
    with open("plan.csv") as fh, open("undo.csv", "a", newline="") as undo:
        u = csv.writer(undo)
        for row in csv.DictReader(fh):
            src, dest = Path(row["src"]), Path(row["dest"])
            if not src.exists() or src == dest:
                continue
            n = 1
            while dest.exists():  # never overwrite
                dest = dest.with_stem(f"{dest.stem}-{n}"); n += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, dest)
            u.writerow([dest, src])
            print(f"{src.name} -> {dest.relative_to(root)}")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in {"plan", "apply"}:
        sys.exit(__doc__)
    cmd, folder = sys.argv[1], Path(sys.argv[2]).expanduser()
    {"plan": plan, "apply": apply}[cmd](folder)