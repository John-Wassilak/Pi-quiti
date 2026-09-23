from piquiti.enrich import describe, root_domain
from piquiti.services import ServiceMap, parse_list


def lists(**files: str):
    return {name.replace("_", "-"): parse_list(name.replace("_", "-"), text) for name, text in files.items()}


SAMPLE = lists(
    alphabet="include:google\n",
    google="google.com\ngstatic.com\ndoubleclick.net @ads\ninclude:youtube\n",
    youtube="youtube.com\ngooglevideo.com\nfull:youtu.be\n",
    netflix="netflix.com\nnflxvideo.net\n",
    category_entertainment="include:netflix\ninclude:youtube\n",
    category_companies="include:google\n",
    category_ads_all="include:google @ads\n",
)


def test_root_domain():
    assert root_domain("a.b.bbc.co.uk") == "bbc.co.uk"
    assert root_domain("images-na.ssl-images-amazon.com") == "ssl-images-amazon.com"
    assert root_domain("foo.s3.amazonaws.com") == "amazonaws.com"
    assert root_domain("printer") == "printer"
    assert root_domain("10.0.168.192.in-addr.arpa") == "in-addr.arpa"
    assert root_domain("Example.COM.") == "example.com"


def test_app_is_most_specific_list_and_company_rolls_up():
    m = ServiceMap(SAMPLE)
    r = m.lookup("rr3---sn-abc.googlevideo.com")
    assert (r.app, r.company, r.category) == ("Youtube", "Alphabet", "Streaming & Entertainment")
    r = m.lookup("fonts.gstatic.com")
    assert (r.app, r.company, r.category) == ("Google", "Alphabet", "Tech Platforms")


def test_ads_attribute_does_not_pull_whole_list_into_ads():
    m = ServiceMap(SAMPLE)
    assert m.lookup("doubleclick.net").category == "Ads & Tracking"
    assert m.lookup("google.com").category == "Tech Platforms"


def test_full_rule_is_exact_only():
    m = ServiceMap(SAMPLE)
    assert m.lookup("youtu.be").app == "Youtube"
    assert m.lookup("x.youtu.be").app is None


def test_overrides_win_and_most_specific_override_wins():
    m = ServiceMap(
        SAMPLE,
        {
            "names": {"youtube": "YouTube"},
            "domains": {
                "netflix.com": {"app": "Netflix (override)"},
                "api.netflix.com": {"app": "Netflix API", "background": True},
            },
            "background": ["YouTube"],
        },
    )
    assert m.lookup("www.netflix.com").app == "Netflix (override)"
    r = m.lookup("x.api.netflix.com")
    assert (r.app, r.company, r.background, r.source) == ("Netflix API", "Netflix API", True, "override")
    r = m.lookup("youtube.com")
    assert (r.app, r.background) == ("YouTube", True)


def test_unmapped_domain_keeps_root():
    i = describe("cdn.somethingobscure.net", ServiceMap(SAMPLE))
    assert (i.app, i.root_domain, i.source) == (None, "somethingobscure.net", "none")
