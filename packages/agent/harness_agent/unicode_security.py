"""Unicode 安全工具：检测欺骗性文本、不可见字符和可疑 URL。

This module is intentionally lightweight so it can be imported in display and
approval paths without affecting startup performance.
"""

from __future__ import annotations

import ipaddress
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

_DANGEROUS_CODEPOINTS: frozenset[int] = frozenset(
    {
        # BiDi directional formatting controls (embeddings, overrides, pop)
        *range(0x202A, 0x202F),
        # BiDi isolate controls (isolates, pop isolate)
        *range(0x2066, 0x206A),
        # Zero-width and invisible formatting controls
        0x200B,  # ZERO WIDTH SPACE
        0x200C,  # ZERO WIDTH NON-JOINER
        0x200D,  # ZERO WIDTH JOINER
        0x200E,  # LEFT-TO-RIGHT MARK
        0x200F,  # RIGHT-TO-LEFT MARK
        0x2060,  # WORD JOINER
        0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
        # Other commonly abused invisible controls
        0x00AD,  # SOFT HYPHEN
        0x034F,  # COMBINING GRAPHEME JOINER
        0x115F,  # HANGUL CHOSEONG FILLER
        0x1160,  # HANGUL JUNGSEONG FILLER
    }
)
"""Code points that should be treated as deceptive/invisible for agent safety."""

_DANGEROUS_CHARACTERS: frozenset[str] = frozenset(
    chr(codepoint) for codepoint in _DANGEROUS_CODEPOINTS
)

# Minimal high-risk confusables for warn-level detection.
CONFUSABLES: dict[str, str] = {
    # Cyrillic
    "\u0430": "a",  # CYRILLIC SMALL LETTER A
    "\u0435": "e",  # CYRILLIC SMALL LETTER IE
    "\u043e": "o",  # CYRILLIC SMALL LETTER O
    "\u0440": "p",  # CYRILLIC SMALL LETTER ER
    "\u0441": "c",  # CYRILLIC SMALL LETTER ES
    "\u0443": "y",  # CYRILLIC SMALL LETTER U
    "\u0445": "x",  # CYRILLIC SMALL LETTER HA
    "\u043d": "h",  # CYRILLIC SMALL LETTER EN
    "\u0456": "i",  # CYRILLIC SMALL LETTER BYELORUSSIAN-UKRAINIAN I
    "\u0458": "j",  # CYRILLIC SMALL LETTER JE
    "\u043a": "k",  # CYRILLIC SMALL LETTER KA
    "\u0455": "s",  # CYRILLIC SMALL LETTER DZE
    # Greek
    "\u03b1": "a",  # GREEK SMALL LETTER ALPHA
    "\u03b5": "e",  # GREEK SMALL LETTER EPSILON
    "\u03bf": "o",  # GREEK SMALL LETTER OMICRON
    "\u03c1": "p",  # GREEK SMALL LETTER RHO
    "\u03c7": "x",  # GREEK SMALL LETTER CHI
    "\u03ba": "k",  # GREEK SMALL LETTER KAPPA
    "\u03bd": "v",  # GREEK SMALL LETTER NU
    "\u03c4": "t",  # GREEK SMALL LETTER TAU
    # Armenian
    "\u0570": "h",  # ARMENIAN SMALL LETTER HO
    "\u0578": "n",  # ARMENIAN SMALL LETTER VO
    "\u057d": "u",  # ARMENIAN SMALL LETTER SEH
    # Fullwidth Latin
    "\uff41": "a",  # FULLWIDTH LATIN SMALL LETTER A
    "\uff45": "e",  # FULLWIDTH LATIN SMALL LETTER E
    "\uff4f": "o",  # FULLWIDTH LATIN SMALL LETTER O
}

URL_ARG_KEYS: frozenset[str] = frozenset(
    {"url", "uri", "href", "link", "base_url", "endpoint"}
)
"""Argument key names that likely contain URLs and should be safety-checked."""

_URL_SAFE_LOCAL_HOSTS: frozenset[str] = frozenset({"localhost"})


@dataclass(frozen=True, slots=True)
class UnicodeIssue:
    """文本中发现的危险 Unicode 字符及其位置。

    Attributes:
        position: Zero-based index in the original string.
        character: The single raw character found in the input.
        codepoint: Uppercase code point string like `U+202E`.
        name: Unicode character name.
    """

    position: int
    character: str
    codepoint: str
    name: str

    def __post_init__(self) -> None:  # noqa: D105
        """校验字符、码点文本一致，防止调用方构造自相矛盾的安全结果。"""
        if len(self.character) != 1:
            msg = (
                "character must be a single code point, "
                f"got length {len(self.character)}"
            )
            raise ValueError(msg)
        expected = f"U+{ord(self.character):04X}"
        if self.codepoint != expected:
            msg = (
                f"codepoint {self.codepoint!r} does not match "
                f"character (expected {expected})"
            )
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class UrlSafetyResult:
    """URL 字符与域名混淆检测的不可变结果。

    A result may have `safe=True` with non-empty `warnings` when
    informational warnings (e.g. punycode decoding) are present without
    suspicious patterns.

    Attributes:
        safe: `True` if no suspicious patterns were found.
        decoded_domain: Punycode-decoded hostname when it differs from the
            original hostname.

            `None` when unchanged or no hostname exists.
        warnings: Human-readable warning strings (immutable).
        issues: Dangerous Unicode issues found in the full URL (immutable).
    """

    safe: bool
    decoded_domain: str | None
    warnings: tuple[str, ...]
    issues: tuple[UnicodeIssue, ...]


def detect_dangerous_unicode(text: str) -> list[UnicodeIssue]:
    """检测文本中可能影响展示或审计的隐藏 Unicode 码点。

    Args:
        text: Input text to inspect.

    Returns:
        A list of `UnicodeIssue` entries in source order.
    """
    issues: list[UnicodeIssue] = []
    for position, character in enumerate(text):
        if character not in _DANGEROUS_CHARACTERS:
            continue
        issues.append(
            UnicodeIssue(
                position=position,
                character=character,
                codepoint=_format_codepoint(character),
                name=_unicode_name(character),
            )
        )
    return issues


def strip_dangerous_unicode(text: str) -> str:
    """移除已知危险或不可见 Unicode 字符。

    Args:
        text: Input text to sanitize.

    Returns:
        Sanitized text with dangerous characters removed.
    """
    return "".join(ch for ch in text if ch not in _DANGEROUS_CHARACTERS)


def sanitize_control_chars(
    text: str,
    *,
    keep_newlines: bool = False,
    collapse_whitespace: bool = True,
    max_length: int | None = None,
) -> str:
    """中和不可信文本中的控制字符与欺骗性 Unicode。

    Untrusted strings (MCP server errors, config-file contents, tool output)
    can carry ANSI escape sequences, other control characters, or invisible
    Unicode that corrupts the terminal, breaks out of a layout, or injects fake
    lines into logs and prompts. This first removes the invisible/bidi code
    points flagged by `strip_dangerous_unicode`, then replaces every remaining
    Unicode "Other" (control/format) character with a space.

    Args:
        text: Untrusted text to sanitize.
        keep_newlines: When `True`, newlines survive so multiline, scrollable
            surfaces keep their line structure; otherwise newlines are
            flattened to spaces along with the other control characters.
        collapse_whitespace: When `True`, runs of whitespace are collapsed to a
            single space and surrounding whitespace is stripped. With
            `keep_newlines`, collapsing is applied per line so line breaks are
            preserved.
        max_length: When set, truncate to at most this many characters,
            replacing the final character with an ellipsis.

    Returns:
        Sanitized text safe to embed in terminal output, markup substitutions,
        logs, or prompts.
    """
    allowed = {" ", "\n"} if keep_newlines else {" "}
    cleaned = "".join(
        ch if ch in allowed or not unicodedata.category(ch).startswith("C") else " "
        for ch in strip_dangerous_unicode(text)
    )
    if collapse_whitespace:
        if keep_newlines:
            cleaned = "\n".join(" ".join(line.split()) for line in cleaned.split("\n"))
        else:
            cleaned = " ".join(cleaned.split())
    if max_length is not None and len(cleaned) > max_length:
        cleaned = cleaned[: max_length - 1].rstrip() + "…"
    return cleaned


def render_with_unicode_markers(text: str) -> str:
    """将隐藏 Unicode 字符渲染为可见码点标记。

    Example output: `abc<U+202E RIGHT-TO-LEFT OVERRIDE>def`.

    Args:
        text: Input text to render.

    Returns:
        Text where dangerous characters are replaced with visible markers.
    """
    rendered_parts: list[str] = []
    for character in text:
        if character not in _DANGEROUS_CHARACTERS:
            rendered_parts.append(character)
            continue
        rendered_parts.append(
            f"<{_format_codepoint(character)} {_unicode_name(character)}>"
        )
    return "".join(rendered_parts)


def summarize_issues(issues: list[UnicodeIssue], *, max_items: int = 3) -> str:
    """将多个 Unicode 问题压缩为适合警告栏展示的摘要。

    Deduplicates by code point. When more than *max_items* unique entries exist,
    the summary is truncated with a `+N more entries` suffix.

    Args:
        issues: A list of detected issues.
        max_items: Max unique code points to include in output.

    Returns:
        Comma-separated summary, e.g.
            `U+202E RIGHT-TO-LEFT OVERRIDE, U+200B ZERO WIDTH SPACE`.
    """
    unique_entries: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        entry = f"{issue.codepoint} {issue.name}"
        if entry in seen:
            continue
        seen.add(entry)
        unique_entries.append(entry)

    if len(unique_entries) <= max_items:
        return ", ".join(unique_entries)

    displayed = ", ".join(unique_entries[:max_items])
    remainder = len(unique_entries) - max_items
    suffix = "entry" if remainder == 1 else "entries"
    return f"{displayed}, +{remainder} more {suffix}"


def format_warning_detail(warnings: tuple[str, ...], *, max_shown: int = 2) -> str:
    """将安全警告拼接为带溢出提示的展示字符串。

    Args:
        warnings: Warning strings from a `UrlSafetyResult`.
        max_shown: Maximum warnings to include before truncating.

    Returns:
        Semicolon-separated detail string, e.g. `'warn1; warn2; +1 more'`.
    """
    shown = warnings[:max_shown]
    detail = "; ".join(shown)
    remaining = len(warnings) - max_shown
    if remaining > 0:
        detail += f"; +{remaining} more"
    return detail


def check_url_safety(url: str) -> UrlSafetyResult:
    """检查 URL 中的危险 Unicode、Punycode 与跨脚本域名混淆。

    Args:
        url: URL string to inspect.

    Returns:
        `UrlSafetyResult` including decoded domain and warning details.
    """
    warnings: list[str] = []
    suspicious = False

    # 先检查整条 URL，避免路径或查询参数中的双向控制符伪造终端展示。
    issues = detect_dangerous_unicode(url)
    if issues:
        suspicious = True
        warnings.append(
            f"URL contains hidden Unicode characters ({summarize_issues(issues)})"
        )

    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return UrlSafetyResult(
            safe=not suspicious,
            decoded_domain=None,
            warnings=tuple(warnings),
            issues=tuple(issues),
        )

    # Punycode 解码后再检查脚本混用，不能只相信 ASCII 外观的原始 hostname。
    decoded_hostname, failed_punycode = _decode_hostname(hostname)
    decoded_domain = decoded_hostname if decoded_hostname != hostname else None
    if decoded_domain:
        warnings.append(f"Punycode domain decodes to '{decoded_domain}'")
    if failed_punycode:
        suspicious = True
        labels = ", ".join(failed_punycode)
        warnings.append(f"Punycode label(s) could not be decoded: {labels}")

    if _is_local_or_ip_hostname(decoded_hostname):
        return UrlSafetyResult(
            safe=not suspicious,
            decoded_domain=decoded_domain,
            warnings=tuple(warnings),
            issues=tuple(issues),
        )

    # 仅对公网域名执行更严格的混淆检测；localhost/IP 属于开发环境常见输入。
    for label in _split_hostname_labels(decoded_hostname):
        scripts = _scripts_in_label(label)
        if len(scripts) > 1:
            suspicious = True
            script_names = ", ".join(sorted(scripts))
            warnings.append(f"Domain label '{label}' mixes scripts ({script_names})")

        if _label_has_suspicious_confusable_mix(label):
            suspicious = True
            warnings.append(
                f"Domain label '{label}' contains confusable Unicode characters"
            )

    return UrlSafetyResult(
        safe=not suspicious,
        decoded_domain=decoded_domain,
        warnings=tuple(warnings),
        issues=tuple(issues),
    )


def _decode_hostname(hostname: str) -> tuple[str, list[str]]:
    """尽可能将 ``xn--`` Punycode 标签解码为 Unicode 标签。

    Returns:
        Tuple of (decoded hostname, list of labels that failed to decode).
    """
    decoded_labels: list[str] = []
    failed_labels: list[str] = []
    for label in _split_hostname_labels(hostname):
        if label.startswith("xn--"):
            try:
                decoded_labels.append(label.encode("ascii").decode("idna"))
            except UnicodeError:
                decoded_labels.append(label)
                failed_labels.append(label)
            continue
        decoded_labels.append(label)
    return ".".join(decoded_labels), failed_labels


def _split_hostname_labels(hostname: str) -> list[str]:
    """将主机名分割为非空标签。

    Returns:
        Hostname labels without empty entries.
    """
    return [label for label in hostname.split(".") if label]


def _is_local_or_ip_hostname(hostname: str) -> bool:
    """判断主机名是否为 localhost 或 IP 字面量。

    Returns:
        `True` when hostname is localhost or an IP literal, else `False`.
    """
    host = hostname.strip().rstrip(".")
    if not host:
        return False

    if host.lower() in _URL_SAFE_LOCAL_HOSTS:
        return True

    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _scripts_in_label(label: str) -> set[str]:
    """收集域名标签使用的非常见 Unicode 字符脚本。

    Returns:
        Set of script names used by the label, excluding common/inherited.
    """
    scripts: set[str] = set()
    for character in label:
        script = _char_script(character)
        if script in {"Common", "Inherited"}:
            continue
        scripts.add(script)
    return scripts


def _label_has_suspicious_confusable_mix(label: str) -> bool:
    """判断域名标签是否存在高风险的跨脚本形近字混用。

    Only flags labels that mix multiple scripts while containing confusable
    characters. Single-script labels (even with confusables) are not flagged
    because they represent legitimate use of that script.

    Returns:
        `True` when the label mixes scripts and contains confusable characters.
    """
    if not any(character in CONFUSABLES for character in label):
        return False

    scripts = _scripts_in_label(label)
    return len(scripts) > 1


def _char_script(character: str) -> str:
    """将字符归类到粗粒度 Unicode 脚本桶。

    Returns:
        One of: `'Fullwidth'`, `'Latin'`, `'Cyrillic'`, `'Greek'`, `'Armenian'`,
            `'EastAsian'`, `'Inherited'`, `'Common'`, or `'Other'`.
    """
    name = unicodedata.name(character, "")
    category = unicodedata.category(character)

    if "FULLWIDTH LATIN" in name:
        return "Fullwidth"
    if "LATIN" in name:
        return "Latin"
    if "CYRILLIC" in name:
        return "Cyrillic"
    if "GREEK" in name:
        return "Greek"
    if "ARMENIAN" in name:
        return "Armenian"
    if any(
        token in name
        for token in (
            "CJK",
            "HIRAGANA",
            "KATAKANA",
            "HANGUL",
            "BOPOMOFO",
            "IDEOGRAPHIC",
        )
    ):
        return "EastAsian"

    if category.startswith("M"):
        return "Inherited"
    if category[0] in {"N", "P", "S", "Z", "C"}:
        return "Common"

    return "Other"


def _format_codepoint(character: str) -> str:
    """把字符码点格式化为大写 ``U+XXXX`` 形式。

    Returns:
        Uppercase `U+XXXX` codepoint string.
    """
    return f"U+{ord(character):04X}"


def _unicode_name(character: str) -> str:
    """返回稳定的 Unicode 名称，未知码点使用安全回退。

    Returns:
        Unicode name string for the character.
    """
    return unicodedata.name(character, "UNKNOWN CHARACTER")


# ---------------------------------------------------------------------------
# Shared helpers for recursive argument inspection
# ---------------------------------------------------------------------------


def iter_string_values(
    data: dict[str, Any],
    *,
    prefix: str = "",
) -> list[tuple[str, str]]:
    """将嵌套 dict/list 展开为键路径与字符串值对。

    Returns:
        List of `(path, value)` tuples for all string leaves.
    """
    values: list[tuple[str, str]] = []
    # 递归路径保留数组下标，调用方才能准确指出不安全参数的位置。
    for key, value in data.items():
        key_path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, str):
            values.append((key_path, value))
            continue
        if isinstance(value, dict):
            values.extend(iter_string_values(value, prefix=key_path))
            continue
        if isinstance(value, list):
            values.extend(_iter_string_values_from_list(value, prefix=key_path))
    return values


def _iter_string_values_from_list(
    values: list[Any],
    *,
    prefix: str,
) -> list[tuple[str, str]]:
    """将嵌套列表展开为键路径与字符串值对。

    Returns:
        List of `(path, value)` tuples for all string leaves.
    """
    entries: list[tuple[str, str]] = []
    for index, value in enumerate(values):
        key_path = f"{prefix}[{index}]"
        if isinstance(value, str):
            entries.append((key_path, value))
            continue
        if isinstance(value, dict):
            entries.extend(iter_string_values(value, prefix=key_path))
            continue
        if isinstance(value, list):
            entries.extend(_iter_string_values_from_list(value, prefix=key_path))
    return entries


def looks_like_url_key(arg_path: str) -> bool:
    """判断参数键路径是否暗示 URL 类型内容。

    Returns:
        `True` for URL-like key names, otherwise `False`.
    """
    key = arg_path.rsplit(".", maxsplit=1)[-1]
    key = key.split("[", maxsplit=1)[0].lower()
    return key in URL_ARG_KEYS
