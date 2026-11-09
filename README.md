# LogeKoll – GitHub Pages

Detta arkiv innehåller en uppdaterad statisk GitHub Pages-webbplats för LogeKoll. Webbplatsens publiceringskatalog är **`docs/`**.

## Nytt i den här leveransen

- En fristående svensk presentationssida: **`docs/om-appen.html`**, med en redaktionell genomgång av appens funktioner, responsiv formgivning och skärmbilder från det tillhandahållna Word-dokumentet.
- Tydlig ingång till **Om appen** från startsidan, huvudnavigationen och sidfoten.
- Versionsbeteckning **1.2 för både iPhone och Android** på alla sidor och i språkfilen `docs/locales/sv.json`.
- Skärmbilder omvandlade till komprimerad WebP under `docs/assets/screenshots/`, med bibehållen ursprunglig information.
- Direktlänkar `docs/about.html` och `docs/sv/om-appen.html` som hänvisar till den nya sidan.

## Publicera på GitHub Pages

1. Ersätt eller uppdatera innehållet i GitHub-repositoriet med filerna från denna katalog. **Behåll `docs/` som webbplatsrot.**
2. Gå till **Settings → Pages** i GitHub-repositoriet.
3. Välj **Deploy from a branch**, rätt branch (normalt `main`) och mappen **`/docs`** om repositoryt använder den publiceringsmetoden.
4. Spara och kontrollera startsidan, länken **Om appen**, support och integritet efter publicering.

Alternativt kan befintligt GitHub Actions-flöde för Pages fortsätta användas, om det redan är konfigurerat för `docs/`.

## Källor och kvalitetssäkring

Webbtexterna på den nya sidan är sammanfattade från `LogeKoll_appbeskrivning_iPhone_Android(2).docx` och webbplatsens befintliga innehåll. Skärmbilderna i dokumentet visar iPhone-gränssnittet; webbplatsen klargör att Android kan skilja sig visuellt.

Versionsnumren har uppdaterats enligt beställningen. **De faktiska iOS-/Android-byggena för version 1.2 ingick inte i materialet**, så operativsystemsspecifika säkerhets- och integritetspåståenden bör kontrolleras mot respektive verklig appversion före publicering. I övrigt har de ursprungliga funktionsbeskrivningarna och integritetspolicyn behållits i sak.

Ingen extern JavaScript- eller CSS-beroendekedja har tillkommit. Svenska är fortsatt det enda publicerade språket.
