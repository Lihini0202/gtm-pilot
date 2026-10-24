from gtm_pilot.leads.clean import clean_consent, clean_country, clean_email, clean_name, clean_rows, normalize_header


def test_email_typo_is_fixed():
    assert clean_email(" Nimal.Perera@GMIAL.com ") == ("nimal.perera@gmail.com", True)


def test_good_email_unchanged():
    assert clean_email("a.b@combank.lk") == ("a.b@combank.lk", False)


def test_invalid_email_rejected():
    assert clean_email("nimal at gmail.com")[0] is None
    assert clean_email("")[0] is None


def test_names_are_normalised():
    assert clean_name("  nimal  ") == "Nimal"
    assert clean_name("MARY-JANE") == "Mary-Jane"
    assert clean_name("o'neil") == "O'Neil"


def test_country_variants():
    assert clean_country("lk") == "Sri Lanka"
    assert clean_country(" USA ") == "United States"
    assert clean_country("Germany") == "Germany"


def test_header_aliases():
    assert normalize_header("E-mail") == "email"
    assert normalize_header("Organisation") == "company"
    assert normalize_header("Favourite Colour") is None


def test_duplicates_merged_and_blanks_filled():
    rows = [
        {"Email": "a@bank.com", "First Name": "ann", "Company": ""},
        {"Email": "A@BANK.COM", "First Name": "Ann", "Company": "Big Bank"},
        {"Email": "broken", "First Name": "x"},
    ]
    clean, report = clean_rows(rows)
    assert len(clean) == 1
    assert clean[0]["company"] == "Big Bank"  # filled from the duplicate
    assert report.duplicates_removed == 1
    assert len(report.rejected) == 1
    assert report.rows_in == 3 and report.rows_out == 1


def test_personal_vs_work_email():
    clean, _ = clean_rows([{"Email": "x@gmail.com"}, {"Email": "y@combank.lk"}])
    assert [c["email_type"] for c in clean] == ["personal", "work"]


def test_consent_values():
    assert [clean_consent(v) for v in ("Yes", " opted in ", "No", "Unsubscribed", "", "maybe")] == [
        "yes", "yes", "no", "no", "", ""]


def test_a_no_on_any_scan_wins():
    rows = [{"Email": "a@bank.com", "Opt-in": "Yes"}, {"Email": "A@bank.com", "Opt-in": "No"}]
    clean, _ = clean_rows(rows)
    assert clean[0]["consent"] == "no"
