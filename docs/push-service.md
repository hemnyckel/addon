# Push-tjänsten — skiss

Status: **under övervägande**. Inget är byggt. Det här dokumentet finns för att
kunna tänka färdigt innan något byggs.

Kontrollerat mot leverantörernas villkor **17 oktober 2026**.

## Varför den behövs

APNs accepterar bara pushar till ett bundle-id från **det team som äger det**.
Hemnyckel-reläet körs i någons hem, men appens bundle-id `se.hemnyckel.app` ägs av
oss. Alltså kan ett hem inte skicka notiser till Hemnyckel-appen med sin egen
nyckel — och vi kan inte lämna ut vår `.p8` till världen.

Tre modeller finns. Den här skissen är modell 1:

| Modell | Infrastruktur | Pris |
|---|---|---|
| 1. Central push-tjänst | En serverless-funktion vi driver | 0 kr/mån |
| 2. Push via Home Assistant | Ingen | Notisen kommer från HA-appen |
| 3. Ingen push | Ingen | Inga notiser i realtid |

Modell 2 kan byggas först och modell 1 sedan; de utesluter inte varandra.

## Modellen

```
   telefon ── parkoppling (QR) ──► hemmet
      │                              │
      │ 1. registrera push-token     │ 2. händelse (krypterad)
      ▼                              ▼
   ┌──────────────── push-tjänsten ────────────────┐
   │  håller APNs-nyckeln, känner inte innehållet  │
   └───────────────────────┬───────────────────────┘
                           │ 3. APNs
                           ▼
                        telefon
```

Tjänsten är ett **autentiserat rör**: den vet vilka enheter ett hem har, och
skickar vidare bytes den inte kan läsa.

## Protokollet

Allt över HTTPS. Hemmet har två hemligheter, som skapas **lokalt i reläet** vid
första start och följer med i parkopplingskoden:

- `home_id` — 128 bitar, publik identifierare
- `home_secret` — 256 bitar, hemlighet

| Anrop | Från | Innehåll |
|---|---|---|
| `POST /v1/subscribe` | appen | `{home_id, home_secret, token, env}` → lagrar enheten under hemmet |
| `DELETE /v1/subscribe` | appen | `{home_id, home_secret, token}` → glömmer enheten (avparkoppling) |
| `POST /v1/push` | reläet | `{home_id, home_secret, push}` → skickar till hemmets enheter, rensar döda tokens |
| `GET /v1/health` | vem som helst | `{ok: true}` — för en enkel syntest |

Svar: `204` när det gick, `401` vid fel hemlighet, `429` vid för många anrop
(per hem och globalt), `410` från APNs betyder att token ska tas bort.

`env` är `production` eller `development` och följer med varje registrering, så
en TestFlight-telefon och en debug-telefon kan samexistera.

## Kryptering

**Notiserna är änd-till-ände-krypterade.** Reläet krypterar nyttolasten med en
nyckel som delas vid parkopplingen; tjänsten ser bara chiffrerede bytes. Appens
notistjänst (`Extensions/NotificationService`, som redan finns) dekrypterar och
skriver om titel och brödtext innan iOS visar dem. Pushen skickas med
`mutable-content: 1`.

**Live Activities är undantaget.** En Live Activity kan inte köra en
notistjänst, så dess innehåll måste skickas läsbart. Det är försvarbart: det som
står i en Live Activity syns ändå på låsskärmen. Skissen accepterar den
avvägningen — men den ska stå i integritetspolicyn.

Tjänsten lagrar **inga händelser**. Bara `home_id → [tokens]` och en tidsstämpel.

## Drift

**Cloudflare Workers** (verifierat 17 oktober 2026):

| Resurs | Gratis | Vårt behov |
|---|---|---|
| Anrop | 100 000/dag (hård gräns) | ~100/dag |
| CPU | 10 ms/anrop | ~2 ms (JWT cachas) |
| KV | 1 000 skrivningar/dag | ~10/dag |
| Adress | `*.workers.dev` | gratis |

`.p8`-nyckeln läggs som Worker-secret, aldrig i kod. APNs-JWT:t cachas i KV med
60 minuters livstid, så signering sker ungefär en gång i timmen.

Jämförelse: **AWS** har alltid-gratis Lambda (1M anrop/mån) och DynamoDB (25 GB),
men nya konton är kreditbaserade och "Free plan" stänger kontot efter sex
månader — man måste välja betalkonto med budgetlarm. **Google Cloud Run** ger 2M
anrop/månad men kräver en container. **Vercel/Netlify Hobby får inte användas
kommersiellt** och är därför uteslutna.

## Reläets sida

En ny inställning, `push_mode`, med fyra värden:

| Värde | Beteende |
|---|---|
| `direct` | Dagens: reläet skickar själv med hemmets egen `.p8` (utvecklarens hem) |
| `cloud` | Reläet postar till tjänsten — brand-and-forget med kort timeout |
| `ha` | Reläet ber Home Assistant skicka notisen (`notify.*`) |
| `off` | Inga notiser |

Push ska aldrig blockera händelseflödet: en misslyckad push loggas och glöms.
Appen hämtar allt vid öppning oavsett.

## Appens sida

1. Vid parkoppling: registrera APNs-token mot hemmet (en POST, tyst).
2. Vid avparkoppling eller "lämna hemmet": `DELETE /v1/subscribe`.
3. Notistjänsten: dekryptera om `enc` finns i payloaden, annars visa som den är.
4. Inställningar: visa vilket läge hemmet kör, så ingen undrar varför notiserna
   kommer från Home Assistant i stället.

## Vad som INTE ingår

Konton, inloggning, webbpanel, statistik, annonser, spårning, historik i
molnet, multi-hem per installation. Tjänsten ska aldrig kunna svara på vilka
dörrar ett hem har.

## Öppna frågor innan beslut

1. **Vill vi driva något alls?** En tjänst ska vara uppe. Alternativet är att
   släppa med HA-vägen och se om någon efterfrågar mer.
2. **Var skapas `home_id`/`home_secret`?** Skissen säger lokalt i reläet, så
   tjänsten aldrig behöver ett registreringssteg. Det gör också varje hem
   oberoende av oss.
3. **Ska Live Activities vara läsbara?** Ovanstående avvägning, eller hoppa
   push-to-start helt.
4. **Hur länge får en token ligga kvar?** Tills APNs säger `410`, eller ska vi
   rensa efter ett år?
5. **Vad händer om tjänsten läggs ner?** Appen måste fortsätta fungera — den
   gör det i dag, och det ska förbli sant.

## Arbete

| Del | Omfattning |
|---|---|
| Worker + KV + tester | ~150 rader, en dag |
| Reläet: `push_mode` + klient | ~80 rader + tester |
| Appen: registrering + notistjänstens dekryptering | ~80 rader |
| Integritetspolicy + granskningsanteckningar | en timme |

Realistiskt: **en helg**, om beslutet är ja.
