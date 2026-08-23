from pathlib import Path

from aruba_mm_cleanup.parser import normalize_mac, parse_global_user_table, parse_global_user_table_explained

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_normalize_mac_accepts_common_formats():
    assert normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("aabb.ccdd.eeff") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("not-a-mac") == ""


def test_parse_global_user_table_extracts_user_mac_only():
    output = """
Global User Table
-----------------
IP              MAC Address          User          Role        VLAN  BSSID
192.0.2.10       aa:bb:cc:00:00:01    user-a        profiling   20    11:22:33:44:55:66
192.0.2.11       aa-bb-cc-00-00-02    user-b        employee    30    22:33:44:55:66:77
192.0.2.12       aabb.cc00.0003       user-c        profiling   20    33:44:55:66:77:88
"""

    entries = parse_global_user_table(output, role_filter="profiling")

    assert [entry.mac for entry in entries] == [
        "aa:bb:cc:00:00:01",
        "aa:bb:cc:00:00:03",
    ]
    assert entries[0].ip_address == "192.0.2.10"
    assert entries[0].username == "user-a"


def test_parse_global_user_table_uses_default_role_when_role_filter_is_none():
    output = """
192.0.2.10 aa:bb:cc:00:00:01 user-a profiling
192.0.2.11 aa:bb:cc:00:00:02 user-b employee
"""

    entries = parse_global_user_table(output, role_filter=None)  # type: ignore[arg-type]

    assert [entry.mac for entry in entries] == ["aa:bb:cc:00:00:01"]


def test_parse_global_user_table_uses_default_role_when_role_filter_strip_fails():
    class BadRole(str):
        def strip(self, *_args, **_kwargs):
            raise RuntimeError("bad strip")

    output = """
192.0.2.10 aa:bb:cc:00:00:01 user-a profiling
192.0.2.11 aa:bb:cc:00:00:02 user-b employee
"""

    entries = parse_global_user_table(output, role_filter=BadRole("profiling"))  # type: ignore[arg-type]

    assert [entry.mac for entry in entries] == ["aa:bb:cc:00:00:01"]


def test_parse_global_user_table_handles_non_string_output_without_attribute_error():
    result = parse_global_user_table_explained(None, role_filter="profiling")  # type: ignore[arg-type]

    assert result.entries == []
    assert len(result.decisions) == 1
    assert result.decisions[0].action == "ignored"
    assert result.decisions[0].reason == "invalid_output"


def test_parse_global_user_table_records_type_na_from_header():
    header = f"{'IP':<16}{'MAC Address':<21}{'User':<14}{'Role':<12}{'Type':<8}{'BSSID'}"
    output = "\n".join(
        [
            header,
            f"{'192.0.2.10':<16}{'aa:bb:cc:00:00:01':<21}{'user-a':<14}{'profiling':<12}{'N/A':<8}{'11:22:33:44:55:66'}",
            f"{'192.0.2.11':<16}{'aa:bb:cc:00:00:02':<21}{'user-b':<14}{'profiling':<12}{'user':<8}{'22:33:44:55:66:77'}",
        ]
    )

    result = parse_global_user_table_explained(output, role_filter="profiling")

    assert [entry.mac for entry in result.entries] == ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"]
    assert result.entries[0].user_type == "N/A"
    assert result.entries[0].type_na is True
    assert result.entries[1].user_type == "user"
    assert result.entries[1].type_na is False
    selected = {item.mac: item for item in result.decisions if item.action == "selected"}
    assert selected["aa:bb:cc:00:00:01"].type_na is True
    assert selected["aa:bb:cc:00:00:01"].user_type == "N/A"


def test_parse_global_user_table_deduplicates_user_macs():
    output = """
192.0.2.10 aa:bb:cc:00:00:01 user-a profiling 11:22:33:44:55:66
192.0.2.10 aa:bb:cc:00:00:01 user-a profiling 22:33:44:55:66:77
"""

    entries = parse_global_user_table(output, role_filter="profiling")

    assert [entry.mac for entry in entries] == ["aa:bb:cc:00:00:01"]


def test_parse_global_user_table_ignores_trailing_bssid_when_role_is_absent():
    output = """
User             IP              MAC                BSSID
user-a           192.0.2.10       aa:bb:cc:00:00:01  11:22:33:44:55:66
"""

    entries = parse_global_user_table(output, role_filter="profiling")

    assert [entry.mac for entry in entries] == ["aa:bb:cc:00:00:01"]


def test_parse_sanitized_aruba_standard_fixture_ignores_bssid_and_other_roles():
    output = (FIXTURE_DIR / "aruba_global_user_table_standard.txt").read_text(encoding="utf-8")

    entries = parse_global_user_table(output, role_filter="profiling")

    assert [entry.mac for entry in entries] == [
        "aa:bb:cc:00:10:01",
        "aa:bb:cc:00:10:02",
    ]
    assert "11:22:33:44:55:66" not in [entry.mac for entry in entries]
    assert "aa:bb:cc:00:10:03" not in [entry.mac for entry in entries]


def test_parse_ignores_row_when_username_matches_filter_but_role_differs():
    output = """
Global User Table
-----------------
IP              MAC Address          User          Role        VLAN
192.0.2.10       aa:bb:cc:00:00:01    profiling     employee    30
192.0.2.11       aa:bb:cc:00:00:02    user-a        profiling   20
"""

    result = parse_global_user_table_explained(output, role_filter="profiling")

    assert [entry.mac for entry in result.entries] == ["aa:bb:cc:00:00:02"]
    assert any(
        item.action == "ignored"
        and item.reason == "role_mismatch"
        and item.mac == "aa:bb:cc:00:00:01"
        and item.role == "employee"
        for item in result.decisions
    )


def test_parse_sanitized_aruba_compact_fixture_accepts_role_filtered_rows_with_ap_name():
    output = (FIXTURE_DIR / "aruba_global_user_table_filtered_compact.txt").read_text(encoding="utf-8")

    entries = parse_global_user_table(output, role_filter="profiling")

    assert [entry.mac for entry in entries] == [
        "aa:bb:cc:00:20:01",
        "aa:bb:cc:00:20:02",
    ]
    assert "44:55:66:77:88:99" not in [entry.mac for entry in entries]


def test_parse_compact_output_ignores_roleless_ap_radio_noise_with_ip():
    output = """
Users for role profiling
------------------------
User             IP Address      MAC Address          BSSID              AP Name       Age
sample-user-d    192.0.2.20      aa-bb-cc-00-20-01    44:55:66:77:88:99  sample-ap-04  12
ap-radio-01      192.0.2.70      66:77:88:99:aa:bb    radio
"""

    result = parse_global_user_table_explained(output, role_filter="profiling")

    assert [entry.mac for entry in result.entries] == ["aa:bb:cc:00:20:01"]
    assert "66:77:88:99:aa:bb" not in [entry.mac for entry in result.entries]
    assert any(
        item.action == "ignored" and item.reason == "device_mac_noise" and item.mac == "66:77:88:99:aa:bb"
        for item in result.decisions
    )


def test_parse_sanitized_aruba_dotted_fixture_ignores_ap_radio_mac():
    output = (FIXTURE_DIR / "aruba_global_user_table_dotted.txt").read_text(encoding="utf-8")

    entries = parse_global_user_table(output, role_filter="profiling")

    assert [entry.mac for entry in entries] == [
        "aa:bb:cc:00:30:01",
        "aa:bb:cc:00:30:02",
    ]
    assert "66:77:88:99:aa:bb" not in [entry.mac for entry in entries]


def test_parse_sanitized_aruba_user_first_fixture_records_type_and_ignores_radio_mac():
    output = (FIXTURE_DIR / "aruba_global_user_table_user_first_type.txt").read_text(encoding="utf-8")

    result = parse_global_user_table_explained(output, role_filter="profiling")

    assert [entry.mac for entry in result.entries] == [
        "aa:bb:cc:00:50:01",
        "aa:bb:cc:00:50:02",
    ]
    assert [entry.user_type for entry in result.entries] == ["N/A", "user"]
    assert [entry.type_na for entry in result.entries] == [True, False]
    assert "aa:bb:cc:00:50:03" not in [entry.mac for entry in result.entries]
    assert "44:55:66:77:88:99" not in [entry.mac for entry in result.entries]


def test_parse_ignores_cli_prompt_echo_and_pagination_noise():
    output = """
(mm-1) #show global-user-table list role profiling
Global User Table
-----------------
IP Address      MAC Address          User             Role        VLAN  BSSID
192.0.2.60      aa:bb:cc:00:60:01    sample-user-a    profiling   20    11:22:33:44:55:66
--More--
(mm-1) #
192.0.2.61      aa:bb:cc:00:60:02    sample-user-b    profiling   20    22:33:44:55:66:77
Total Users: 2
"""

    result = parse_global_user_table_explained(output, role_filter="profiling")

    assert [entry.mac for entry in result.entries] == [
        "aa:bb:cc:00:60:01",
        "aa:bb:cc:00:60:02",
    ]
    assert "11:22:33:44:55:66" not in [entry.mac for entry in result.entries]
    ignored_reasons = [item.reason for item in result.decisions if item.action == "ignored"]
    assert "no_mac_token" in ignored_reasons
    assert "header_or_command" in ignored_reasons


def test_parse_explained_records_selected_and_ignored_reasons():
    output = (FIXTURE_DIR / "aruba_global_user_table_mixed_reasoned.txt").read_text(encoding="utf-8")

    result = parse_global_user_table_explained(output, role_filter="profiling")

    assert [entry.mac for entry in result.entries] == [
        "aa:bb:cc:00:40:01",
        "aa:bb:cc:00:40:03",
    ]
    decisions = {(item.action, item.reason, item.mac) for item in result.decisions}
    assert ("selected", "selected_identity_mac_before_role", "aa:bb:cc:00:40:01") in decisions
    assert ("ignored", "role_mismatch", "aa:bb:cc:00:40:02") in decisions
    assert ("ignored", "duplicate_user_mac", "aa:bb:cc:00:40:03") in decisions
    assert any(item.action == "ignored" and item.reason == "role_not_found" for item in result.decisions)
