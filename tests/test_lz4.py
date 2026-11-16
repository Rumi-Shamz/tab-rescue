import json
import random
import struct
import unittest

from tab_rescue.lz4 import LZ4Error, decompress_block, read_mozlz4, MOZLZ4_MAGIC
from .fixtures import lz4_compress, mozlz4_bytes, mozlz4_literal_bytes


class Lz4BlockTests(unittest.TestCase):
    def test_literals_only(self):
        self.assertEqual(decompress_block(bytes([0x30]) + b"abc"), b"abc")

    def test_empty(self):
        self.assertEqual(decompress_block(b"\x00"), b"")

    def test_hand_built_match(self):
        # literals "abcd", match offset 4 length 4+2=6, then final literals "xy"
        block = bytes([0x42]) + b"abcd" + struct.pack("<H", 4) + bytes([0x20]) + b"xy"
        self.assertEqual(decompress_block(block), b"abcd" + b"abcdab" + b"xy")

    def test_overlapping_match_is_run_length(self):
        # literal "a", match offset 1 length 4+6=10 -> 11 x 'a', then literal "b"
        block = bytes([0x16]) + b"a" + struct.pack("<H", 1) + bytes([0x10]) + b"b"
        self.assertEqual(decompress_block(block), b"a" * 11 + b"b")

    def test_extended_lengths(self):
        lit = bytes(range(65, 91)) * 2  # 52 literals -> 15 + 37
        # match length 4 + 15 + 255 + 10 = 284 at offset 52
        block = bytes([0xFF, 37]) + lit + struct.pack("<H", 52) + bytes([255, 10]) + bytes([0x10]) + b"!"
        expected = bytearray(lit)
        for _ in range(284):
            expected.append(expected[-52])
        expected += b"!"
        self.assertEqual(decompress_block(block), bytes(expected))

    def test_size_hint_checked(self):
        with self.assertRaises(LZ4Error):
            decompress_block(bytes([0x30]) + b"abc", 5)

    def test_corrupt_input(self):
        for bad in (b"\x40ab", b"\x11a\x00\x00", b"\x11a\x05\x00", b"\xf0"):
            with self.assertRaises(LZ4Error):
                decompress_block(bad)

    def test_round_trip_with_real_matches(self):
        rng = random.Random(7)
        payloads = [
            b"",
            b"short",
            b"abcabcabcabcabcabcabcabcabcabc" * 3,
            b"x" * 1000,
            json.dumps([{"url": f"https://example.com/p/{i % 17}", "title": "Title %d" % (i % 5)} for i in range(400)]).encode(),
            bytes(rng.randrange(256) for _ in range(3000)),
            (bytes(rng.randrange(256) for _ in range(50)) * 40) + b"tail-literals",
        ]
        for p in payloads:
            comp = lz4_compress(p)
            self.assertEqual(decompress_block(comp, len(p)), p)
        # the compressor really found matches on repetitive data
        self.assertLess(len(lz4_compress(payloads[4])), len(payloads[4]) // 4)


class MozLz4Tests(unittest.TestCase):
    def test_container_round_trip(self):
        obj = {"windows": [{"tabs": [{"entries": [{"url": "https://example.com/", "title": "é☃"}]}]}] * 20}
        for build in (mozlz4_bytes, mozlz4_literal_bytes):
            self.assertEqual(json.loads(read_mozlz4(build(obj))), obj)

    def test_bad_magic(self):
        with self.assertRaises(LZ4Error):
            read_mozlz4(b"nope" * 5)

    def test_header_constants(self):
        data = mozlz4_bytes({"a": 1})
        self.assertTrue(data.startswith(MOZLZ4_MAGIC))
        self.assertEqual(struct.unpack_from("<I", data, 8)[0], len(json.dumps({"a": 1})))


if __name__ == "__main__":
    unittest.main()
