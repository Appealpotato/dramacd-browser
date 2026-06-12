"""_parse_json_payload — the metadata-translation JSON extractor must survive
the common non-pure-JSON shapes models actually return."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers.api import _coerce_notes_list, _escape_inner_quotes, _parse_json_payload

OBJ = '{"title_en": "Test", "description_en": "Desc"}'


class ParseJsonPayloadTests(unittest.TestCase):
    def test_pure_json(self):
        self.assertEqual(_parse_json_payload(OBJ)["title_en"], "Test")

    def test_code_fence(self):
        self.assertEqual(_parse_json_payload(f"```json\n{OBJ}\n```")["title_en"], "Test")

    def test_think_block(self):
        raw = f"<think>\nLet me translate this…\n</think>\n{OBJ}"
        self.assertEqual(_parse_json_payload(raw)["title_en"], "Test")

    def test_prose_around_json(self):
        raw = f"Here is the translation you requested:\n\n{OBJ}\n\nLet me know if you need anything else!"
        self.assertEqual(_parse_json_payload(raw)["title_en"], "Test")

    def test_array_payload(self):
        self.assertEqual(_parse_json_payload(f"sure!\n[{OBJ}]")[0]["title_en"], "Test")

    def test_unparseable_raises_with_snippet(self):
        with self.assertRaises(ValueError) as ctx:
            _parse_json_payload("I cannot translate this content.")
        self.assertIn("I cannot translate", str(ctx.exception))

    def test_unescaped_inner_quotes(self):
        # Real failure shape (item RJ01432516): 「うーちゃん」 translated to
        # "Uu-chan" with literal quotes inside the JSON string value.
        raw = (
            '{"title_en":"Test","description_en":"The one who picked up your '
            'beloved stuffed animal "Uu-chan" was a young rider.","seiyuu_en":[]}'
        )
        parsed = _parse_json_payload(raw)
        self.assertIn('"Uu-chan"', parsed["description_en"])

    def test_unescaped_quotes_in_fenced_json(self):
        raw = '```json\n{"title_en":"He said "hello" loudly"}\n```'
        self.assertEqual(_parse_json_payload(raw)["title_en"], 'He said "hello" loudly')


class EscapeInnerQuotesTests(unittest.TestCase):
    def test_valid_json_untouched(self):
        raw = '{"a": "x", "b": ["y", "z"], "c": {"d": "w"}}'
        self.assertEqual(_escape_inner_quotes(raw), raw)

    def test_already_escaped_untouched(self):
        raw = '{"a": "say \\"hi\\" now"}'
        self.assertEqual(_escape_inner_quotes(raw), raw)

    def test_repairs_inner_quotes(self):
        raw = '{"a": "the "thing" here"}'
        self.assertEqual(
            __import__("json").loads(_escape_inner_quotes(raw))["a"],
            'the "thing" here',
        )


class CoerceNotesListTests(unittest.TestCase):
    def test_string_note_kept(self):
        self.assertEqual(_coerce_notes_list("One prose note."), ["One prose note."])

    def test_list_passthrough_capped(self):
        self.assertEqual(len(_coerce_notes_list(["a", "b", "c", "d"])), 3)

    def test_none_and_junk(self):
        self.assertEqual(_coerce_notes_list(None), [])
        self.assertEqual(_coerce_notes_list(42), [])


if __name__ == "__main__":
    unittest.main()
