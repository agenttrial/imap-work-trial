import base64
import unittest

from imapgw.search import _decoded_body, _decoded_headers


class DecodedContentTests(unittest.TestCase):
    def test_base64_and_qp_bodies_decode(self):
        b64 = base64.b64encode("café in base64".encode()).decode()
        raw = (
            "Subject: =?UTF-8?B?Y2Fmw6k=?=\r\nMIME-Version: 1.0\r\n"
            'Content-Type: multipart/alternative; boundary="b"\r\n\r\n'
            "--b\r\nContent-Type: text/plain; charset=utf-8\r\n"
            f"Content-Transfer-Encoding: base64\r\n\r\n{b64}\r\n"
            "--b\r\nContent-Type: text/plain; charset=utf-8\r\n"
            "Content-Transfer-Encoding: quoted-printable\r\n\r\nna=C3=AFve in qp\r\n--b--\r\n"
        ).encode()
        body = _decoded_body(raw)
        self.assertIn("café in base64", body)
        self.assertIn("naïve in qp", body)
        self.assertIn("Subject: café", _decoded_headers(raw))

    def test_non_text_parts_are_skipped(self):
        raw = (
            b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain\r\n\r\nvisible\r\n"
            b"--b\r\nContent-Type: application/octet-stream\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            b"aW52aXNpYmxl\r\n--b--\r\n"
        )
        body = _decoded_body(raw)
        self.assertIn("visible", body)
        self.assertNotIn("invisible", body)
        self.assertNotIn("aW52aXNpYmxl", body)
