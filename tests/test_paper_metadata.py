from paper_metadata import (
    extract_affiliations_from_html,
    extract_affiliations_from_pdf,
)


def test_extract_affiliations_from_html_meta_and_schema():
    html = """
    <meta name="citation_author_institution" content="University of Water">
    <span itemprop="affiliation">River Research Institute</span>
    """
    assert extract_affiliations_from_html(html) == [
        "University of Water",
        "River Research Institute",
    ]


def test_extract_affiliations_from_pdf_first_page():
    import pymupdf as fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "A Paper Title\nAlice 1 Bob 2\n"
        "1 University of Water  2 River Research Institute\n"
        "alice@example.org\n\nAbstract\nWe forecast floods.",
    )
    pdf_bytes = document.tobytes()
    document.close()
    assert extract_affiliations_from_pdf(pdf_bytes) == [
        "University of Water",
        "River Research Institute",
    ]

