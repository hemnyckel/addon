# Ändringar

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
