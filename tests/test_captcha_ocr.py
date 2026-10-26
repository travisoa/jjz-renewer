import unittest

from jjz.captcha_ocr import recognize_captcha


class CaptchaOcrTests(unittest.TestCase):
    def test_classifier_result_is_reduced_to_one_alphanumeric_guess(self):
        image = b"\x89PNG\r\n\x1a\nSYNTHETIC"
        self.assertEqual(recognize_captcha(image, classify=lambda _: " ab-12 "), "ab12")
        self.assertIsNone(recognize_captcha(image, classify=lambda _: "12"))
        self.assertIsNone(recognize_captcha(image, classify=lambda _: "汉字"))
        self.assertIsNone(recognize_captcha(b"", classify=lambda _: "abcd"))

    def test_oversized_image_never_reaches_an_engine(self):
        def png(width, height):
            return (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + width.to_bytes(4, "big")
                    + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00SYNTHETIC")

        def gif(width, height):
            return b"GIF89a" + width.to_bytes(2, "little") + height.to_bytes(2, "little") + b"SYNTHETIC"

        def jpeg(width, height):
            app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            frame = b"\xff\xff\xc2\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big")
            return b"\xff\xd8" + app0 + frame + b"\x03SYNTHETIC"

        for build in (png, gif, jpeg):
            with self.subTest(format=build.__name__):
                calls = []
                classify = lambda image: calls.append(image) or "ab12"
                self.assertEqual(recognize_captcha(build(120, 40), classify=classify), "ab12")
                self.assertIsNone(recognize_captcha(build(1001, 1000), classify=classify))
                self.assertEqual(len(calls), 1)

    def test_classifier_failure_returns_no_guess(self):
        def broken(_image):
            raise RuntimeError("SYNTHETIC_OCR_DETAIL")

        self.assertIsNone(recognize_captcha(b"\x89PNG\r\n\x1a\nSYNTHETIC", classify=broken))


if __name__ == "__main__":
    unittest.main()
