import unittest

from Crawler.cleaner import clean_content
from Crawler.links import content_url
from RAG.text import chunk_text


class TextProcessingTests(unittest.TestCase):
    def test_chunk_text_returns_overlapping_windows(self):
        chunks = chunk_text("abcdefghij", chunk_size=6, overlap=2)

        self.assertEqual(chunks, ["abcdef", "efghij", "ij"])

    def test_clean_content_converts_simple_table(self):
        content = "| Name | Value |\n| --- | --- |\n| Club | KOSS |\n"

        self.assertEqual(clean_content(content), "Name: Value\nClub: KOSS")

    def test_content_url_keeps_course_titles_with_colons(self):
        self.assertEqual(
            content_url("/w/CS10001:_Programming_and_Data_Structures"),
            "https://wiki.metakgp.org/w/CS10001:_Programming_and_Data_Structures",
        )
        self.assertIsNone(content_url("/w/Category:Courses"))


if __name__ == "__main__":
    unittest.main()
