"""Parse Aruba MM global user table output."""

from __future__ import annotations

import re

from .models import ParseDecision, ParseResult, UserEntry

_MAC_PATTERNS = (
    re.compile(r"(?i)\b([0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b"),
    re.compile(r"(?i)\b[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}\b"),
)
_IPV4_PATTERN = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")


def normalize_mac(value: str) -> str:
    """Return aa:bb:cc:dd:ee:ff form, or an empty string when invalid."""
    if not isinstance(value, str):
        return ""
    try:
        text = value.strip().strip(",;()[]{}<>")
        hex_chars = re.sub(r"[^0-9A-Fa-f]", "", text)
        if len(hex_chars) != 12 or not re.fullmatch(r"(?i)[0-9a-f]{12}", hex_chars):
            return ""
        return ":".join(hex_chars[index : index + 2] for index in range(0, 12, 2)).lower()
    except Exception:
        return ""


def parse_global_user_table(output: str, *, role_filter: str = "profiling") -> list[UserEntry]:
    return parse_global_user_table_explained(output, role_filter=role_filter).entries


def parse_global_user_table_explained(output: str | None, *, role_filter: str = "profiling") -> ParseResult:
    """Extract target user MACs from `show global-user-table list role ...`.

    Aruba user-table output often contains other MAC-looking fields such as
    BSSID or AP details. This parser keeps only the first user MAC-like token
    from the identity columns near the start of each data row.
    """
    try:
        role = role_filter.strip().casefold() if isinstance(role_filter, str) else "profiling"
    except Exception:
        role = "profiling"
    if not isinstance(output, str):
        return ParseResult(entries=[], decisions=[ParseDecision(0, "ignored", "invalid_output")])
    entries: dict[str, UserEntry] = {}
    decisions: list[ParseDecision] = []
    type_spans: list[tuple[int, int]] = []
    try:
        lines = output.splitlines()
    except Exception:
        return ParseResult(entries=[], decisions=[ParseDecision(0, "ignored", "invalid_output")])
    for line_number, line in enumerate(lines, start=1):
        try:
            stripped = line.strip()
        except Exception:
            decisions.append(ParseDecision(line_number, "ignored", "invalid_line"))
            continue
        try:
            detected_type_spans = _type_column_spans(line)
        except Exception:
            decisions.append(ParseDecision(line_number, "ignored", "invalid_line"))
            continue
        if detected_type_spans:
            type_spans = detected_type_spans
            decisions.append(ParseDecision(line_number, "ignored", "header_or_command"))
            continue
        try:
            skip_reason = _skip_reason(stripped)
        except Exception:
            decisions.append(ParseDecision(line_number, "ignored", "invalid_line"))
            continue
        if skip_reason:
            if stripped:
                decisions.append(ParseDecision(line_number, "ignored", skip_reason))
            continue

        try:
            tokens = stripped.split()
        except Exception:
            decisions.append(ParseDecision(line_number, "ignored", "invalid_line"))
            continue
        try:
            mac_index, mac, mac_reason = _target_mac_from_tokens(tokens, role_filter=role)
        except Exception:
            decisions.append(ParseDecision(line_number, "ignored", "invalid_line"))
            continue
        try:
            user_type = _extract_type_value(line, type_spans)
        except Exception:
            user_type = ""
        try:
            type_na = user_type.strip().casefold() == "n/a"
        except Exception:
            type_na = False
        if not mac:
            decisions.append(ParseDecision(line_number, "ignored", mac_reason or "no_user_mac", user_type=user_type, type_na=type_na))
            continue
        try:
            role_matches, role_reason = _row_matches_role(tokens, role, mac_index)
        except Exception:
            decisions.append(
                ParseDecision(line_number, "ignored", "invalid_line", mac=mac, user_type=user_type, type_na=type_na)
            )
            continue
        if role and not role_matches:
            # The command should already filter by role, but this avoids deleting
            # unrelated rows if a device echoes a broader table.
            try:
                ignored_role = _probable_role_token(tokens, mac_index) or _extract_role(tokens, role)
            except Exception:
                ignored_role = role
            decisions.append(
                ParseDecision(
                    line_number,
                    "ignored",
                    role_reason,
                    mac=mac,
                    role=ignored_role,
                    user_type=user_type,
                    type_na=type_na,
                )
            )
            continue
        if mac in entries:
            try:
                duplicate_role = _extract_role(tokens, role)
            except Exception:
                duplicate_role = role
            decisions.append(
                ParseDecision(
                    line_number,
                    "ignored",
                    "duplicate_user_mac",
                    mac=mac,
                    role=duplicate_role,
                    user_type=user_type,
                    type_na=type_na,
                )
            )
            continue
        try:
            entry_role = _extract_role(tokens, role)
        except Exception:
            entry_role = role
        try:
            username = _extract_username(tokens, mac_index)
        except Exception:
            username = ""
        try:
            ip_address = _extract_ip(tokens)
        except Exception:
            ip_address = ""
        entry = UserEntry(
            mac=mac,
            role=entry_role,
            username=username,
            ip_address=ip_address,
            user_type=user_type,
            type_na=type_na,
        )
        entries[mac] = entry
        decisions.append(
            ParseDecision(
                line_number,
                "selected",
                mac_reason or role_reason or "selected",
                mac=mac,
                role=entry.role,
                user_type=user_type,
                type_na=type_na,
            )
        )
    return ParseResult(entries=list(entries.values()), decisions=decisions)


def _skip_reason(line: str) -> str:
    if not line:
        return "blank"
    lowered = line.casefold()
    if lowered.startswith(("show ", "global user", "users", "total", "----", "mac address", "ip address")):
        return "header_or_command"
    if set(line.replace(" ", "")) <= {"-"}:
        return "separator"
    if not any(pattern.search(line) for pattern in _MAC_PATTERNS):
        return "no_mac_token"
    return ""


def _target_mac_from_tokens(tokens: list[str], *, role_filter: str) -> tuple[int, str, str]:
    candidates = [(index, normalize_mac(token)) for index, token in enumerate(tokens)]
    candidates = [(index, mac) for index, mac in candidates if mac]
    if not candidates:
        return -1, "", "no_mac_token"

    role_index = _role_index(tokens, role_filter)
    if role_index >= 0:
        before_role = [(index, mac) for index, mac in candidates if index < role_index]
        identity_candidates = [(index, mac) for index, mac in before_role if index <= 3]
        if len(identity_candidates) == 1:
            index, mac = identity_candidates[0]
            return index, mac, "selected_identity_mac_before_role"
        if len(identity_candidates) > 1:
            return -1, "", "ambiguous_identity_macs_before_role"
        if len(before_role) == 1:
            index, mac = before_role[0]
            return index, mac, "selected_mac_before_role"
        if len(before_role) > 1:
            return -1, "", "ambiguous_macs_before_role"

    primary_identity_candidates = [(index, mac) for index, mac in candidates if index <= 2]
    if len(primary_identity_candidates) == 1:
        index, mac = primary_identity_candidates[0]
        return index, mac, "selected_identity_mac_without_role_column"
    if len(primary_identity_candidates) > 1:
        return -1, "", "ambiguous_identity_macs_without_role_column"
    fallback_identity_candidates = [(index, mac) for index, mac in candidates if index <= 3]
    if len(fallback_identity_candidates) == 1:
        index, mac = fallback_identity_candidates[0]
        return index, mac, "selected_identity_mac_without_role_column"
    if len(fallback_identity_candidates) > 1:
        return -1, "", "ambiguous_identity_macs_without_role_column"
    return -1, "", "mac_token_outside_identity_columns"


def _row_matches_role(tokens: list[str], role_filter: str, mac_index: int) -> tuple[bool, str]:
    if not role_filter:
        return True, "role_filter_empty"
    role_candidate = _probable_role_token(tokens, mac_index)
    if role_candidate:
        if role_candidate.casefold() == role_filter:
            return True, "probable_role_matches"
        return False, "role_mismatch"
    role_index = _role_index(tokens, role_filter)
    if role_index >= 0:
        return True, "role_column_matches"
    # Some filtered outputs omit a role column. In that shape, accept rows where
    # the user MAC appears in the usual identity columns.
    if 0 <= mac_index <= 3 and _extract_ip(tokens):
        if _looks_like_device_mac_noise(tokens, mac_index):
            return False, "device_mac_noise"
        return True, "accepted_filtered_output_without_role_column"
    return False, "role_not_found"


def _role_index(tokens: list[str], role_filter: str) -> int:
    if not role_filter:
        return -1
    for index, token in enumerate(tokens):
        try:
            token_value = token.strip().casefold()
        except Exception:
            continue
        if token_value == role_filter:
            return index
    return -1


def _extract_role(tokens: list[str], role_filter: str) -> str:
    index = _role_index(tokens, role_filter)
    if index >= 0:
        return tokens[index]
    return role_filter


def _probable_role_token(tokens: list[str], mac_index: int) -> str:
    candidate_index = mac_index + 2
    if candidate_index >= len(tokens):
        return ""
    if normalize_mac(tokens[candidate_index - 1]):
        return ""
    try:
        token = tokens[candidate_index].strip()
    except Exception:
        return ""
    try:
        if not token or token.isdigit() or normalize_mac(token) or _IPV4_PATTERN.fullmatch(token):
            return ""
    except Exception:
        return ""
    try:
        if token.casefold() in {"role", "vlan", "bssid", "ap", "essid"}:
            return ""
    except Exception:
        return ""
    return token


def _looks_like_device_mac_noise(tokens: list[str], mac_index: int) -> bool:
    try:
        trailing_tokens = tokens[mac_index + 1 : mac_index + 4]
    except Exception:
        return False
    device_markers = {"ap", "bssid", "essid", "monitor", "radio", "uplink"}
    for token in trailing_tokens:
        try:
            normalized = token.strip().casefold()
        except Exception:
            continue
        if normalized in device_markers:
            return True
    return False


def _extract_username(tokens: list[str], mac_index: int) -> str:
    for token in tokens[mac_index + 1 : mac_index + 5]:
        try:
            if _IPV4_PATTERN.fullmatch(token):
                continue
            if normalize_mac(token):
                continue
            if token.casefold() in {"role", "vlan"}:
                continue
        except Exception:
            continue
        return token
    return ""


def _extract_ip(tokens: list[str]) -> str:
    for token in tokens[:6]:
        try:
            if _IPV4_PATTERN.fullmatch(token):
                return token
        except Exception:
            continue
    return ""


def _type_column_spans(line: str) -> list[tuple[int, int]]:
    columns = _fixed_width_columns(line)
    spans: list[tuple[int, int]] = []
    for index, (label, start) in enumerate(columns):
        if label.strip().casefold() != "type":
            continue
        end = columns[index + 1][1] if index + 1 < len(columns) else len(line)
        spans.append((start, end))
    return spans


def _fixed_width_columns(line: str) -> list[tuple[str, int]]:
    columns: list[tuple[str, int]] = []
    for match in re.finditer(r"\S(?:.*?\S)?(?=\s{2,}|$)", line.rstrip()):
        columns.append((match.group(0).strip(), match.start()))
    return columns


def _extract_type_value(line: str, type_spans: list[tuple[int, int]]) -> str:
    for start, end in type_spans:
        if start >= len(line):
            continue
        value = line[start:end].strip()
        if value:
            return value
    return ""
