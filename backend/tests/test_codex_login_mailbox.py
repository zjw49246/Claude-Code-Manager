from scripts.codex_login import WEBMAIL_LOGIN_URLS, detect_mail_provider


def test_detect_mail_provider():
    assert detect_mail_provider("user@onet.pl") == "onet"
    assert detect_mail_provider("user@GAZETA.PL") == "gazeta"
    assert detect_mail_provider("user@israelmail.com") == "171mail"


def test_webmail_login_urls_are_explicit():
    assert WEBMAIL_LOGIN_URLS == {
        "onet": "https://onet.pl/poczta",
        "gazeta": "https://oauth.gazeta.pl",
    }
