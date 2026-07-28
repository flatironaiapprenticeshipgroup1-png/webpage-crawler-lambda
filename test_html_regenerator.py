from unittest.mock import MagicMock

from html_regenerator import _regenerate_chunk, _strip_code_fences


def _fake_openai_client(content="<p>ok</p>"):
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=content), finish_reason="stop")],
        usage=MagicMock(prompt_tokens=10, completion_tokens=20),
    )
    return client


def test_no_theme_does_not_leak_none_into_head_prompt():
    client = _fake_openai_client()
    _regenerate_chunk(client, "<title>Old</title>", "head", None, 0, 1)

    system_msg = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "None" not in system_msg, f"Head prompt leaked literal None: {system_msg}"
    assert "a clean, modern redesign using current web design best practices" in system_msg


def test_no_theme_does_not_leak_none_into_body_prompt():
    client = _fake_openai_client()
    _regenerate_chunk(client, "<p>Hi</p>", "body", "", 0, 1)

    system_msg = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "None" not in system_msg, f"Body prompt leaked literal None: {system_msg}"
    assert "a clean, modern redesign using current web design best practices" in system_msg


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
