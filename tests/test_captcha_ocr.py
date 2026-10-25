import unittest

from jjz.captcha_ocr import recognize_captcha


class CaptchaOcrTests(unittest.TestCase):
    def test_classifier_result_is_reduced_to_one_alphanumeric_guess(self):
        image = b"\x89PNG\r\n\x1a\nSYNTHETIC"
        self.assertEqual(recognize_captcha(image, classify=lambda _: " ab-12 "), "ab12")
        self.assertIsNone(recognize_captcha(image, classify=lambda _: "12"))
        self.assertIsNone(recognize_captcha(image, classify=lambda _: "汉字"))
        self.assertIsNone(recognize_captcha(b"", classify=lambda _: "abcd"))

    def test_classifier_failure_returns_no_guess(self):
        def broken(_image):
            raise RuntimeError("SYNTHETIC_OCR_DETAIL")

        self.assertIsNone(recognize_captcha(b"\x89PNG\r\n\x1a\nSYNTHETIC", classify=broken))


if __name__ == "__main__":
    unittest.main()
