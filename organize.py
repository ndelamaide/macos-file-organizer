# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "ocrmac",
#     "ollama",
#     "pillow-heif",
#     "pydantic",
#     "pypdf",
#     "pypdfium2",
#     "python-docx",
# ]
# ///
"""
Local AI file organizer (Ollama). Nothing leaves your machine.

  uv run organize.py plan ~/Downloads ~/Desktop "~/My Drive"   # writes plan.csv, moves nothing
  # -> review plan.csv: edit dest/category, set action to "skip" for rows to leave alone
  uv run organize.py apply                                      # executes plan.csv, logs undo.csv

Settings live in config.toml next to this script (start from config.example.toml).
"""
import argparse, csv, hashlib, os, re, shutil, sys, tomllib
from pathlib import Path
from typing import Literal

import ollama
from pydantic import create_model

CONFIG_PATH = Path(__file__).with_name("config.toml")
PLAN, UNDO = Path("plan.csv"), Path("undo.csv")
PLAN_FIELDS = ["action", "src", "dest", "category", "suggested", "confidence", "note"]

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".heic", ".tiff", ".tif"}
TEXT_EXT = {".txt", ".md", ".csv", ".json"}
GOOGLE_STUBS = {".gdoc", ".gsheet", ".gslides", ".gdraw", ".gform", ".gmap", ".gsite"}
BUNDLES = (".app", ".photoslibrary", ".bundle", ".framework", ".pkg")
MIN_PDF_TEXT = 50  # below this many characters, treat the PDF as a scan and OCR it


# ---- config --------------------------------------------------------------

def expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"No config at {CONFIG_PATH}\nCreate it with: cp config.example.toml config.toml")
    with open(CONFIG_PATH, "rb") as fh:
        raw = tomllib.load(fh)

    cfg = {
        "model": raw.get("model", "qwen3.5:9b"),
        "max_chars": raw.get("max_chars", 3000),
        "ocr_languages": raw.get("ocr_languages", ["fr-FR", "en-US"]),
        "min_confidence": raw.get("min_confidence", 0.6),
        "inbox": raw.get("inbox", "Inbox"),
        "exclude": set(raw.get("exclude", [])),
        "root": expand(raw["destination"]) if "destination" in raw else None,
    }
    if "cloud" in cfg["model"]:
        sys.exit("config.toml: cloud models would send your files off-device; use a local model")
    if not raw.get("categories"):
        sys.exit("config.toml: add at least one entry under [categories]")

    cats = {}
    for name, v in raw["categories"].items():
        if isinstance(v, str):
            v = {"description": v}
        cats[name] = {"description": v.get("description", ""), "destination": v.get("destination")}
    cats.setdefault(cfg["inbox"], {"description": "No category above clearly fits", "destination": None})
    cfg["categories"] = cats
    return cfg


def category_dir(cfg: dict, name: str) -> Path:
    custom = cfg["categories"][name]["destination"]
    return expand(custom) if custom else cfg["root"] / name


# ---- scanning ------------------------------------------------------------

def iter_files(sources: list[Path], cfg: dict, sorted_dirs: set[Path]):
    """Yield files to sort. Skips hidden items, git repos, excluded names,
    app bundles and folders that are already sorted categories."""
    seen = set()
    for src in sources:
        for dirpath, dirnames, filenames in os.walk(src):
            d = Path(dirpath)
            if ".git" in dirnames or ".git" in filenames or d in sorted_dirs:
                dirnames[:] = []  # code repo or already sorted: don't descend
                continue
            dirnames[:] = [
                n for n in dirnames
                if not n.startswith(".") and n not in cfg["exclude"] and not n.endswith(BUNDLES)
            ]
            for name in filenames:
                f = d / name
                if not name.startswith(".") and f not in seen:
                    seen.add(f)
                    yield f


class DuplicateFinder:
    """Finds files with identical content. Compares sizes first and only
    hashes (SHA-256) when sizes match, so large trees stay fast."""

    def __init__(self):
        self.by_size: dict[int, list[Path]] = {}
        self._hashes: dict[Path, str] = {}

    def _hash(self, p: Path) -> str:
        if p not in self._hashes:
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            self._hashes[p] = h.hexdigest()
        return self._hashes[p]

    def add(self, p: Path):
        self.by_size.setdefault(p.stat().st_size, []).append(p)

    def check_and_add(self, p: Path) -> Path | None:
        """Return an existing file with the same content, or register p."""
        for other in self.by_size.get(p.stat().st_size, []):
            if self._hash(other) == self._hash(p):
                return other
        self.add(p)
        return None


def index_sorted(sorted_dirs: set[Path], finder: DuplicateFinder):
    """Register files already in category folders, so re-runs catch copies of them."""
    for d in sorted_dirs:
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [n for n in dirnames if not n.startswith(".")]
            for name in filenames:
                if not name.startswith("."):
                    finder.add(Path(dirpath) / name)


# ---- text extraction ------------------------------------------------------

def ocr(image, cfg: dict) -> str:
    from ocrmac import ocrmac  # Apple Vision, on-device
    res = ocrmac.OCR(image, language_preference=cfg["ocr_languages"]).recognize()
    return " ".join(text for text, _, _ in res)


def extract_text(p: Path, cfg: dict) -> str:
    ext = p.suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            text = "\n".join(pg.extract_text() or "" for pg in PdfReader(p).pages[:3])
            if len(text.strip()) < MIN_PDF_TEXT:  # scanned PDF: OCR the first page
                import pypdfium2 as pdfium
                text = ocr(pdfium.PdfDocument(p)[0].render(scale=2).to_pil(), cfg)
            return text
        if ext in IMAGE_EXT:
            from PIL import Image
            from pillow_heif import register_heif_opener
            register_heif_opener()
            return ocr(Image.open(p), cfg)
        if ext == ".docx":
            import docx
            return "\n".join(par.text for par in docx.Document(p).paragraphs)
        if ext in TEXT_EXT:
            return p.read_text(errors="ignore")
    except Exception as e:
        print(f"  ! could not read {p.name}: {e}")
    return ""  # Google stubs, audio, etc. -> classified by filename only


# ---- classification --------------------------------------------------------

def make_decision_model(cfg: dict):
    return create_model(
        "Decision",
        category=(Literal[tuple(cfg["categories"])], ...),
        new_name=(str, ...),
        confidence=(float, ...),
    )


def classify(p: Path, cfg: dict, Decision):
    text = extract_text(p, cfg)[: cfg["max_chars"]]
    menu = "\n".join(f"- {name}: {c['description']}" for name, c in cfg["categories"].items())
    prompt = (
        "You sort personal files (documents may be in French or English). "
        f"Pick the single best category:\n{menu}\n\n"
        "Also propose a short new filename (no extension): lowercase, words joined by "
        "hyphens, prefixed with YYYY-MM-DD if the document's date is clear. "
        "Give your confidence from 0 to 1.\n\n"
        f"Filename: {p.name}\nContent:\n{text or '(no readable content)'}"
    )
    r = ollama.chat(
        model=cfg["model"],
        messages=[{"role": "user", "content": prompt}],
        format=Decision.model_json_schema(),
        options={"temperature": 0},
        think=False,
    )
    return Decision.model_validate_json(r.message.content)


def safe_name(s: str) -> str:
    return re.sub(r"[^a-z0-9\-_]", "", s.lower().replace(" ", "-"))[:80] or "file"


# ---- commands -------------------------------------------------------------

def plan(sources: list[Path], cfg: dict):
    if cfg["root"] is None:
        sys.exit("Set 'destination' in config.toml or pass --into")
    Decision = make_decision_model(cfg)
    sorted_dirs = {category_dir(cfg, c) for c in cfg["categories"]}
    files = list(iter_files(sources, cfg, sorted_dirs))
    finder = DuplicateFinder()
    index_sorted(sorted_dirs, finder)
    counts = {"move": 0, "duplicate": 0, "skip": 0, "inbox": 0}

    with open(PLAN, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=PLAN_FIELDS)
        w.writeheader()
        for i, f in enumerate(files, 1):
            print(f"[{i}/{len(files)}] {f}")
            row = {"src": f}
            try:
                original = finder.check_and_add(f)
            except OSError as e:
                w.writerow(row | {"action": "skip", "note": f"unreadable: {e}"}); counts["skip"] += 1
                continue
            if original:
                w.writerow(row | {"action": "duplicate", "note": f"same content as {original}"})
                counts["duplicate"] += 1
                continue

            try:
                d = classify(f, cfg, Decision)
            except Exception as e:
                w.writerow(row | {"action": "skip", "note": f"model error: {e}"}); counts["skip"] += 1
                continue

            cat = d.category if d.confidence >= cfg["min_confidence"] else cfg["inbox"]
            counts["inbox"] += cat == cfg["inbox"]
            row |= {"category": cat, "suggested": d.category, "confidence": round(d.confidence, 2)}

            ext = f.suffix.lower()
            if ext in GOOGLE_STUBS:
                if cfg["categories"][cat]["destination"]:
                    w.writerow(row | {"action": "skip", "note": "Google-native file can't leave Drive"})
                    counts["skip"] += 1
                    continue
                name = f.name  # renaming a stub isn't worth the risk
            else:
                name = safe_name(d.new_name) + ext

            w.writerow(row | {"action": "move", "dest": category_dir(cfg, cat) / name})
            counts["move"] += 1

    print(
        f"\nWrote {PLAN}: {counts['move']} to move ({counts['inbox']} to {cfg['inbox']}), "
        f"{counts['duplicate']} duplicates, {counts['skip']} skipped.\n"
        "Review it, then run: uv run organize.py apply"
    )


def apply():
    if not PLAN.exists():
        sys.exit("No plan.csv here. Run: uv run organize.py plan <folders>")
    moved = 0
    with open(PLAN, newline="") as fh, open(UNDO, "a", newline="") as undo:
        log = csv.writer(undo)
        for row in csv.DictReader(fh):
            if row["action"] != "move" or not row["dest"]:
                continue
            src, dest = Path(row["src"]), Path(row["dest"])
            if not src.exists() or src == dest:
                continue
            base, n = dest, 1
            while dest.exists():  # never overwrite
                dest = base.with_stem(f"{base.stem}-{n}"); n += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, dest)
            log.writerow([dest, src])
            moved += 1
            print(f"{src}  ->  {dest}")
    print(f"\nMoved {moved} files. Each move is logged in {UNDO} (new path, original path).")


def main():
    ap = argparse.ArgumentParser(description="Sort files into folders with a local LLM.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="classify files and write plan.csv (moves nothing)")
    p.add_argument("sources", nargs="+", help="folders to scan")
    p.add_argument("--into", help="destination root (overrides config.toml)")
    sub.add_parser("apply", help="execute plan.csv")
    args = ap.parse_args()

    if args.cmd == "apply":
        return apply()
    cfg = load_config()
    if args.into:
        cfg["root"] = expand(args.into)
    sources = [expand(s) for s in args.sources]
    for s in sources:
        if not s.is_dir():
            sys.exit(f"Not a folder: {s}")
    plan(sources, cfg)


if __name__ == "__main__":
    main()
