from __future__ import annotations

import unittest

import ebayda_helper


class LaunchUrlTests(unittest.TestCase):
    def test_valid_launch_url_is_parsed(self) -> None:
        request = ebayda_helper.parse_launch_url(
            "ebayda://run?job_id=job_abc-123&ticket=abcdefghijklmnop"
        )

        self.assertEqual(request.job_id, "job_abc-123")
        self.assertEqual(request.ticket, "abcdefghijklmnop")

    def test_invalid_protocol_shape_is_rejected(self) -> None:
        invalid_urls = (
            "https://run?job_id=job_1&ticket=abcdefghijklmnop",
            "ebayda://bind?job_id=job_1&ticket=abcdefghijklmnop",
            "ebayda://run/path?job_id=job_1&ticket=abcdefghijklmnop",
            "ebayda://run?job_id=job_1&ticket=abcdefghijklmnop#fragment",
        )

        for value in invalid_urls:
            with self.subTest(value=value), self.assertRaises(ebayda_helper.HelperError):
                ebayda_helper.parse_launch_url(value)

    def test_missing_duplicate_unknown_or_unsafe_parameters_are_rejected(self) -> None:
        invalid_urls = (
            "ebayda://run?job_id=job_1",
            "ebayda://run?ticket=abcdefghijklmnop",
            "ebayda://run?job_id=job_1&job_id=job_2&ticket=abcdefghijklmnop",
            "ebayda://run?job_id=job_1&ticket=abcdefghijklmnop&extra=1",
            "ebayda://run?job_id=../job&ticket=abcdefghijklmnop",
            "ebayda://run?job_id=job_1&ticket=short",
        )

        for value in invalid_urls:
            with self.subTest(value=value), self.assertRaises(ebayda_helper.HelperError):
                ebayda_helper.parse_launch_url(value)
