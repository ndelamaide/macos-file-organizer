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

  uv run organize.py plan ~/Downloads ~/Desktop "~/My Drive"   # writes runs/plan-<timestamp>.csv, moves nothing
  # -> review the plan: edit dest/category, set action to "skip" for rows to leave alone
  uv run organize.py apply                                      # executes the latest plan, logs runs/undo-<timestamp>.csv
  uv run organize.py apply runs/plan-20260926-165700.csv        # or a specific plan

Settings live in config.toml next to this script (start from config.example.toml).
"""
import argparse, csv, hashlib, os, re, shutil, sys, tomllib
from datetime import datetime
from pathlib import Path
from typing import Literal

import ollama
from pydantic import create_model

CONFIG_PATH = Path(__file__).with_name("config.toml")
RUNS_DIR = Path(__file__).with_name("runs")  # plans and undo logs, one file per run
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
        "exclude": {e for e in raw.get("exclude", []) if not e.startswith(("/", "~"))},
        "exclude_paths": {expand(e) for e in raw.get("exclude", []) if e.startswith(("/", "~"))},
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

    def warn(err: OSError):
        print(f"  ! cannot read {err.filename}: {err.strerror}", file=sys.stderr)
        if err.errno == 1:  # EPERM: macOS privacy protection
            print("    Allow your terminal app in System Settings > Privacy & Security > "
                  "Files & Folders (or Full Disk Access), then restart it.", file=sys.stderr)

    for src in sources:
        found = 0
        for dirpath, dirnames, filenames in os.walk(src, onerror=warn):
            d = Path(dirpath)
            if ".git" in dirnames or ".git" in filenames or d in sorted_dirs or d in cfg["exclude_paths"]:
                dirnames[:] = []  # code repo, already sorted or excluded: don't descend
                continue
            dirnames[:] = [
                n for n in dirnames
                if not n.startswith(".") and n not in cfg["exclude"] and not n.endswith(BUNDLES)
            ]
            for name in filenames:
                f = d / name
                if not name.startswith(".") and f not in seen:
                    seen.add(f)
                    found += 1
                    yield f
        print(f"Scanned {src}: {found} files", file=sys.stderr)


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


# ---- run files ------------------------------------------------------------

def new_run_file(kind: str) -> Path:
    """runs/<kind>-YYYYMMDD-HHMMSS.csv, with a -1, -2… suffix if that name is taken."""
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path, n = RUNS_DIR / f"{kind}-{stamp}.csv", 1
    while path.exists():
        path = RUNS_DIR / f"{kind}-{stamp}-{n}.csv"; n += 1
    return path


def latest_plan() -> Path | None:
    def key(p: Path):  # plan-DATE-TIME[-N]
        parts = p.stem.split("-")
        return parts[1], parts[2], int(parts[3]) if len(parts) > 3 else 0
    plans = sorted(RUNS_DIR.glob("plan-*.csv"), key=key) if RUNS_DIR.exists() else []
    return plans[-1] if plans else None


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
    plan_path = new_run_file("plan")

    with open(plan_path, "x", newline="") as fh:
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
        f"\nWrote {plan_path}: {counts['move']} to move ({counts['inbox']} to {cfg['inbox']}), "
        f"{counts['duplicate']} duplicates, {counts['skip']} skipped.\n"
        "Review it, then run: uv run organize.py apply"
    )


def apply(plan_path: Path | None):
    plan_path = plan_path or latest_plan()
    if plan_path is None:
        sys.exit("No plan found in runs/. Run: uv run organize.py plan <folders>")
    if not plan_path.is_file():
        sys.exit(f"Plan not found: {plan_path}")
    print(f"Applying {plan_path}\n")

    moved, undo_path, undo_fh, log = 0, None, None, None
    try:
        with open(plan_path, newline="") as fh:
            for row in csv.DictReader(fh):
                if row["action"] != "move" or not row["dest"]:
                    continue
                src, dest = Path(row["src"]), Path(row["dest"])
                if not src.exists() or src == dest:
                    continue
                base, n = dest, 1
                while dest.exists():  # never overwrite
                    dest = base.with_stem(f"{base.stem}-{n}"); n += 1
                if log is None:  # create the undo log only once something moves
                    undo_path = new_run_file("undo")
                    undo_fh = open(undo_path, "x", newline="")
                    log = csv.writer(undo_fh)
                    log.writerow(["new_path", "original_path"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(src, dest)
                log.writerow([dest, src])
                undo_fh.flush()  # keep the log accurate even if the run is interrupted
                moved += 1
                print(f"{src}  ->  {dest}")
    finally:
        if undo_fh:
            undo_fh.close()

    if moved:
        print(f"\nMoved {moved} files. Undo log: {undo_path} (new path, original path).")
    else:
        print("Nothing to move: every 'move' row is already done or its source is gone.")


def main():
    ap = argparse.ArgumentParser(description="Sort files into folders with a local LLM.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="classify files and write runs/plan-<timestamp>.csv (moves nothing)")
    p.add_argument("sources", nargs="+", help="folders to scan")
    p.add_argument("--into", help="destination root (overrides config.toml)")
    a = sub.add_parser("apply", help="execute a plan (the latest one by default)")
    a.add_argument("plan", nargs="?", type=Path, help="plan file to apply (default: latest in runs/)")
    args = ap.parse_args()

    if args.cmd == "apply":
        return apply(args.plan)
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
