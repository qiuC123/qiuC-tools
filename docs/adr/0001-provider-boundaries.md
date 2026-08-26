# Keep provider boundaries explicit and read-only

wxcli uses four named providers—HTTP, Chrome, Official Account API, and local files—instead of treating all inputs as one scraper. This makes the source, authentication rules, verification behavior, and safety boundary visible: public pages use HTTP or a human-visible browser, official data uses explicitly authorized APIs, and local imports remain local. All providers are read-only.
