"""Command line entry point.

Phase 0 ships the command surface and a real ``doctor``. Every pipeline command
exists now and prints which phase will implement it, so the gate can be checked
before any scraping or model code is written::

    python -m app.cli --help
    python -m app.cli doctor
    python -m app.cli doctor --fix
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import platform
import sys
from pathlib import Path

from . import __version__
from .config import (
    DATA_SUBDIRS,
    DEFAULT_CONFIG_PATH,
    KNOWN_PROVIDERS,
    PROJECT_ROOT,
    ConfigError,
    load_settings,
)

# command name -> (phase, one-line description)
COMMANDS: dict[str, tuple[int, str]] = {
    "sources validate": (1, "Validate the 21-entry source registry and check reachability"),
    "load": (1, "Fetch source pages and extract typed sections into data/01_raw"),
    "chunk": (2, "Build fact-card chunks into data/02_chunks"),
    "embed": (3, "Embed chunks with the configured model into data/03_embeddings"),
    "index": (4, "Upsert embeddings into the vector DB"),
    "retrieve": (5, "Run retrieval only and print the trace"),
    "answer": (6, "Run the full guarded answer path"),
    "eval": (7, "Score the golden set (hit@3, fact accuracy, refusal accuracy)"),
}

_OPTIONAL_DEPS: tuple[tuple[str, str], ...] = (
    ("yaml", "PyYAML"),
    ("pydantic", "pydantic"),
    ("dotenv", "python-dotenv"),
    ("httpx", "httpx"),
    ("tenacity", "tenacity"),
    ("selectolax", "selectolax"),
    ("bs4", "beautifulsoup4"),
    ("tiktoken", "tiktoken"),
    ("rapidfuzz", "rapidfuzz"),
    ("numpy", "numpy"),
    ("sentence_transformers", "sentence-transformers"),
    ("chromadb", "chromadb"),
    ("groq", "groq"),
    ("streamlit", "streamlit"),
    ("pytest", "pytest"),
)


def _package_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:  # pragma: no cover - metadata can be broken in odd envs
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Groww MF Facts Assistant - transparent, facts-only RAG pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "start here:\n"
            "  python -m app.cli doctor\n"
            "  python -m app.cli sources validate\n"
            "  python -m app.cli load && python -m app.cli chunk && "
            "python -m app.cli embed && python -m app.cli index\n"
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="path to config.yaml (default: ./config/config.yaml)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, description=help_text)

    sources = add("sources", "Inspect the source registry")
    sources_sub = sources.add_subparsers(dest="sources_command", metavar="<action>", required=True)
    validate = sources_sub.add_parser(
        "validate", help=COMMANDS["sources validate"][1], description=COMMANDS["sources validate"][1]
    )
    validate.add_argument(
        "--offline",
        action="store_true",
        help="validate the registry only; skip network reachability checks",
    )
    validate.set_defaults(func=cmd_sources_validate, command_name="sources validate")

    load = add("load", COMMANDS["load"][1])
    load.add_argument("--force", action="store_true", help="ignore the raw HTML cache")
    load.add_argument("--only", metavar="SOURCE_ID", help="fetch a single source id")
    load.set_defaults(func=cmd_load, command_name="load")

    chunk = add("chunk", COMMANDS["chunk"][1])
    chunk.add_argument(
        "--force",
        action="store_true",
        help="rewrite every chunk (chunking is deterministic; there is no chunk cache)",
    )
    chunk.set_defaults(func=cmd_chunk, command_name="chunk")

    embed = add("embed", COMMANDS["embed"][1])
    embed.add_argument("--rebuild", action="store_true", help="ignore the embedding cache")
    embed.set_defaults(func=cmd_embed, command_name="embed")

    index = add("index", COMMANDS["index"][1])
    index.add_argument("--rebuild", action="store_true", help="delete and recreate the collection")
    index.add_argument(
        "--skip-smoke",
        action="store_true",
        help="skip the five gate queries (they load the embedding model)",
    )
    index.set_defaults(func=cmd_index, command_name="index")

    retrieve = add("retrieve", COMMANDS["retrieve"][1])
    retrieve.add_argument("--query", "-q", required=True, help="question to retrieve for")
    retrieve.add_argument("--scheme", metavar="SLUG", help="session scheme slug to inherit")
    retrieve.add_argument("--debug", action="store_true", help="print the candidate table")
    retrieve.set_defaults(func=cmd_retrieve, command_name="retrieve")

    answer = add("answer", COMMANDS["answer"][1])
    answer.add_argument("--query", "-q", required=True, help="question to answer")
    answer.add_argument("--scheme", metavar="SLUG", help="session scheme slug to inherit")
    answer.add_argument("--debug", action="store_true", help="print prompt and validation details")
    answer.set_defaults(func=cmd_answer, command_name="answer")

    evaluation = add("eval", COMMANDS["eval"][1])
    evaluation.add_argument("--golden-set", metavar="PATH", help="override the golden-set path")
    evaluation.add_argument("--limit", type=int, default=None, help="score only the first N cases")
    evaluation.set_defaults(func=_run_stub, command_name="eval")

    doctor = add("doctor", "Check config, directories, packages, and provider key presence")
    doctor.add_argument("--fix", action="store_true", help="create any missing data/ directories")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def _run_stub(args: argparse.Namespace) -> int:
    name = getattr(args, "command_name", args.command)
    phase = COMMANDS[name][0]
    print(f"{name}: not implemented yet (build it in phase {phase} of implementation.md)")
    query = getattr(args, "query", None)
    if query:
        print(f"  query: {query!r}")
    return 0


def cmd_sources_validate(args: argparse.Namespace) -> int:
    from .pipeline.validate import run_validation

    settings = load_settings(args.config)
    summary = run_validation(settings, offline=args.offline)
    print(summary.describe())
    return 0 if summary.ok else 1


def cmd_load(args: argparse.Namespace) -> int:
    from .pipeline.load import run_load

    settings = load_settings(args.config)
    summary = run_load(settings, force=args.force, only=args.only)
    print(summary.describe())
    return 0 if summary.ok else 1


def cmd_chunk(args: argparse.Namespace) -> int:
    from .pipeline.chunk import run_chunk

    settings = load_settings(args.config)
    summary = run_chunk(settings, force=args.force)
    print(summary.describe())
    return 0 if summary.ok else 1


def cmd_embed(args: argparse.Namespace) -> int:
    from .pipeline.embed import run_embed

    settings = load_settings(args.config)
    summary = run_embed(settings, force=args.rebuild)
    print(summary.describe())
    return 0 if summary.ok else 1


def cmd_index(args: argparse.Namespace) -> int:
    from .pipeline.index import run_index

    settings = load_settings(args.config)
    summary = run_index(
        settings,
        rebuild=args.rebuild,
        verify_smoke=not args.skip_smoke,
    )
    print(summary.describe())
    return 0 if summary.ok else 1


def cmd_retrieve(args: argparse.Namespace) -> int:
    from .pipeline.retrieve import retrieve

    settings = load_settings(args.config)
    result = retrieve(
        settings,
        query=args.query,
        session_last_scheme=args.scheme,
    )
    print(f"status: {result.status}")
    print(f"intent: {result.intent}   scheme: {result.scheme_slug or '-'} ({result.scheme_source or 'none'})")
    print(f"filters: {result.filters or '-'}")
    print(f"top similarity: {result.top_similarity if result.top_similarity is not None else '-'}")
    if result.reason:
        print(f"reason: {result.reason}")
    if result.message:
        print(f"message: {result.message}")
    print(f"trace: {result.trace_path}")
    if args.debug and result.candidates:
        print()
        print(f"{'#':>2}  {'similarity':>10}  {'adjusted':>9}  {'fact':<20}  chunk_id")
        for index, candidate in enumerate(result.candidates[:20], start=1):
            marker = "*" if candidate in result.context_chunks else " "
            print(
                f"{marker}{index:>2}  {candidate.similarity:>10.4f}  "
                f"{candidate.adjusted:>9.4f}  {candidate.fact_type:<20}  {candidate.chunk_id}"
            )
    if result.context_chunks:
        print()
        print("context:")
        for index, candidate in enumerate(result.context_chunks, start=1):
            print(f"  [S{index}] {candidate.chunk_id}")
            print(f"       {candidate.preview(160)}")
    return 0 if result.status in {"ok", "clarify"} else 1


def cmd_answer(args: argparse.Namespace) -> int:
    from .pipeline.answer import AnswerService

    settings = load_settings(args.config)
    service = AnswerService(settings)
    summary = service.answer_with_trace(args.query, last_scheme=args.scheme)
    print(summary.describe())
    if args.debug and summary.prompt:
        print()
        print("--- prompt ---")
        print(summary.prompt)
        print("--- validation ---")
        print(summary.validation.model_dump_json(indent=2) if summary.validation else "{}")
    return 0 if summary.result.status in {"answered", "clarify"} else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    if args.fix:
        settings.ensure_data_dirs()

    base = settings.data_dir_path
    existing = [sub for sub in DATA_SUBDIRS if (base / sub).is_dir()]
    env_name = settings.api_key_env
    key_state = "set" if os.environ.get(env_name) else "missing (only needed by answer/eval)"
    sources_path = settings.sources_path
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path

    info = [
        ("python", f"{platform.python_version()}  ({sys.executable})"),
        ("project", str(PROJECT_ROOT)),
        ("venv", "ok (.venv)" if (PROJECT_ROOT / ".venv").is_dir() else "not found (optional)"),
        ("config", f"{config_path}  sha256:{settings.config_sha256[:12]}"),
        ("provider", f"{settings.llm_provider}  model:{settings.llm_model}"),
        ("api key", f"{env_name} = {key_state}"),
        ("data dir", f"{base}  ({len(existing)}/{len(DATA_SUBDIRS)} subdirs)"),
        (
            "sources",
            f"{sources_path}  ({'found' if sources_path.exists() else 'missing - created in phase 1'})",
        ),
    ]

    present: list[str] = []
    missing: list[str] = []
    for module, distribution in _OPTIONAL_DEPS:
        version = _package_version(distribution)
        if version is None:
            missing.append(distribution)
        else:
            present.append(f"{distribution}={version}")

    width = max(len(label) for label, _ in info)
    print("Groww MF Facts Assistant - doctor")
    print("-" * 46)
    for label, value in info:
        print(f"{label:<{width}}  {value}")
    print(f"{'packages':<{width}}  " + (", ".join(present) if present else "none"))
    if missing:
        print(f"{'missing':<{width}}  " + ", ".join(missing))
        print(f"{'':<{width}}  fix: .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt")

    known = ", ".join(KNOWN_PROVIDERS)
    print("-" * 46)
    print(f"providers available: {known}")
    if settings.llm_provider not in KNOWN_PROVIDERS:
        print(f"phase 0: FAILED (unknown provider {settings.llm_provider!r})")
        return 1
    if len(existing) != len(DATA_SUBDIRS):
        print("phase 0: incomplete - run: python -m app.cli doctor --fix")
        return 0
    print("phase 0: ok (config loads, directories ready, no API key required)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
