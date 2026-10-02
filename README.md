# Arb bot – srpske kladionice

Skuplja pre-match kvote sa 17 srpskih kladionica za **fudbal, košarku, tenis, hokej, rukomet, odbojku i stoni tenis**, uparuje iste mečeve i traži arbitraže.

| Kladionica | Kako | Fajl |
|---|---|---|
| Mozzart | headless Edge + `fetch` iz stranice (direktni zahtevi dobijaju 429) | `mozzart.py` |
| Meridian | JSON API, anonimni token iz HTML-a | `meridian.py` |
| MaxBet, Soccerbet, Merkur X-Tip, Oktagon, BetOle, BrazilBet, 365.rs | zajednička "restapi" platforma | `restapi.py` |
| Admiral | Admiral "WebBet" platforma | `admiral.py` |
| Balkanbet | NSoft distribution API | `nsoft.py` |
| Superbet | javni offer API (samo glavni market po sportu) | `superbet.py` |
| StarBet | ASP.NET page methods (bez Betradar ID-ja, uparuje se po imenu) | `starbet.py` |
| 1xBet, VivatBet | 1xBet "LineFeed" API, po ligama, osvežava se na 2 min (VivatBet ima iste kvote kao 1xBet) | `onexbet.py` |
| Topbet | cela ponuda kao jedan gzip JSON fajl | `topbet.py` |
| King.rs | Phoenix365 sportsbook API (njegov Betradar ID je nepouzdan, pa se uparuje po imenu) | `king.py` |
| 🪙 Stake.com | GraphQL iz headless Edge-a (Cloudflare), Betradar ID | `stake.py` |
| 🪙 1xBit | isti LineFeed kao 1xBet (sajt u Srbiji traži VPN) | `onexbet.py` |
| 🪙 BC.Game, BetFury, Rainbet, Betpanda, Betplay, Golden Panda, Thrill, Flush | Betby feed (sptpub.com), običan HTTP | `betby.py` |
| 🪙 Wild.io | njihov JSON API, samo glavne kvote (1X2 / pobednik); DNS preko Cloudflare-a ako provajder blokira | `wild.py` |
| 🪙 Sportsbet.io | GraphQL iz headless Edge-a (samo tačni upiti sajta), glavne kvote, pun krug na 2 min | `sportsbet.py` |
| 🪙 CasinOK | Digitain API iz headless Edge-a, Betradar ID (`bid`), pun krug na 2 min | `casinok.py` |
| 🪙 Cloudbet | njihov JSON API (DNS preko Cloudflare-a), Betradar ID | `cloudbet.py` |
| 🪙 Dexsport | websocket iz headless Edge-a (disciplina → turniri → mečevi → marketi), pun krug na 2 min | `dexsport.py` |
| 🪙 Duelbits | svoj sportsbook (Betradar), JSON iz deljenog browsera (Cloudflare), glavne kvote | `duelbits.py` |
| 🪙 Shuffle | svoj sportsbook (Betradar), GraphQL (samo tačni upiti sajta, ograničen broj zahteva → pun krug ~2 min na 5 min) | `shuffle.py` |
| 🪙 Vave | svoja platforma (Betradar), JSON API, svi marketi | `vave.py` |
| 🪙 Polymarket | berza (prediction market), gamma API, najbolje kvote; „Ne“ strana = dupla šansa | `polymarket.py` |
| 🪙 SX Bet | berza, V3 order book, traži API ključ – unosi se u botu: 🏦 Kladionice → 🔑 SX Bet ključ (čuva se u data/secrets.json) | `sxbet.py` |

Svi fajlovi su u `arb/scrapers/`. Za svaki meč scraper vraća **direktan link do utakmice** na sajtu kladionice (Topbet: njihov sportsbook `sportbook.topbetbo.com`).

Marketi:
- ⚽ fudbal: 1X2, ukupno golova 1.5 / 2.5 / 3.5, GG/NG, plus **kombinacije sa duplom šansom**: 1 + X2, X + 12, 2 + 1X (važi i za hokej)
- 🏀 košarka: pobednik sa produžecima, 1X2 bez produžetaka
- 🎾 tenis: pobednik meča
- 🏒 hokej: 1X2 (regularno vreme)
- 🤾 rukomet: 1X2 · 🏐 odbojka i 🏓 stoni tenis: pobednik meča

Uparivanje: prvo po **Betradar ID-ju**, pa po vremenu i imenu. Svaki meč se okreće na isti redosled domaćin/gost (u tenisu kladionice različito ređaju igrače), a u porukama piše ime igrača ili tima umesto "1"/"2".

## Pokretanje

**Najlakše: dupli klik na `start.bat`.** Proveri Python, instalira biblioteke, pokrene bota i
restartuje ga posle 10 s ako padne. Bot se gasi zatvaranjem prozora.

Ručno:

```bash
pip install -r requirements.txt
python bot.py                  # Telegram bot (token i ID u .env)
python scan.py                 # jedan scan u terminalu, ispiše arbitraže za ulog 10.000
python scan.py --stake 5000 --min 0.5 --dump
```

### Telegram bot

Bot izbacuje **sve** arbitraže, bez filtera za procenat. Za svaku prikazuje:
- koliko da uložiš na koju kvotu u kojoj kladionici, za tvoj ulog
- tabelu profita za ulog od 10k / 50k / 100k / 200k
- 🔗 **dugme za svaku kladionicu**, koje otvara baš tu utakmicu na njihovom sajtu
- 💰 **Promeni ulog**, 🔄 **Osveži** i ❌ **Sakrij**

Ulog menjaš tako što upišeš broj u chat (`50000`, `50k`, `50 000`) ili preko dugmeta `💰 Ulog`.

Ostalo:
- `🔍 Arbitraže`: sve trenutne arbitraže, najveći profit prvi
- `🏦 Kladionice`: koje kladionice pratiš (uključi samo one gde imaš nalog); dugme `🪙 Prebaci na kripto` prebacuje na kripto kladionice (Stake, 1xBit, BC.Game, BetFury, Rainbet, Betpanda, Betplay, Golden Panda, Wild.io, Sportsbet.io, CasinOK, Thrill, Cloudbet, Dexsport, Duelbits, Shuffle, Vave, Flush, Polymarket, SX Bet); ulog u $ ide i na pola dolara (npr. 10.5) sa ulogom u $, i nazad
- `📋 Lista arbitraža` ili `/arbitraze`: **sve** arbitraže u jednoj poruci, po 10 na strani (◀️ ▶️). Filter **Sve / 24h / 6h / 3h** (samo mečevi koji počinju u tom roku) i sortiranje **po profitu ili po vremenu**; izbor se pamti. Poruka se sama osvežava posle svakog scana (12 h ili dok ne klikneš ⏸). Broj otvara detalje sa ulozima i linkovima.
- `🔔 Obaveštenja`: uključi ili isključi i postavi minimalni % (npr. 1.5%). Posebnom porukom stižu samo arbitraže iznad tog procenta, a lista i dalje prikazuje sve.
- `/bot` (ili dugme `🤖 Bot (SX + Polymarket)` u kripto režimu): 🤖 **automatsko klađenje SX Bet + Polymarket**, preko njihovih zvaničnih API-ja. Panel vodi kroz podešavanje redom:
  1. 🪙 kripto režim, 2. 🔑 SX Bet API ključ, 3. 🔵 SX Bet novčanik (privatni ključ – njime se potpisuju nalozi), 4. 🟣 Polymarket nalog (adresa + privatni ključ, tip naloga email/Google ili MetaMask), 5. USDC na obe berze, 6. 🤖 uključi.
  - Ključevi idu u Windows Credential Manager (`keyring`), nikad u fajl; poruka sa ključem se odmah briše iz chata. Preporuka: poseban novčanik samo za bota, sa samo onoliko novca koliko je za klađenje.
  - ⚙️ Pravila: najviše $ po arbitraži, dnevno i u otvorenim tiketima, najmanji profit %, rok početka meča. ✋ „Pitaj pre uplate“ je uključeno dok ga ne isključiš (dugme ✅ Uplati važi 90 s, kvote se tad proveravaju ponovo).
  - Redosled: kvote se ponovo pročitaju sa obe berze, pa **prvo SX Bet** nalog „fill-or-kill“ po planiranoj kvoti ili boljoj (ako ne prođe, ništa nije uplaćeno), pa **Polymarket** FOK po ceni na kojoj je cela arbitraža najgore na nuli, za iznos koji je SX stvarno primio. Ako Polymarket ne prođe ni posle 3 pokušaja, pokriva uz gubitak do 5 %; ako ni to, stiže 🚨 poruka sa 🛟 Pokrij.
  - Pre svake uplate proverava da li Polymarket dozvoljava tvoju zemlju (njihov geoblock) – ako ne, ništa ne uplaćuje. Bot ne ide preko VPN-a niti zaobilazi ograničenja berzi.
  - 📈 **Procena**: posle svakog skeniranja beleži svaku SX Bet + Polymarket arbitražu (`data/live.db`) i pokazuje koliko ih ima dnevno, tipičan %, koliko primaju i koliko bi to bilo $ i % dnevno na tvoj kapital – gornja granica, kao da je svaka uhvaćena.
  - 🔍 Proba bez uplate: ceo put na najboljoj arbitraži (oba naloga se naprave i potpišu), ništa se ne šalje. 📒 Tiketi, 📊 Izveštaj (i uveče), 🛑 STOP.
  - Ishod se čita sa Polymarket-a kad se tržište razreši; ako ne može, bot pita ko je dobio. Dobitak na Polymarket-u se preuzima na sajtu (Claim).
- `/bottest`: 🧪 **test na papiru** (samo kripto), sa svojim pravilima, odvojenim od obaveštenja:
  - ⏰ meč počinje u narednih N sati (1 / 3 / 6 / 12 / 24 h, bilo kad ili upišeš svoje)
  - 📈 najmanji profit u %
  - 💵 najveći ulog po arbitraži u $; manje ako kladionica ili Polymarket ponuda ne prima toliko (najmanje 5 $)
  - ⏱ koliko posle prve noge ide druga: 5 s (novac je već na obe kladionice) do 5 min (novac se prvo šalje, npr. preko Solane)
  - 💼 **novac po kladionicama**: upišeš koliko imaš na kojoj (npr. 1xBit 25 $, Polymarket 25 $). Test igra samo arbitraže između tih kladionica, a bot sam računa podelu: nijedna strana ne dobija više nego što tamo ima (podela 70/30 sa 25 $ + 25 $ → oko 25 $ + 10,7 $). Bez toga se koristi jedan zajednički budžet. Sa novcem upisanim samo na SX Bet + Polymarket test pokazuje koliko bi od tih arbitraža stvarno prošlo.
  - ulog je „u igri“ dok se meč ne završi (računa se 3 h posle početka); tada se ulog i zarada vraćaju na kladionicu na kojoj je opklada prošla (test ne zna pravi ishod, pa ga izvlači po kvotama). `/bottest` pokazuje balans, novac na svakoj kladionici, šta je u igri i koliko zarade čeka; ⚖️ predloži prebacivanje kad jedna strana ostane bez novca
  - 📊 **Parovi kladionica**: koji parovi su najčešći u arbitražama koje prolaze tvoja pravila (poslednja 24 h), da znaš gde da staviš novac
  - 🔄 Kreni ispočetka: balans opet kreće od upisanog novca

  Bot „igra“ svaku arbitražu koja prolazi pravila, ali **ništa ne uplaćuje**. Proveri kvote uživo, „uplati“ prvu nogu, posle izabranog vremena ponovo proveri poslednju i javi da li bi prošlo i kolika bi bila zarada. Više testova radi istovremeno, a ulog se odmah rezerviše. Mečevi koji počinju pre druge uplate se preskaču. Uveče stiže izveštaj, a 📊 Izveštaj ga prikazuje odmah. Rezultati se čuvaju u `data/paper.db`.
- Detalji arbitraže prikazuju profit za svaki ishod ("ako prođe X") i minimalnu i maksimalnu zaradu.
- `📊 Status`: da li sve kladionice rade i koliko je mečeva upareno

Podešavanja korisnika se čuvaju u `data/users.json`.

`--dump` čuva sve uparene kvote u `data/snapshot.json`, da se lako proveri šta je bot video.

Mozzart koristi Microsoft Edge koji već postoji na Windowsu. Ako Edge nije dostupan:
`playwright install chromium`, pa `set MOZZART_BROWSER=chromium`.

## Struktura

```
arb/models.py        Event + definicije marketa
arb/scrapers/        po jedan scraper za svaku kladionicu
arb/matcher.py       uparivanje mečeva između kladionica
arb/arbitrage.py     traženje arbitraža + raspodela uloga
arb/scanner.py       pokreće sve scrapere paralelno
arb/live/            pravo klađenje SX Bet + Polymarket (nalozi, redosled, knjiga tiketa, procena)
arb/tg/              Telegram bot (meni, dugmići, obaveštenja, podešavanja; /bot je arb/tg/auto.py)
bot.py               pokretanje bota
scan.py              CLI za ručni scan
```

## Brzina

Scan svih kladionica traje oko 15 s. Uparivanje mečeva koristi indeks po sportu i vremenu početka (a ne poređenje svih parova), pa traje manje od 1 s. Sav teški posao ide u poseban thread, a dugmići u botu uvek odmah koriste poslednji rezultat i ne čekaju scan. Log je u `data/bot.log`.

## Testovi

`python test_live.py`: automatsko klađenje bez interneta, sa lažnim berzama – SX Bet nalog (lestvica kvota, EIP-712 potpis, odgovori), redosled nogu i svaki kraj (obe uplaćene, SX odbio, SX delimično, Polymarket promašio → pokrivanje / 🚨, SX bez odgovora), geoblock, proba, zatvaranje posle meča i 📈 procena.

## Test pre restarta

`python test_bot.py crypto` (ili `rs` / `both`) prolazi kroz listu, klik na arbitražu, osvežavanje, upisan ulog i prebacivanje režima kao pravi korisnik i meri vreme.

## Marketi (kripto)

Pored 1X2 / dvojne šanse / oba daju gol / pobednika, kripto kladionice daju i **sve linije ukupnih golova/poena/gemova** (`OU_<linija>`) i **dvosmerne hendikepe** (`AH_<linija domaćina>`), samo cele i .5 linije (četvrt-linije se ne računaju - nisu sigurna arbitraža; Polymarket samo .5 jer push isplaćuje 50/50). Sportovi u kripto režimu: + bejzbol, američki fudbal, CS2, Dota 2, LoL, Valorant (pobednik).
Polymarket kvote su posle naknade (0,05·p·(1−p) po deonici); kod Polymarket/SX nogu bot piše tačno šta da se klikne (npr. „No“ na „Will India win?“ = X2).
"# arbitraza" 
