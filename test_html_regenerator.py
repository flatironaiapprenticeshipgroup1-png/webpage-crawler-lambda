from html_regenerator import _strip_code_fences


def test_fully_fenced_response_with_language_tag_is_unwrapped():
    text = '```html\n<div class="card">Hi</div>\n```'
    assert _strip_code_fences(text) == '<div class="card">Hi</div>'


def test_fully_fenced_response_without_language_tag_is_unwrapped():
    text = "```\n<p>Hi</p>\n```"
    assert _strip_code_fences(text) == "<p>Hi</p>"


def test_unfenced_response_is_unchanged():
    text = '<div class="card">Hi</div>'
    assert _strip_code_fences(text) == text


def test_truncated_response_with_only_a_leading_fence_has_it_stripped():
    text = '```html\n<div class="card">Hi, this got cut off mid'
    assert _strip_code_fences(text) == '<div class="card">Hi, this got cut off mid'


def test_leading_fence_with_surrounding_whitespace_is_stripped():
    text = '  \n```html\n<p>Hi</p>\n```  \n'
    assert _strip_code_fences(text) == "<p>Hi</p>"


def test_empty_and_none_input_is_returned_unchanged():
    assert _strip_code_fences("") == ""
    assert _strip_code_fences(None) is None
