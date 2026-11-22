import unittest

from textkit import slugify


class TestSlugify(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_punctuation_is_dropped(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_double_space_gives_double_hyphen(self):
        self.assertEqual(slugify("Hello  World"), "hello--world")
