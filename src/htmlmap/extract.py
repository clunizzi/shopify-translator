from __future__ import annotations

from bs4 import BeautifulSoup, NavigableString, Tag

SKIP_TAGS = {"script", "style"}


def extract_text_segments(html: str) -> tuple[str, list[str]]:
    """
    Sostituisce tutti i nodi testuali con placeholder [[T0]], [[T1]]...
    Restituisce (html_con_placeholder, lista_segmenti).
    - Salta <script>/<style>
    - Gestisce testo adiacente ai tag (es. 'Scopri il<strong>...').
    """
    soup = BeautifulSoup(html or "", "html5lib")
    texts: list[str] = []

    def replace_in(node: Tag) -> None:
        for child in list(node.children):
            # Salta interi sottoalberi non testuali
            if isinstance(child, Tag) and child.name in SKIP_TAGS:
                continue
            if isinstance(child, NavigableString):
                original = str(child)
                idx = len(texts)
                texts.append(original)
                child.replace_with(f"[[T{idx}]]")
            else:
                replace_in(child)  # Tag generico

    root = soup.body if soup.body else soup  # html5lib avvolge in <html><body>
    replace_in(root)

    # Ricostruzione senza wrapper <html>/<body>
    html_with_placeholders = "".join(str(x) for x in root.contents)
    return html_with_placeholders, texts
