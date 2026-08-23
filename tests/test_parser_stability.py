"""Tests for parser stability fixes."""

from aruba_mm_cleanup.parser import parse_global_user_table_explained


class BadStr(str):
    """A string subclass that raises RuntimeError on splitlines()."""

    def splitlines(self, *args, **kwargs):
        raise RuntimeError("bad splitlines")


class BadLine(str):
    """A string subclass whose strip() raises RuntimeError."""

    def strip(self, *args, **kwargs):
        raise RuntimeError("bad strip")


class BadCasefoldLine(str):
    """A string subclass whose casefold() raises RuntimeError."""

    def casefold(self, *args, **kwargs):
        raise RuntimeError("bad casefold")

    def strip(self, *args, **kwargs):
        return self


class BadRstripLine(str):
    """A string subclass whose rstrip() raises RuntimeError."""

    def rstrip(self, *args, **kwargs):
        raise RuntimeError("bad rstrip")

    def strip(self, *args, **kwargs):
        return self


class BadSplitLine(str):
    """A string subclass whose strip() returns self and split() raises RuntimeError."""

    def strip(self, *args, **kwargs):
        return self

    def split(self, *args, **kwargs):
        raise RuntimeError("bad split")


class BadTypeSliceLine(str):
    """A string subclass whose fixed-width type slice raises RuntimeError."""

    def __getitem__(self, key):
        if isinstance(key, slice):
            raise RuntimeError("bad type slice")
        return super().__getitem__(key)


class BadToken(str):
    """A token whose strip() raises RuntimeError."""

    def strip(self, *args, **kwargs):
        raise RuntimeError("bad token strip")


class BadCasefoldToken(str):
    """A token whose casefold() raises RuntimeError."""

    def strip(self, *args, **kwargs):
        return self

    def casefold(self, *args, **kwargs):
        raise RuntimeError("bad token casefold")


class NonStringToken:
    """A token that cannot be passed to regex string matchers."""


def test_parse_global_user_table_explained_handles_bad_splitlines():
    """Test that parse_global_user_table_explained handles bad splitlines gracefully."""
    output = BadStr("some content")

    result = parse_global_user_table_explained(output, role_filter="profiling")

    assert result.entries == []
    assert len(result.decisions) == 1
    assert result.decisions[0].action == "ignored"
    assert result.decisions[0].reason == "invalid_output"


def test_parse_global_user_table_explained_handles_bad_line_strip():
    """Test that parse_global_user_table_explained handles bad line strip gracefully."""
    class BadOutput(str):
        def splitlines(self, *args, **kwargs):
            return [BadLine("some content")]

    output = BadOutput("some content")

    result = parse_global_user_table_explained(output, role_filter="profiling")

    assert result.entries == []
    assert len(result.decisions) == 1
    assert result.decisions[0].action == "ignored"
    assert result.decisions[0].reason == "invalid_line"


def test_parse_global_user_table_explained_handles_bad_type_slice():
    """Test that a broken Type column extraction does not drop an otherwise valid MAC row."""
    header = f"{'IP':<16}{'MAC Address':<21}{'User':<14}{'Role':<12}{'Type':<8}{'BSSID'}"
    row = BadTypeSliceLine(
        f"{'192.0.2.10':<16}{'aa:bb:cc:00:00:01':<21}{'user-a':<14}{'profiling':<12}{'N/A':<8}{'11:22:33:44:55:66'}"
    )

    class BadOutput(str):
        def splitlines(self, *args, **kwargs):
            return [header, row]

    result = parse_global_user_table_explained(BadOutput(""), role_filter="profiling")

    assert [entry.mac for entry in result.entries] == ["aa:bb:cc:00:00:01"]
    assert result.entries[0].user_type == ""
    assert result.entries[0].type_na is False
    assert result.decisions[-1].action == "selected"


def test_parse_global_user_table_explained_handles_bad_line_rstrip():
    """Test that parse_global_user_table_explained handles bad line rstrip gracefully."""
    class BadOutput(str):
        def splitlines(self, *args, **kwargs):
            return [BadRstripLine("some content")]

    output = BadOutput("some content")

    result = parse_global_user_table_explained(output, role_filter="profiling")

    assert result.entries == []
    assert len(result.decisions) == 1
    assert result.decisions[0].action == "ignored"
    assert result.decisions[0].reason == "invalid_line"


def test_parse_global_user_table_explained_handles_bad_line_casefold():
    """Test that parse_global_user_table_explained handles bad line casefold gracefully."""
    class BadOutput(str):
        def splitlines(self, *args, **kwargs):
            return [BadCasefoldLine("some content")]

    output = BadOutput("some content")

    result = parse_global_user_table_explained(output, role_filter="profiling")

    assert result.entries == []
    assert len(result.decisions) == 1
    assert result.decisions[0].action == "ignored"
    assert result.decisions[0].reason == "invalid_line"


def test_parse_global_user_table_explained_handles_bad_line_split():
    """Test that parse_global_user_table_explained handles bad line split gracefully."""
    class BadOutput(str):
        def splitlines(self, *args, **kwargs):
            return [BadSplitLine("00:11:22:33:44:55 some content")]

    output = BadOutput("00:11:22:33:44:55 some content")

    result = parse_global_user_table_explained(output, role_filter="profiling")

    assert result.entries == []
    assert len(result.decisions) == 1
    assert result.decisions[0].action == "ignored"
    assert result.decisions[0].reason == "invalid_line"


def test_parse_global_user_table_explained_handles_bad_token_container():
    """Test that a bad token container does not abort parsing."""
    class BadTokens(list):
        def __iter__(self):
            raise RuntimeError("bad token iterator")

    class BadTokenLine(str):
        def strip(self, *args, **kwargs):
            return self

        def split(self, *args, **kwargs):
            return BadTokens(["192.0.2.10", "aa:bb:cc:00:00:01", "user-a", "profiling"])

    class BadOutput(str):
        def splitlines(self, *args, **kwargs):
            return [BadTokenLine("192.0.2.10 aa:bb:cc:00:00:01 user-a profiling")]

    result = parse_global_user_table_explained(BadOutput(""), role_filter="profiling")

    assert result.entries == []
    assert len(result.decisions) == 1
    assert result.decisions[0].action == "ignored"
    assert result.decisions[0].reason == "invalid_line"


def test_parse_global_user_table_explained_handles_bad_role_match_container():
    """Test that a bad token container during role matching does not abort parsing."""
    class BadSliceTokens(list):
        def __getitem__(self, key):
            if isinstance(key, slice):
                raise RuntimeError("bad token slice")
            return super().__getitem__(key)

    class BadTokenLine(str):
        def strip(self, *args, **kwargs):
            return self

        def split(self, *args, **kwargs):
            return BadSliceTokens(["192.0.2.10", "aa:bb:cc:00:00:01", "user-a"])

    class BadOutput(str):
        def splitlines(self, *args, **kwargs):
            return [BadTokenLine("192.0.2.10 aa:bb:cc:00:00:01 user-a")]

    result = parse_global_user_table_explained(BadOutput(""), role_filter="profiling")

    assert result.entries == []
    assert len(result.decisions) == 1
    assert result.decisions[0].action == "ignored"
    assert result.decisions[0].reason == "invalid_line"


def test_parse_global_user_table_explained_handles_bad_role_lookup_token():
    """Test that a bad token before the role column does not abort parsing."""
    class BadTokenLine(str):
        def strip(self, *args, **kwargs):
            return self

        def split(self, *args, **kwargs):
            return [BadToken("bad"), "192.0.2.10", "aa:bb:cc:00:00:01", "user-a", "profiling"]

    class BadOutput(str):
        def splitlines(self, *args, **kwargs):
            return [BadTokenLine("bad 192.0.2.10 aa:bb:cc:00:00:01 user-a profiling")]

    result = parse_global_user_table_explained(BadOutput(""), role_filter="profiling")

    assert [entry.mac for entry in result.entries] == ["aa:bb:cc:00:00:01"]
    assert result.decisions[-1].action == "selected"


def test_parse_global_user_table_explained_handles_bad_probable_role_token():
    """Test that a bad probable role token does not abort parsing."""
    class BadTokenLine(str):
        def strip(self, *args, **kwargs):
            return self

        def split(self, *args, **kwargs):
            return ["192.0.2.10", "aa:bb:cc:00:00:01", "user-a", BadToken("profiling")]

    class BadOutput(str):
        def splitlines(self, *args, **kwargs):
            return [BadTokenLine("192.0.2.10 aa:bb:cc:00:00:01 user-a profiling")]

    result = parse_global_user_table_explained(BadOutput(""), role_filter="profiling")

    assert [entry.mac for entry in result.entries] == ["aa:bb:cc:00:00:01"]
    assert result.entries[0].username == "user-a"


def test_parse_global_user_table_explained_handles_bad_username_token():
    """Test that a bad username token does not abort row parsing."""
    class BadTokenLine(str):
        def strip(self, *args, **kwargs):
            return self

        def split(self, *args, **kwargs):
            return ["192.0.2.10", "aa:bb:cc:00:00:01", BadCasefoldToken("bad-user"), "profiling"]

    class BadOutput(str):
        def splitlines(self, *args, **kwargs):
            return [BadTokenLine("192.0.2.10 aa:bb:cc:00:00:01 bad-user profiling")]

    result = parse_global_user_table_explained(BadOutput(""), role_filter="profiling")

    assert [entry.mac for entry in result.entries] == ["aa:bb:cc:00:00:01"]
    assert result.decisions[-1].action == "selected"


def test_parse_global_user_table_explained_handles_bad_entry_detail_container():
    """Test that bad detail extraction does not abort a selected row."""
    class BadSliceTokens(list):
        def __getitem__(self, key):
            if isinstance(key, slice):
                raise RuntimeError("bad token slice")
            return super().__getitem__(key)

    class BadTokenLine(str):
        def strip(self, *args, **kwargs):
            return self

        def split(self, *args, **kwargs):
            return BadSliceTokens(["192.0.2.10", "aa:bb:cc:00:00:01", "user-a", "profiling"])

    class BadOutput(str):
        def splitlines(self, *args, **kwargs):
            return [BadTokenLine("192.0.2.10 aa:bb:cc:00:00:01 user-a profiling")]

    result = parse_global_user_table_explained(BadOutput(""), role_filter="profiling")

    assert [entry.mac for entry in result.entries] == ["aa:bb:cc:00:00:01"]
    assert result.entries[0].role == "profiling"
    assert result.entries[0].username == ""
    assert result.entries[0].ip_address == ""
    assert result.decisions[-1].action == "selected"


def test_parse_global_user_table_explained_handles_bad_duplicate_role_token():
    """Test that duplicate row decision logging does not abort on bad role access."""
    class BadRoleAccessTokens(list):
        def __getitem__(self, key):
            if key == 3:
                raise RuntimeError("bad role access")
            return super().__getitem__(key)

    class DuplicateBadTokenLine(str):
        def strip(self, *args, **kwargs):
            return self

        def split(self, *args, **kwargs):
            return BadRoleAccessTokens(["192.0.2.11", "aa:bb:cc:00:00:01", "user-b", "profiling"])

    class BadOutput(str):
        def splitlines(self, *args, **kwargs):
            return [
                "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling",
                DuplicateBadTokenLine("192.0.2.11 aa:bb:cc:00:00:01 user-b profiling"),
            ]

    result = parse_global_user_table_explained(BadOutput(""), role_filter="profiling")

    assert [entry.mac for entry in result.entries] == ["aa:bb:cc:00:00:01"]
    duplicate = result.decisions[-1]
    assert duplicate.action == "ignored"
    assert duplicate.reason == "duplicate_user_mac"
    assert duplicate.role == "profiling"


def test_parse_global_user_table_explained_handles_bad_unmatched_role_metadata():
    """Test that unmatched-role decision logging does not abort on bad role metadata access."""
    class BadRoleMetadataTokens(list):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.iter_calls = 0

        def __iter__(self):
            self.iter_calls += 1
            if self.iter_calls > 3:
                raise RuntimeError("bad role metadata iterator")
            return super().__iter__()

    class RoleMismatchLine(str):
        def strip(self, *args, **kwargs):
            return self

        def split(self, *args, **kwargs):
            return BadRoleMetadataTokens(["aa:bb:cc:00:00:01"])

    class BadOutput(str):
        def splitlines(self, *args, **kwargs):
            return [RoleMismatchLine("aa:bb:cc:00:00:01")]

    result = parse_global_user_table_explained(BadOutput(""), role_filter="profiling")

    assert result.entries == []
    decision = result.decisions[-1]
    assert decision.action == "ignored"
    assert decision.reason == "role_not_found"
    assert decision.mac == "aa:bb:cc:00:00:01"
    assert decision.role == "profiling"


def test_parse_global_user_table_explained_handles_bad_ip_lookup_token():
    """Test that a bad token before an IP address does not abort row parsing."""
    class BadTokenLine(str):
        def strip(self, *args, **kwargs):
            return self

        def split(self, *args, **kwargs):
            return [NonStringToken(), "192.0.2.10", "aa:bb:cc:00:00:01", "user-a", "profiling"]

    class BadOutput(str):
        def splitlines(self, *args, **kwargs):
            return [BadTokenLine("bad 192.0.2.10 aa:bb:cc:00:00:01 user-a profiling")]

    result = parse_global_user_table_explained(BadOutput(""), role_filter="profiling")

    assert [entry.mac for entry in result.entries] == ["aa:bb:cc:00:00:01"]
    assert result.entries[0].ip_address == "192.0.2.10"
