import unittest

from imapgw.bytecache import ByteCache


class ByteCacheTests(unittest.TestCase):
    def test_put_get_and_budget_eviction(self):
        c = ByteCache(10)
        c.put("a", b"1234")
        c.put("b", b"5678")
        self.assertEqual(c.size, 8)
        self.assertEqual(c.get("a"), b"1234")  # a becomes most recent
        c.put("c", b"9999")  # evicts b (least recent)
        self.assertIsNone(c.get("b"))
        self.assertEqual(c.get("a"), b"1234")
        self.assertEqual(c.get("c"), b"9999")
        self.assertLessEqual(c.size, 10)

    def test_oversize_value_not_cached(self):
        c = ByteCache(3)
        c.put("big", b"12345")
        self.assertIsNone(c.get("big"))
        self.assertEqual(c.size, 0)

    def test_replace_and_discard(self):
        c = ByteCache(100)
        c.put("k", b"aaa")
        c.put("k", b"bb")
        self.assertEqual((c.get("k"), c.size), (b"bb", 2))
        c.discard("k")
        self.assertEqual((c.get("k"), c.size, len(c)), (None, 0, 0))

    def test_rejects_str(self):
        with self.assertRaises(TypeError):
            ByteCache(10).put("k", "text")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
