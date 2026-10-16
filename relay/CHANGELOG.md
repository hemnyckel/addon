# Ändringar

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
