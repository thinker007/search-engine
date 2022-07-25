"""Module containing utils to work with human language."""

import fasttext

_FASTTEXT_MODEL = fasttext.load_model("lid.176.bin")


def detect_lang(text: str) -> str:
    """Detect language of given text, returning ISO language code."""
    return _FASTTEXT_MODEL.predict(text)[0][0].replace("__label__", "")
