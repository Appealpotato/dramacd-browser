import argparse
import os
import sys
from pathlib import Path

from text_cleaning import clean_dialogue_line


def _read_text_with_fallback(path: Path) -> str:
    encodings = ("utf-8-sig", "utf-8", "cp932")
    last_error = None
    for enc in encodings:
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise UnicodeDecodeError(
        "unknown",
        b"",
        0,
        1,
        f"Could not decode file with utf-8-sig/utf-8/cp932 ({last_error})",
    )


def extract_dialogue(input_filepath: str, output_filepath: str, keep_duplicates: bool = False, drop_sfx: bool = True) -> int:
    """
    Extract clean dialogue from subtitle-like files (SRT/VTT).

    Returns the number of written dialogue lines.
    """
    input_path = Path(input_filepath)
    output_path = Path(output_filepath)

    raw_text = _read_text_with_fallback(input_path)
    dialogue_lines = []
    seen = set()

    for raw_line in raw_text.splitlines():
        line = clean_dialogue_line(raw_line, drop_sfx=drop_sfx)
        if not line:
            continue

        if not keep_duplicates:
            if line in seen:
                continue
            seen.add(line)

        dialogue_lines.append(line)

    output_path.write_text("\n".join(dialogue_lines), encoding="utf-8")
    return len(dialogue_lines)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract dialogue text from subtitle files for cleaner AI translation input."
    )
    parser.add_argument("input_files", nargs="+", help="One or more subtitle files (.srt/.vtt/.txt).")
    parser.add_argument(
        "--keep-duplicates",
        action="store_true",
        help="Keep repeated lines instead of deduplicating them.",
    )
    parser.add_argument(
        "--keep-sfx",
        action="store_true",
        help="Keep bracketed SFX lines like [BGM] or (laughs).",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    exit_code = 0

    for input_file in args.input_files:
        base, _ = os.path.splitext(input_file)
        output_file = f"{base}_dialogue.txt"

        print(f"\nProcessing: {input_file}")
        try:
            count = extract_dialogue(
                input_file,
                output_file,
                keep_duplicates=args.keep_duplicates,
                drop_sfx=not args.keep_sfx,
            )
            print(f"OK: wrote {count} lines -> {output_file}")
        except FileNotFoundError:
            exit_code = 1
            print(f"ERROR: input file not found: {input_file}", file=sys.stderr)
        except Exception as exc:
            exit_code = 1
            print(f"ERROR: failed processing {input_file}: {exc}", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
