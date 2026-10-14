from frontera.scan import scan_text


def rules(text, terms=()):
    return [f.rule for f in scan_text(text, "t", terms)]


def test_secrets_detected_and_redacted():
    found = scan_text("token ghp_" + "a" * 36, "t")
    assert [f.rule for f in found] == ["github_token"]
    assert "a" * 10 not in found[0].excerpt
    assert rules("-----BEGIN OPENSSH PRIVATE KEY-----") == ["private_key"]
    assert rules("key sk-tellico-" + "x" * 24) == ["openai_style_key"]


def test_home_paths_but_not_placeholders():
    assert rules("see /Users/alice/project") == ["home_path"]
    assert rules("see /Users/<you>/project and /home/$USER") == []


def test_ip_addresses_except_documentation_ranges():
    assert rules("server 10.1.2.3") == ["ip_address"]
    assert rules("server 192.0.2.10 and 127.0.0.1") == []


def test_private_terms_case_insensitive():
    assert rules("ssh Secret-Host.example", ("secret-host.example",)) == ["private_term"]


def test_clean_text():
    assert rules("Add a unit test for the timeout path.") == []
