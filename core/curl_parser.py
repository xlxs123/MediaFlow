"""Parse Chrome's Copy as cURL without invoking a shell or reading files.

Bash quoting and CMD's caret/Windows-argv quoting are deliberately separate.
Only literal request URLs, HTTP headers, methods and request bytes leave this
module; curl transfer options are never passed to another executable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from urllib.parse import parse_qsl, quote_plus, unquote, urlsplit

from core.errors import MediaCheckError
from core.request_context import RequestContext


class CurlParseError(ValueError):
    """A safe, user-facing Chinese parse error with a stable reason code."""

    def __init__(self, message: str, code: str = "parse_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ParsedCurl:
    url: str | None
    urls: list[str] = field(default_factory=list)
    media_urls: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    shell_format: str = "bash"
    warnings: list[str] = field(default_factory=list)
    method: str = "GET"
    body: bytes | None = field(default=None, repr=False)

    @property
    def origin(self) -> str:
        return self.headers.get("origin", "")

    @property
    def referer(self) -> str:
        return self.headers.get("referer", "")

    @property
    def user_agent(self) -> str:
        return self.headers.get("user-agent", "")

    @property
    def query_parameters(self) -> list[tuple[str, str]]:
        # Return a fresh decoded view; the original URL is never reserialized.
        return parse_qsl(urlsplit(self.url).query, keep_blank_values=True) if self.url else []

    def to_context(self, url: str | None = None) -> RequestContext:
        selected = url or self.url
        if not selected:
            raise CurlParseError("发现多个媒体 URL，请先选择一个。", "multiple_urls")
        try:
            return RequestContext(selected, method=self.method, headers=self.headers, body=self.body)
        except MediaCheckError:
            raise CurlParseError("请求地址或请求头不合法，请检查完整 URL 与 Header。", "invalid_request") from None


_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_MEDIA_EXTENSION = re.compile(r"\.(m3u8|mpd|mp4)(?=$|[?#&;])", re.IGNORECASE)
_MEDIA_RANK = {"m3u8": 3, "mpd": 2, "mp4": 1}
_MEDIA_QUERY_KEYS = {
    "url", "uri", "src", "source", "file", "filename", "path", "playlist",
    "manifest", "video", "media", "play_url", "playurl", "download", "target",
}
_SIGNATURE_KEYS = {
    "sign", "signature", "sig", "token", "auth", "access_token", "auth_token",
    "expires", "expire", "expiry", "exp", "t", "timestamp", "auth_key",
    "authkey", "hdnts", "hdnea", "jwt", "policy", "key-pair-id",
    "x-amz-signature", "x-amz-expires", "x-goog-signature", "x-goog-expires",
}


def _error_quotes() -> CurlParseError:
    return CurlParseError("cURL 引号可能不完整，请重新从 Chrome DevTools 复制。", "incomplete_quotes")


def _error_shell() -> CurlParseError:
    return CurlParseError(
        "发现 Shell 操作符或多条命令。请只复制 Chrome 中单条请求的 Copy as cURL。",
        "shell_syntax",
    )


def _error_expansion() -> CurlParseError:
    return CurlParseError(
        "cURL 包含变量展开或命令替换，无法安全还原。请重新从 Chrome DevTools 复制完整请求。",
        "shell_expansion",
    )


def _newline_end(text: str, index: int) -> int | None:
    if index >= len(text) or text[index] not in "\r\n":
        return None
    if text[index:index + 2] == "\r\n":
        return index + 2
    return index + 1


def _detect_shell_format(text: str) -> str:
    """Recognize actual CMD caret syntax, ignoring literal Bash single quotes."""
    quote: str | None = None
    index = 0
    saw_single_quote = False
    while index < len(text):
        char = text[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\":
            # Bash escapes can contain a caret without indicating CMD syntax.
            index += 2
            continue
        if char == "'" and quote is None:
            saw_single_quote = True
            quote = "'"
        elif char == '"':
            quote = None if quote == '"' else '"'
        elif char == "^" and index + 1 < len(text):
            following = text[index + 1]
            if quote is None and (following == '"' or following in "\r\n"):
                return "cmd"
            if quote == '"' and (following in "\\%" or text[index:index + 2] == "^^"):
                return "cmd"
        index += 1
    if not saw_single_quote and re.match(r"(?i)^curl\.exe(?:\s|$)", text):
        return "cmd"
    return "bash"


def _starts_bash_expansion(text: str, index: int) -> bool:
    if text[index] != "$" or index + 1 >= len(text):
        return False
    following = text[index + 1]
    return following.isalnum() or following == "_" or following in "{($!?-*@#\""


def _ansi_escape(text: str, index: int) -> tuple[str, int]:
    """Decode Bash $'...' escapes directly; never use eval or a shell."""
    if index + 1 >= len(text):
        raise _error_quotes()
    char = text[index + 1]
    simple = {
        "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f",
        "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\",
        "'": "'", '"': '"',
    }
    if char in simple:
        return simple[char], index + 2
    if char in "xuU":
        length = 2 if char == "x" else (4 if char == "u" else 8)
        match = re.match(r"[0-9a-fA-F]{1," + str(length) + "}", text[index + 2:])
        if match is None:
            raise CurlParseError("Bash ANSI-C 引号中存在不完整的字符转义。", "invalid_escape")
        number = int(match.group(), 16)
        if number > 0x10FFFF or 0xD800 <= number <= 0xDFFF:
            raise CurlParseError("Bash ANSI-C 引号中存在无效的 Unicode 字符。", "invalid_escape")
        # \xNN and octal are byte escapes, unlike \u/\U Unicode escapes.
        # Surrogateescape carries their high bytes through the string tokenizer
        # so --data-binary can reconstruct 0xFF rather than UTF-8 C3 BF.
        decoded = chr(0xDC00 + number) if char == "x" and number >= 128 else chr(number)
        return decoded, index + 2 + len(match.group())
    if char in "01234567":
        match = re.match(r"[0-7]{1,3}", text[index + 1:])
        assert match is not None
        number = int(match.group(), 8) & 0xFF
        decoded = chr(0xDC00 + number) if number >= 128 else chr(number)
        return decoded, index + 1 + len(match.group())
    if char == "c":
        if index + 2 >= len(text) or text[index + 2] == "'":
            raise CurlParseError("Bash ANSI-C 控制字符转义不完整。", "invalid_escape")
        return chr(ord(text[index + 2].upper()) & 0x1F), index + 3
    newline_end = _newline_end(text, index + 1)
    if newline_end is not None:
        return "", newline_end
    # Bash retains the backslash for unknown ANSI-C escapes.
    return "\\" + char, index + 2


def _tokenize_bash(text: str) -> list[str]:
    tokens: list[str] = []
    parts: list[str] = []
    state = "plain"
    started = False
    index = 0
    while index < len(text):
        char = text[index]
        if state == "single":
            if char == "'":
                state = "plain"
            else:
                parts.append(char)
            index += 1
            continue
        if state == "ansi":
            if char == "'":
                state = "plain"
                index += 1
            elif char == "\\":
                decoded, index = _ansi_escape(text, index)
                parts.append(decoded)
            else:
                parts.append(char)
                index += 1
            continue
        if state == "double":
            if char == '"':
                state = "plain"
                index += 1
            elif char == "\\":
                if index + 1 >= len(text):
                    raise _error_quotes()
                newline_end = _newline_end(text, index + 1)
                if newline_end is not None:
                    index = newline_end
                elif text[index + 1] in '$`"\\':
                    parts.append(text[index + 1])
                    index += 2
                else:
                    parts.append("\\")
                    index += 1
            elif char == "`" or _starts_bash_expansion(text, index):
                raise _error_expansion()
            else:
                parts.append(char)
                index += 1
            continue

        if char in "\r\n":
            raise _error_shell()
        if char.isspace():
            if started:
                tokens.append("".join(parts))
                parts = []
                started = False
            index += 1
        elif char == "\\":
            if index + 1 >= len(text):
                raise CurlParseError("Bash 续行或反斜杠转义不完整，请重新复制 cURL。", "invalid_escape")
            newline_end = _newline_end(text, index + 1)
            if newline_end is not None:
                index = newline_end
            else:
                started = True
                parts.append(text[index + 1])
                index += 2
        elif char in "'\"":
            started = True
            state = "single" if char == "'" else "double"
            index += 1
        elif char == "$" and text[index:index + 2] == "$'":
            started = True
            state = "ansi"
            index += 2
        elif char == "`" or _starts_bash_expansion(text, index):
            raise _error_expansion()
        elif char in "&|;<>()" or (char == "#" and not started):
            raise _error_shell()
        else:
            started = True
            parts.append(char)
            index += 1
    if state != "plain":
        raise _error_quotes()
    if started:
        tokens.append("".join(parts))
    return tokens


def _cmd_characters(text: str) -> list[tuple[str, bool]]:
    """Undo caret escapes, retaining provenance for literal operators and %."""
    characters: list[tuple[str, bool]] = []
    index = 0
    native_quote = False
    while index < len(text):
        char = text[index]
        if char == "^" and not native_quote:
            if index + 1 >= len(text):
                raise CurlParseError("CMD 的 ^ 转义或续行不完整，请重新复制 cURL。", "invalid_escape")
            newline_end = _newline_end(text, index + 1)
            if newline_end is not None:
                index = newline_end
                continue
            characters.append((text[index + 1], True))
            index += 2
            continue
        if char == '"':
            # Native CMD quotes suppress caret processing; Chrome's ^" wrappers
            # were consumed above and intentionally do not toggle this state.
            native_quote = not native_quote
        characters.append((char, False))
        index += 1
    return characters


def _tokenize_cmd(text: str) -> list[str]:
    characters = _cmd_characters(text)
    tokens: list[str] = []
    parts: list[str] = []
    quoted = False
    chrome_quote = False
    started = False
    index = 0
    while index < len(characters):
        char, escaped = characters[index]
        if char == "\\":
            end = index
            while end < len(characters) and characters[end][0] == "\\":
                end += 1
            count = end - index
            if end < len(characters) and characters[end][0] == '"':
                parts.append("\\" * (count // 2))
                if count % 2:
                    parts.append('"')
                else:
                    quoted = not quoted
                    chrome_quote = characters[end][1] if quoted else False
                started = True
                index = end + 1
            else:
                # DevTools first doubles *every* original backslash and then
                # caret-escapes it. Restore those pairs even before nonquotes.
                if quoted and chrome_quote and all(item[1] for item in characters[index:end]):
                    count = (count + 1) // 2
                parts.append("\\" * count)
                started = True
                index = end
            continue
        if char == '"':
            quoted = not quoted
            chrome_quote = escaped if quoted else False
            started = True
            index += 1
            continue
        if char in "\r\n" and not quoted:
            raise _error_shell()
        if char.isspace() and not quoted and not escaped:
            if started:
                tokens.append("".join(parts))
                parts = []
                started = False
            index += 1
            continue
        if char in "&|<>()" and not quoted and not escaped:
            raise _error_shell()
        if char in "%!" and not escaped:
            raw_tail = "".join(item[0] for item in characters[index:])
            pattern = r"%[A-Za-z_][A-Za-z0-9_]*%" if char == "%" else r"![A-Za-z_][A-Za-z0-9_]*!"
            match = re.match(pattern, raw_tail)
            if match and not any(item[1] for item in characters[index:index + len(match.group())]):
                raise _error_expansion()
        started = True
        parts.append(char)
        index += 1
    if quoted:
        raise _error_quotes()
    if started:
        tokens.append("".join(parts))
    return tokens


def _validated_url(value: str, shell_format: str) -> str:
    # These are cURL's literal-URL glob escapes emitted by Chrome, not URL
    # percent escapes. No query parsing/re-serialization is performed here.
    value = re.sub(r"\\([\[\]{}])", r"\1", value)
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
        parsed.port  # Validate a malformed explicit port without changing text.
    except ValueError:
        valid = False
    if not valid or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        if shell_format == "cmd":
            raise CurlParseError("检测到 CMD 转义格式，但 URL 还原失败。请重新复制完整 cURL。", "invalid_url")
        raise CurlParseError("未检测到有效的 HTTP/HTTPS URL，请重新复制完整 cURL。", "invalid_url")
    return value


def _decode_for_classification(value: str) -> str:
    for _ in range(3):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return value


def _media_priority(url: str) -> int:
    parsed = urlsplit(url)
    candidates = [_decode_for_classification(parsed.path)]
    for name, value in parse_qsl(parsed.query, keep_blank_values=True):
        decoded = _decode_for_classification(value)
        if name.lower() in _MEDIA_QUERY_KEYS or decoded.lower().startswith(("http://", "https://", "/")):
            candidates.append(decoded)
    ranks = [
        _MEDIA_RANK[match.group(1).lower()]
        for candidate in candidates
        for match in _MEDIA_EXTENSION.finditer(candidate)
    ]
    return max(ranks, default=0)


def _store_header(headers: dict[str, str], name: str, value: str, warnings: list[str]) -> None:
    name = name.strip().lower()
    value = value.strip(" \t")
    if not _HEADER_NAME.fullmatch(name):
        raise CurlParseError("请求头名称无效，请检查 -H 中的 Header Name。", "invalid_header")
    if any(ord(char) < 32 and char != "\t" or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise CurlParseError("请求头包含换行或控制字符，不能作为 HTTP 请求头使用。", "invalid_header")
    if not value:
        headers.pop(name, None)
        warning = "空值请求头不会发送。"
        if warning not in warnings:
            warnings.append(warning)
        return
    headers[name] = value


def _parse_header(value: str, headers: dict[str, str], warnings: list[str]) -> None:
    if value.startswith("@"):
        raise CurlParseError("不读取 cURL 的请求头文件，请提供 Chrome 复制的内联请求头。", "header_file")
    if ":" in value:
        name, content = value.split(":", 1)
    elif value.endswith(";"):
        name, content = value[:-1], ""
    else:
        raise CurlParseError("请求头格式不完整，应为 -H '名称: 值'。", "invalid_header")
    _store_header(headers, name, content, warnings)


_FLAGS = {
    "--compressed", "--location", "--globoff", "--silent", "--show-error",
    "--fail", "--fail-with-body", "--include", "--http1.0", "--http1.1",
    "--http2", "--http2-prior-knowledge", "--http3", "--http3-only",
    "--no-buffer", "--path-as-is", "--ipv4", "--ipv6", "--create-dirs",
    "--remote-name", "--remote-header-name", "--no-progress-meter",
}
_SHORT_FLAGS = {"s", "S", "L", "f", "i", "N", "g", "4", "6", "O"}
_IGNORED_VALUE_OPTIONS = {
    "--output", "--output-dir", "--retry", "--retry-delay", "--retry-max-time",
    "--connect-timeout", "--max-time", "--limit-rate", "--write-out",
    "--stderr", "--max-redirs", "--speed-limit", "--speed-time",
}
_BODY_OPTIONS = {
    "--data": "data", "--data-ascii": "data", "--data-raw": "raw",
    "--data-binary": "binary", "--data-urlencode": "urlencode", "--json": "json",
}
_UPLOAD_OPTIONS = {"--form", "--form-string", "--upload-file"}
_HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
_VALUE_OPTIONS = {
    "--url": "url", "--header": "header", "--cookie": "cookie",
    "--referer": "referer", "--user-agent": "user-agent", "--request": "request",
}
_SHORT_VALUE_OPTIONS = {
    "H": "header", "b": "cookie", "e": "referer", "A": "user-agent",
    "X": "request", "o": "ignore", "m": "ignore", "w": "ignore", "d": "data",
}


def _literal_body(value: str, kind: str) -> bytes:
    if kind not in {"raw", "urlencode"} and value.startswith("@"):
        raise CurlParseError("不读取 cURL 的正文文件或 stdin；请提供内联请求正文。", "body_file")
    if "\x00" in value:
        raise CurlParseError("请求正文包含命令参数无法表示的空字符，请重新复制。", "invalid_character")
    try:
        if kind == "urlencode":
            # curl checks '=' before '@'. Only the content is escaped; a
            # supplied name is expected to already be encoded. RFC1866 spaces
            # use '+', whereas an actual '+' in the content becomes '%2B'.
            if "=" in value:
                name, content = value.split("=", 1)
                value = (name + "=" if name else "") + quote_plus(content, safe="", encoding="utf-8", errors="surrogateescape")
            elif "@" in value:
                raise CurlParseError("--data-urlencode 指向正文文件；请提供内联字段值。", "body_file")
            else:
                value = quote_plus(value, safe="", encoding="utf-8", errors="surrogateescape")
        # Literal argv values are preserved. curl strips CR/LF only when it
        # reads a --data @file, and this parser never reads those files.
        return value.encode("utf-8", errors="surrogateescape")
    except UnicodeEncodeError:
        raise CurlParseError("请求正文包含无效 Unicode 字符，请重新复制。", "invalid_character") from None


def _append_get_query(url: str, body: bytes) -> str:
    if not body:
        return url
    # Add the supplied data before the fragment. In particular, preserve the
    # existing query's escapes, '+' symbols, order and empty fields byte-for-byte.
    before_fragment, marker, fragment = url.partition("#")
    _, query_marker, existing_query = before_fragment.partition("?")
    if not query_marker:
        separator = "?"
    else:
        separator = "" if not existing_query or existing_query.endswith("&") else "&"
    return before_fragment + separator + body.decode("utf-8", errors="surrogateescape") + (marker + fragment if marker else "")


def parse_curl(text: str) -> ParsedCurl:
    """Extract a literal HTTP request from one cURL command.

    It does not persist cookies, access the clipboard, read @files, expand
    variables, or execute commands. Same-priority media URLs remain ambiguous
    for the GUI to ask the user to select.
    """
    if not isinstance(text, str) or not text.strip():
        raise CurlParseError("未检测到有效 cURL，请先在 Chrome DevTools 中复制请求。", "not_curl")
    if len(text) > 2_000_000:
        raise CurlParseError("cURL 文本过长，请只复制单条媒体请求。", "input_too_large")
    if "\x00" in text:
        raise CurlParseError("cURL 包含无效的空字符，请重新复制。", "invalid_character")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in text):
        raise CurlParseError("cURL 文本包含无效 Unicode 字符，请重新复制。", "invalid_character")
    text = text.strip().removeprefix("\ufeff").strip()
    # Classify a normal URL/other clipboard text before looking for '&', '$',
    # or other syntax that only has meaning in an actual cURL command.
    command_prefix = re.match(
        r'''(?ix)^(?:
            curl(?:\.exe)?(?=\s|$)
            | ['"](?:curl(?:\.exe)?|[^'"]*[/\\]curl(?:\.exe)?)['"](?=\s|$)
            | [^\s'"]*[/\\]curl(?:\.exe)?(?=\s|$)
        )''',
        text,
    )
    if command_prefix is None:
        raise CurlParseError("未检测到有效 cURL，请使用 Chrome 的 Copy as cURL。", "not_curl")
    shell_format = _detect_shell_format(text)
    tokens = _tokenize_cmd(text) if shell_format == "cmd" else _tokenize_bash(text)
    executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower() if tokens else ""
    if executable not in {"curl", "curl.exe"}:
        raise CurlParseError("未检测到有效 cURL，请使用 Chrome 的 Copy as cURL。", "not_curl")

    headers: dict[str, str] = {}
    urls: list[str] = []
    warnings: list[str] = []
    explicit_headers: set[str] = set()
    explicit_method: str | None = None
    body: bytes | None = None
    use_get = False
    use_head = False
    use_json = False
    index = 1
    positional_only = False
    while index < len(tokens):
        token = tokens[index]
        index += 1
        kind: str | None = None
        value: str | None = None
        if token == "--" and not positional_only:
            positional_only = True
            continue
        if not positional_only and token.startswith("-"):
            option, separator, attached = token.partition("=")
            if option in _UPLOAD_OPTIONS or (not token.startswith("--") and token[1:2] in {"F", "T"}):
                raise CurlParseError(
                    "不支持 cURL multipart 表单或文件上传；请提供内联 data/raw/binary/json 正文。",
                    "request_body",
                )
            if option in {"--config", "--cookie-jar"} or token.startswith(("-K", "-c")):
                raise CurlParseError("不读取或写入 cURL 配置/Cookie 文件，请复制内联请求头。", "external_file")
            if option in _VALUE_OPTIONS:
                kind = _VALUE_OPTIONS[option]
                value = attached if separator else None
            elif option in _BODY_OPTIONS:
                kind = _BODY_OPTIONS[option]
                value = attached if separator else None
            elif option in _IGNORED_VALUE_OPTIONS:
                kind = "ignore"
                value = attached if separator else None
            elif option in {"--head", "--get", "--no-head", "--no-get"}:
                if separator:
                    raise CurlParseError("cURL 开关选项后出现了不支持的值。", "unsupported_option")
                if option == "--head":
                    use_head = True
                elif option == "--get":
                    use_get = True
                elif option == "--no-head":
                    use_head = False
                else:
                    use_get = False
                continue
            elif option == "--insecure":
                warnings.append("cURL 的 --insecure 未应用；下载仍会验证 HTTPS 证书。")
                continue
            elif token in _FLAGS:
                continue
            elif not token.startswith("--") and len(token) > 1 and token[1] in _SHORT_VALUE_OPTIONS:
                kind = _SHORT_VALUE_OPTIONS[token[1]]
                value = token[2:] if len(token) > 2 else None
            elif not token.startswith("--") and len(token) > 1:
                # A short flag cluster may end in one value-taking option,
                # e.g. -sSLGdname=value. Its remaining bytes are that value.
                for position, char in enumerate(token[1:], 1):
                    if char in {"F", "T"}:
                        raise CurlParseError("不支持 cURL multipart 表单或文件上传；请提供内联正文。", "request_body")
                    if char in {"K", "c"}:
                        raise CurlParseError("不读取或写入 cURL 配置/Cookie 文件，请复制内联请求头。", "external_file")
                    if char in _SHORT_VALUE_OPTIONS:
                        kind = _SHORT_VALUE_OPTIONS[char]
                        value = token[position + 1:] or None
                        break
                    if char not in _SHORT_FLAGS | {"I", "G", "k"}:
                        raise CurlParseError("cURL 含有未支持的短选项，请重新复制完整请求。", "unsupported_option")
                    if char == "I":
                        use_head = True
                    elif char == "G":
                        use_get = True
                    elif char == "k":
                        warnings.append("cURL 的 --insecure 未应用；下载仍会验证 HTTPS 证书。")
                if kind is None:
                    continue
            else:
                raise CurlParseError(
                    "cURL 含有未支持的选项。请重新复制 Chrome 的单条媒体请求，或使用普通 URL 模式。",
                    "unsupported_option",
                )
            if value is None:
                if index >= len(tokens) or (tokens[index].startswith("-") and kind not in _BODY_OPTIONS.values()):
                    raise CurlParseError("cURL 的 URL、请求头或选项值不完整，请重新复制。", "missing_option_value")
                value = tokens[index]
                index += 1
        else:
            kind, value = "url", token

        assert value is not None
        if kind == "url":
            url = _validated_url(value, shell_format)
            if url not in urls:
                urls.append(url)
        elif kind == "header":
            _parse_header(value, headers, warnings)
            explicit_headers.add(value.split(":", 1)[0].strip().removesuffix(";").lower())
        elif kind == "cookie":
            if value and (value.startswith("@") or "=" not in value):
                raise CurlParseError("-b/--cookie 指向 Cookie 文件；请使用 Chrome 复制的内联 Cookie。", "cookie_file")
            _store_header(headers, "cookie", value, warnings)
        elif kind == "referer":
            if value.lower().endswith(";auto"):
                raise CurlParseError("不支持 cURL 自动 Referer，请提供浏览器实际发送的 Referer 请求头。", "automatic_referer")
            _store_header(headers, "referer", value, warnings)
        elif kind == "user-agent":
            _store_header(headers, "user-agent", value, warnings)
        elif kind == "request":
            method = value.upper()
            if not re.fullmatch(r"[A-Z]+", method) or method not in _HTTP_METHODS:
                raise CurlParseError("HTTP Method 不正确；请使用 GET、POST、HEAD 等常规方法。", "unsupported_method")
            explicit_method = method
        elif kind in _BODY_OPTIONS.values():
            chunk = _literal_body(value, kind)
            if body is None:
                body = chunk
            else:
                body += (b"&" if body and kind != "json" else b"") + chunk
            use_json = use_json or kind == "json"

    if not urls:
        raise CurlParseError("检测到 cURL，但未检测到 URL。请重新复制完整请求。", "no_url")
    if use_head and body is not None and not use_get:
        raise CurlParseError("cURL --head 与正文选项不兼容；请复制浏览器实际请求，或使用 -G 将数据放入查询。", "incompatible_options")
    method = explicit_method or ("HEAD" if use_head else "GET" if use_get else "POST" if body is not None else "GET")
    if use_get and body is not None:
        urls = [_validated_url(_append_get_query(url, body), shell_format) for url in urls]
        body = None
    if use_json:
        defaults = {"content-type": "application/json", "accept": "application/json"}
    elif body is not None:
        defaults = {"content-type": "application/x-www-form-urlencoded"}
    else:
        defaults = {}
    for name, value in defaults.items():
        if name not in explicit_headers:
            headers.setdefault(name, value)
    if method == "HEAD":
        warnings.append("原请求为 HEAD，下载时会使用 GET 获取媒体。")
    priorities = [(url, _media_priority(url)) for url in urls]
    best_rank = max(rank for _, rank in priorities)
    media_urls = [url for url, rank in priorities if rank == best_rank] if best_rank else list(urls)
    if not best_rank:
        warnings.append("地址没有已知媒体后缀，将依据服务器实际返回内容判断媒体类型。")
    if len(media_urls) > 1:
        warnings.append("发现多个媒体 URL，请选择一个。")
    return ParsedCurl(
        url=media_urls[0] if len(media_urls) == 1 else None,
        urls=urls,
        media_urls=media_urls,
        headers=headers,
        shell_format=shell_format,
        warnings=list(dict.fromkeys(warnings)),
        method=method,
        body=body,
    )


def parse_input(text: str) -> RequestContext:
    """Accept one literal HTTP URL or a cURL request with one selected URL."""
    if not isinstance(text, str) or not text.strip():
        raise CurlParseError("输入为空，请粘贴完整 URL 或 Chrome Copy as cURL。", "empty_input")
    if len(text) > 2_000_000:
        raise CurlParseError("输入文本过长，请只粘贴单条媒体请求。", "input_too_large")
    candidate = text.strip().removeprefix("\ufeff").strip()
    if candidate[:1] in {"'", '"'} and candidate[-1:] == candidate[:1]:
        candidate = candidate[1:-1]
    if re.match(r"(?i)^https?://", candidate):
        if any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in candidate):
            raise CurlParseError("URL 包含控制字符，请重新复制完整地址。", "invalid_url")
        try:
            # Raw URL mode never treats '&' or literal '$' as shell syntax.
            return RequestContext(candidate)
        except MediaCheckError:
            raise CurlParseError("URL 不合法，请粘贴完整 HTTP/HTTPS 地址。", "invalid_url") from None
    return parse_curl(text).to_context()


def has_temporary_signature(url: str) -> bool:
    """Inspect parameter *names* without rewriting or synthesizing a URL."""
    try:
        query = urlsplit(url).query
        return any(_decode_for_classification(name).lower() in _SIGNATURE_KEYS for name, _ in parse_qsl(query, keep_blank_values=True))
    except (TypeError, ValueError):
        return False
