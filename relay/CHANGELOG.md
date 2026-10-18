# Ändringar

## 0.10.16

- **`POST /people` — en person innan första fingret.** En person kan skapas utan
  app och utan kod, så ett finger kan läggas till i en ledig plats. Personen syns
  i Personer direkt, får identitet och ikon och kan senare bjudas in till appen.
- **Borttagning rensar hela platsen.** `DELETE /people/{person}` rensar PIN,
  finger och RFID per namngiven plats — inte bara koden — och fungerar även för
  en person som bara har finger. En borttagen persons finger kan därför inte
  återuppstå som en namnlös åtkomst.

## 0.10.15

- **Återförsök med samma kod även för en befintlig person.**
  `POST /people/{person}/code` tar emot en valfri `code` (4–8 siffror), så en
  dörr som inte svarade kan skrivas om utan att koden på de andra dörrarna
  roteras.

## 0.10.14

- **`POST /people/{person}/code` — en ny kod för en befintlig person.** Gamla
  kodplatser rensas först; kan en dörr inte rensas vägrar bytet, så två koder
  aldrig lever samtidigt. Samma siffror skrivs till alla personens dörrar, och
  en gäst **behåller sitt liv** — dörrar, dagar, tider och slutdatum ändras
  aldrig av en kodrotation.

## 0.10.13

- **Åtkomstpersoner får en identitet första gången Personer läses**, så en
  person som bara finns som finger-/kodplats kan ges en ikon redan innan någon
  inbjudan har skapats. Identiteten skapas en gång; listan skriver aldrig om en
  person som redan finns.

## 0.10.12

- **Personer visar även den som bara har åtkomst i låset.** Ett namn på en
  finger-, PIN- eller RFID-plats är en person, även utan app. Reläet läser
  slot-tabellen och visar dem som *Ingen app* med dörrar och typ av åtkomst —
  de rör inte låset och behöver ingen inbjudan för att finnas.
- **`POST /people/{person}/invite` — ren parningskod till en befintlig person.**
  Inga låskoder skapas eller roteras. En gäst ärver sitt liv (dörrar, dagar,
  tider, slutdatum) och en familjemedlem blir `user` när telefonen parkopplas.
  Personen behåller finger, kod, historik och ikon. Bara en levande
  parningskod per person; en ny ogiltigförklarar den gamla.

## 0.10.11

- **En gäst får en identitet när livet skrivs.** Gästen får därmed ett stabilt
  id och kan ges en ikon redan innan någon telefon har parkopplats — annars
  svarade ikonväljaren "okänd person" för en gäst som bara fanns som inbjudan
  eller kod.

## 0.10.10

- **Gäster är personer i Personer.** En gäst finns från det att livet skrivs —
  även om ingen telefon någonsin parkopplas. Tidigare byggdes Personer bara av
  parkopplade enheter, så en inbjuden gäst utan telefon försvann ur listan och
  gick inte att redigera, trots att koden fanns i låset. Nu visas gästen med
  rollen **Gäst**, sin dörr, dag, tid och sitt slutdatum — och kan redigeras där.
- **En kod under Nycklar blir också en gäst.** `POST /guest-codes` sparar
  gästens liv och platser, så städfirman dyker upp i Personer och schemat kan
  ändras i efterhand.
- **Ta bort en gäst utan telefon:** `DELETE /people/{person}` rensar låskoderna
  först och vägrar om en dörr inte svarar. En gäst med telefon tas bort via sin
  enhet, som förut.

## 0.10.9

- **En misslyckad dörr kan skrivas om med samma kod.** `POST /guest-codes` tar
  emot en valfri `code` (4–8 siffror). Appen behåller koden den visade en gång
  och skickar tillbaka den tillsammans med bara de dörrar som inte svarade —
  annars skulle en ny kod ogiltigförklara siffrorna som redan getts till
  personen.

## 0.10.8

- **En kod till alla dörrar.** En gäst har nu en enda kod, som skrivs identiskt
  till varje vald dörr — även när en redigering tvingar fram en ny kod byts den
  på alla dörrar samtidigt. Städfirman behöver aldrig hålla reda på olika koder
  per dörr.
- **Nytt `POST /guest-codes`** — en kod till flera dörrar utan inbjudan och utan
  app: namn, dörrar, valfria dagar/tider och slutdatum. Svaret är ett resultat
  per dörr, så appen kan visa exakt vilka dörrar som fick koden och vilka som
  inte gjorde det.

## 0.10.7

- **Lås: en långsam låsoperation kraschar inte längre reläet.** Ett anrop till
  `hemnyckel.clear_slot` rensar PIN och fingeravtryck, och Nimly-låset är
  batteridrivet och måste väckas mellan försöken — det kan ta längre tid än
  HTTP-klientens 15 sekunder. Timeouten blev då ett ospårat 500-svar och appen
  visade *"Reläet svarade inte som väntat"*, medan Home Assistant fortsatte och
  ofta hann bli klar. Tjänsteanrop får nu 45 sekunder, och en timeout rapporteras
  som ett avslag (502) i stället för ett kraschat anrop.

## 0.10.6

- **Parningskoden finns nu i Home Assistant** — som sensorn *Parningskod* på
  reläets enhet, med en knapp *Ny parningskod* bredvid. Ingen behöver längre
  öppna en logg för att komma igång med en ny telefon.
  - Koden ligger på en **egen MQTT-topic**, aldrig i `/health` — den dokumentet
    är öppet för alla som kan nå reläet, och en parningskod är en nyckel.
  - Koden gäller i tio minuter; därefter visar sensorn *Utgången* och knappen
    skapar en ny. Knappen är medvetet ingen automatisk förnyelse: en kod som
    alltid är giltig är en dörr som alltid står öppen.
  - Knappen och sensorn kommer via MQTT discovery, så de dyker upp av sig själva
    på reläets enhet i Home Assistant.

## 0.10.5

- **Lås:** reläet hittar en dörrs nycklar och låsdata även när dörren heter något
  annat än låset. Integrationen kortade låsets etikett — sensorerna säger
  "Källardörr" medan låset och appen säger "Källardörren" — och en dörr som var
  konfigurerad med den längre formen tappade därför sina nycklar, inklusive
  fingeravtrycksmappningen. Händelser fortsatte fungera, eftersom de matchas på
  `lock_entity`; bara nycklarna försvann, vilket gjorde felet lätt att missa.
  Matchningen går nu **först** på dörrens eget `lock_entity` — entity-id:t, som
  skapas en gång och aldrig byts — och därefter på namn, etikett och entry-id som
  förut. Ingen ändring behövs i konfigurationen.

## 0.10.4

- **Lås:** reläet hittar en dörrs nycklar och låsdata även när sensorerna är
  namngivna på svenska (`_nycklar` och `_lasdata`, i stället för `_slots` och
  `_lock_facts`). Namnbytet skedde i integrationen, men ett entity-id skapas en
  gång och byts aldrig — så ett äldre lås bär fortfarande de engelska namnen och
  ett nyare de svenska. Båda läses nu.
  Utan det fick ett nyare lås inga nycklar, och inget **live entry-id** — vilket
  också är vad en lås-/upplåsning från appen behöver.

## 0.10.3

- **Sophämtning:** kvällsnotisens klockslag och nedräkningens försprung är nu
  husets egna inställningar i appen (18:00 respektive 2 timmar som standard).
  Ett sparande som inte nämner dem behåller det huset redan valt.

## 0.10.2

- **Sophämtning:** påminnelsen och nedräkningen ankras nu vid husets egen frist i
  stället för elprisets morgontid. Med standardfristen 06:45 hade morgonnotisen
  annars kommit *efter* att tunnorna skulle stått ute, och nedräkningen hade
  aldrig startat eftersom dess fönster var tomt.
- **Sophämtning:** `DELETE /api/live/activity` känner igen `kind: "waste"`, så en
  avslutad aktivitet städas i stället för att lämna en rad kvar.

## 0.10.1

- **Sophämtning:** en Live Activity som startas på hämtningsmorgonen med
  nedräkning mot fristen, och som avslutas när fristen har passerat. Den bär
  fristen som `stale-date`, så kortet grånar i tid även om reläet inte hinner
  stänga det.

## 0.10.0

- **Sophämtning:** hushållets hämtningsdagar som en lista — namn, veckodag och
  paritet (varje / jämna / udda veckor) — plus "senast ute".
  `GET /api/waste` svarar vad som hämtas i dag, i morgon och nästa gång;
  `POST /api/waste/settings` låter en ägare skriva schemat.
- **Sophämtning:** en notis kvällen innan ("ut med tunnorna i kväll") och en på
  hämtningsmorgonen, med fristen. Varje telefon bestämmer själv om den vill bli
  påmind, och dess tysta timmar gäller fortfarande.

## 0.9.3

- Elpriset, låsen och MQTT-bryggan som förut.
