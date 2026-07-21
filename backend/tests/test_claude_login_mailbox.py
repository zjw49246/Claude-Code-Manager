from scripts.auto_login import detect_login_method, uses_mailcatcher_api


def test_detect_claude_mailbox_provider():
    assert detect_login_method("user@onet.pl") == "onet"
    assert detect_login_method("user@GAZETA.PL") == "gazeta"
    assert detect_login_method("user@mail.com") == "mailcom"
    assert detect_login_method("user@example.com") == "171mail"


def test_onet_and_gazeta_use_mailcatcher_decode_api():
    assert uses_mailcatcher_api("onet") is True
    assert uses_mailcatcher_api("gazeta") is True
    assert uses_mailcatcher_api("mailcom") is True
    assert uses_mailcatcher_api("171mail") is False
