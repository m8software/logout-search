# How-to bake-off, round 2 on RunPod — 2026-10-13

Three newer models on the same 50 frozen topics, the same runner (`bakeoff.py`) and the same format check as
round 1 (`BAKEOFF.md`). **Nothing here is judged.** No judge has read these answers; the numbers below are
format, length and speed only, and say nothing about whether an answer is correct. Judging is the other lane's,
on lr.

**Two of three ran. Gemma-4-26B-A4B did not** (see "Not run").

## Result

Medians are over the answers that kept the format (`report.py` `shape`), as in BAKEOFF.md. Round 1 for scale,
first prompt: Qwen3-14B 104.5 words / 12.3 per step, gpt-oss-120b 122 / 15.9.

| model | prompt | format ok | median words | median steps | median words per step | tokens/answer (mean) | prompt tokens (mean) | 4 streams: 50 answers in | generated tokens/s at 4 streams | 20,000 topics | price on this GPU |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Qwen3.8-27B Q8_0 | base | 49/50 | 121 | 8 | 10.9 | 175 | 520 | 214.5 s | 41 | 23.8 h, 13.9 M tokens | $14.06 |
| Qwen3.8-27B Q8_0 | detail | 35/50 | 175 | 8 | 15.9 | 275 | 577 | 306.4 s | 45 | 34.0 h, 17.0 M tokens | $20.09 |
| Gemma-4-31B Q8_0 | base | 50/50 | 92 | 7 | 10.6 | 138 | 522 | 212.5 s | 33 | 23.6 h, 13.2 M tokens | $13.93 |
| Gemma-4-31B Q8_0 | detail | 47/50 | 133 | 6 | 13.9 | 180 | 578 | 259.7 s | 35 | 28.9 h, 15.2 M tokens | $17.02 |

- "20,000 topics" is the measured 4-stream wall time for 50 answers × 400, on the same card at $0.59/h, and the
  tokens are (prompt + generated) per answer × 20,000 (round 1 reference: 13.1 M with Qwen3-14B). Add about
  7 minutes of download and load. Only 4 streams were tried.
- Over all 50 answers, format kept or not: Qwen3.8-27B 123 words / 11.2 per step (first prompt) and 202 / 17.8
  (detailed); Gemma-4-31B 92 / 10.6 and 129.5 / 13.9. Qwen3.8's detailed median in the table (175) is lower
  than its all-answers median (202) because its 14 longest answers broke the 240-word cap and are left out.
- On the first prompt neither model writes more per step than Qwen3-14B did (10.9 and 10.6 against 12.3).
  Gemma-4-31B is the shortest model of both rounds (92 words). With the detailed prompt Qwen3.8-27B reaches
  15.9 words per step, level with gpt-oss-120b on the first prompt, but loses 15 of 50 to the format check.
- The `Mistake:` line the detailed prompt asks for is present in 49 of 50 (Qwen3.8) and 47 of 50 (Gemma;
  the three without it are its INSUFFICIENT SOURCE replies).

## Per model

| | Qwen3.8-27B Q8_0 | Gemma-4-31B Q8_0 |
|---|---|---|
| Model | `Qwen/Qwen3.8-27B`, hub revision 1d4bf0f2, Apache-2.0, not gated | `google/gemma-4-31B-it`, hub revision 842da379, Apache-2.0, not gated |
| Weights | `unsloth/Qwen3.8-27B-GGUF` revision 4ca72078, `Qwen3.8-27B-Q8_0.gguf`, 29,036,089,344 bytes as loaded, 27.32 B parameters, hub sha256 a680f44a06920e5d… | `unsloth/gemma-4-31B-it-GGUF` revision c1ac76e9, `gemma-4-31B-it-Q8_0.gguf`, 32,619,841,776 bytes as loaded, 30.70 B parameters, hub sha256 d5808e5874e660a8… |
| Quantisation | GGUF Q8_0 (8-bit), text only (`--no-mmproj`) | GGUF Q8_0 (8-bit), text only (`--no-mmproj`) |
| GPU | RunPod A40 48 GB, secure, $0.59/h, EU-SE-1 | RunPod A40 48 GB, secure, $0.59/h, CA-MTL-1 |
| Server | `ghcr.io/ggml-org/llama.cpp:server-cuda`, build b11580-4413a1924; `-ngl 99 -c 16384 -np 4` (4,096 tokens per stream) | the same |
| Pod | 15.2 min (ready at 5.4), balance moved $0.134 at once | 16.2 min (ready at 7.1), balance moved $0.144 |
| Request settings | temperature 0.2, top_p 0.9, `chat_template_kwargs: {"enable_thinking": false}`, max 400 tokens (detailed: 600) | the same |
| Thinking check | reasoning text: 0 characters in all 104 answers; no template tags in any text. Without the switch: 1,532 characters of reasoning and an empty answer at 400 tokens | 0 characters in all 104 answers; no template tags. Without the switch: 1,320 characters of reasoning and an empty answer at 400 tokens |
| Tokens per answer | first prompt mean 175 (87–287); detailed 275 (16–429) | first prompt mean 138 (72–238); detailed 180 (25–277) |
| One stream (4 topics) | 19.4 tokens/s | 16.1 tokens/s |
| Four streams | 41 tokens/s (11.8 per stream) first prompt; 45 detailed | 33 tokens/s (10.0 per stream) first prompt; 35 detailed |

- **Why unsloth's GGUF.** Round 1 ran Qwen3.5-27B from unsloth's Q8_0, so the same converter keeps the two Qwen
  rows comparable. Google's own Gemma 4 GGUFs are 4-bit QAT only; unsloth, bartowski and ggml-org all publish a
  Q8_0, and unsloth's repo has one Q8_0 file at its root, which is what `llama-server -hf <repo>:Q8_0` resolves
  to. The repo, revision and file in the table are the path each server reported for the model it loaded
  (`…/snapshots/<revision>/<file>`), not an assumption.
- **Gemma 4 thought by default on this server.** The card says thinking is off unless the system prompt starts
  with the think token, but one request sent with no switch at all came back as 1,320 characters of reasoning
  and no answer (measured on build b11580; why — presumably the server's template default — was not looked
  into). With `enable_thinking: false` in the request there is none. The Gemma-4-12B run on buildbox (another build,
  b11205) should be checked for the same thing: its `reasoning_chars` column will show it.
- Sampling is the bake-off's (0.2 / 0.9), not either card's recommended values (Qwen non-thinking 0.7 / 0.8 /
  top_k 20 / presence 1.5; Gemma 1.0 / 0.95 / top_k 64), the same choice as round 1.

## Not run: Gemma-4-26B-A4B

`unsloth/gemma-4-26B-A4B-it-GGUF:Q8_0` (revision c099eb48, 26.86 GB), same image and command. Two pods, one per
data centre (EU-SE-1, then CA-MTL-1): the server never answered on its port in 14.1 and 13.1 minutes, where the
two larger files were serving in 5.4 and 7.1. On the second pod the container's status was read every 30 s: up
the whole time, no restart, CPU 0–4 %. So it is not a crash loop; it is a download or a load that does not
finish. The cause is **not established** — this method has no shell and no log access. The hub manifest resolves
the tag to the right file and the file's URL answers. Stopped after two tries: $0.29 spent on it, no answers.
A third try would have been with ggml-org's Q8_0 or with a pod that has log access.

## Spend

Four pods, one at a time, each terminated (not stopped); no volume, no SSH key. Every pod had a local watchdog
(28, 30, 22 and 20 minutes) and a timer inside the pod; no pod lived to its cap, so neither fired. Balance **5.5974 before, 5.0338 after:
$0.56 spent of the $1.00 cap** (5.5973705792 − 5.0337937607 = 0.5635768185). After the last pod the API lists
zero pods and spend per hour is 0.

| pod | model | minutes | balance moved |
|---|---|---|---|
| fyczp1b95lq2ii | Qwen3.8-27B | 15.2 | 0.1339 |
| z0ap5x1cubh6fi | Gemma-4-31B | 16.2 | 0.1442 |
| tyjapwd1e1k03j | Gemma-4-26B-A4B, first try | 14.1 | 0.1442 |
| zybxq0v5qpki6u | Gemma-4-26B-A4B, second try | 13.1 | 0.1413 |

The per-pod figures are the balance read 20 s after each terminate; RunPod's billing trails by a little, so a
pod's last seconds can land in the next row. The total is exact.

## Answers that failed the format check

- Qwen3.8-27B Q8_0 — how to use the paris metro: 193 words > 180
- Qwen3.8-27B Q8_0 detail — how to make pancakes: 257 words > 240
- Qwen3.8-27B Q8_0 detail — how to tie a tie: 302 words > 240
- Qwen3.8-27B Q8_0 detail — how to sew a button: 302 words > 240
- Qwen3.8-27B Q8_0 detail — how to knit a scarf: 248 words > 240
- Qwen3.8-27B Q8_0 detail — how to make an origami crane: 280 words > 240
- Qwen3.8-27B Q8_0 detail — how to braid hair: 250 words > 240
- Qwen3.8-27B Q8_0 detail — how to remove a red wine stain: 277 words > 240
- Qwen3.8-27B Q8_0 detail — how to hang a picture on drywall: 241 words > 240
- Qwen3.8-27B Q8_0 detail — how to patch a small hole in drywall: 263 words > 240
- Qwen3.8-27B Q8_0 detail — how to set up a new wifi router: 250 words > 240
- Qwen3.8-27B Q8_0 detail — how to make a pdf smaller: 308 words > 240
- Qwen3.8-27B Q8_0 detail — how to get over jet lag: insufficient source
- Qwen3.8-27B Q8_0 detail — how to get from narita airport to tokyo: 246 words > 240
- Qwen3.8-27B Q8_0 detail — how to use the paris metro: 266 words > 240
- Qwen3.8-27B Q8_0 detail — how to do long division: 280 words > 240
- Gemma-4-31B Q8_0 detail — how to tie a bowline knot: insufficient source
- Gemma-4-31B Q8_0 detail — how to fix a running toilet: insufficient source
- Gemma-4-31B Q8_0 detail — how to get over jet lag: insufficient source

## Samples — the same 5 topics as BAKEOFF.md (unjudged)

### how to jump start a car

**Qwen3.8-27B Q8_0** — wikibooks:Automobile Repair/Jump start#Step-by-Step Guide — 156 words — format ok — unjudged

```
Needed: jumper cables, a car with a good battery
Time: 5 minutes
1. Clamp one cable to the positive (+) terminal of the dead battery.
2. Ensure the positive cable does not touch anything metal other than the battery terminals.
3. Connect the other end of the positive cable to the positive terminal of the good battery.
4. Connect one end of the negative (-) cable to the negative terminal of the good battery.
5. Connect the other end of the negative cable to metal on the engine block of the car with the dead battery.
6. Do not connect the negative cable to the dead battery, carburetor, fuel lines, or moving parts.
7. Stand back and start the car with the good battery.
8. Start the stalled car.
9. Remove the cables in reverse order.
Warning: Do not let the positive cable touch anything metal other than the battery terminals.
Source: wikibooks:Automobile Repair/Jump start#Step-by-Step Guide
```

**Qwen3.8-27B Q8_0 detail** — wikibooks:Automobile Repair/Jump start#Step-by-Step Guide — 172 words — format ok — unjudged

```
Needed: jumper cables, a car with a good battery
Time: 5 minutes
1. Clamp one cable to the positive (+) terminal of the dead battery. Ensure the positive cable touches nothing metal other than the battery terminals.
2. Connect the other end of the positive cable to the positive terminal of the good battery.
3. Connect one end of the negative (-) cable to the negative terminal of the good battery.
4. Connect the other end of the negative cable to metal on the engine block of the car with the dead battery. Do not connect it to the dead battery, carburetor, fuel lines, or moving parts.
5. Stand back and start the car with the good battery.
6. Start the stalled car.
7. Remove the cables in reverse order.
Mistake: Connecting the negative cable to the dead battery; avoid this by attaching it to the engine block instead.
Warning: Do not let the positive cable touch anything metal other than the battery terminals to prevent sparks.
Source: wikibooks:Automobile Repair/Jump start#Step-by-Step Guide
```

**Gemma-4-31B Q8_0** — wikibooks:Automobile Repair/Jump start#Step-by-Step Guide — 142 words — format ok — unjudged

```
Needed: jumper cables, a car with a good battery
Time: 10-15 minutes
1. Clamp one cable to the positive (+) terminal of the dead battery.
2. Connect the other end of the positive cable to the positive terminal of the good battery.
3. Connect one end of the negative (-) cable to the negative terminal of the good battery.
4. Connect the other end of the negative cable to metal on the engine block of the car with the dead battery.
5. Stand back and start the car with the good battery.
6. Start the stalled car.
7. Remove the cables in reverse order.

Warning: Do not let the positive cable touch metal other than battery terminals, and do not connect the negative cable to the dead battery, carburetor, fuel lines, or moving parts.

Source: wikibooks:Automobile Repair/Jump start#Step-by-Step Guide, CC BY-SA 4.0
```

**Gemma-4-31B Q8_0 detail** — wikibooks:Automobile Repair/Jump start#Step-by-Step Guide — 145 words — format ok — unjudged

```
Needed: jumper cables, a car with a good battery
Time: 10-15 minutes
1. Clamp one cable to the positive (+) terminal of the dead battery.
2. Connect the other end of that positive cable to the positive terminal of the good battery.
3. Connect one end of the negative (-) cable to the negative terminal of the good battery.
4. Connect the other end of the negative cable to metal on the engine block of the car with the dead battery.
5. Stand back and start the car with the good battery.
6. Start the stalled car.
7. Remove the cables in reverse order.

Mistake: Connecting the negative cable to the dead battery; connect it to metal on the engine block instead.
Warning: Do not let the positive cable touch any metal other than the battery terminals.

Source: wikibooks:Automobile Repair/Jump start#Step-by-Step Guide, CC BY-SA 4.0
```

### how to tie a tie

**Qwen3.8-27B Q8_0** — wikibooks:How To Tie A Tie/Tying the Knots — 133 words — format ok — unjudged

```
Needed: a necktie
Time: 1-2 minutes
1. Place the tie around your collar with the seams visible.
2. Let the wide end hang 30 cm (12 inches) lower than the narrow end.
3. Cross the narrow end over the front of the wide end, 12 cm (5 inches) from the collar.
4. Wrap the wide end across the narrow end so it is right side out.
5. Push the wide end through the large loop around your neck.
6. Push the wide end through the smaller loop around the tie.
7. Pull the wide end until the knot forms a pleasing shape.
8. Pull the thin end and push the knot up to your collar.
Warning: Pull gently to avoid choking or damaging the fabric.
Source: wikibooks:How To Tie A Tie/Tying the Knots
```

**Qwen3.8-27B Q8_0 detail** — wikibooks:How To Tie A Tie/Tying the Knots — 302 words — format FAILED: 302 words > 240 — unjudged

```
Needed: nothing special
Time: 2-5 minutes
1. Place the tie around your collar with the seams visible (Oriental) or hidden (Four-In-Hand/Windsor), ensuring the wide end hangs 30 cm (12 inches) lower than the narrow end for Oriental/Four-In-Hand, or 50 cm lower for Windsor.
2. Cross the wide end over the front of the narrow end, stopping about 12 cm (5 inches) below the collar.
3. For Oriental, wrap the wide end across the narrow end; for Four-In-Hand, fold the wide end behind the narrow end and loop it over the front; for Windsor, put the wide end through the back of the loop near your face.
4. For Oriental and Four-In-Hand, put the wide end through the back of the large neck loop; for Windsor, fold the wide end over the knot in the opposite direction and pull it around behind to the other side.
5. For Oriental and Four-In-Hand, push the wide end through the smaller loop around the tie; for Windsor, take the wide end through the top of the neck loop towards you and down.
6. For Windsor, bring the wide end around the knot from the same direction as step 4, finishing behind the knot, then bring it from the back through the large neck loop.
7. Push the wide end through the smaller loop around the tie and pull it through carefully, ensuring it does not fold near the knot.
8. Pull the thin end carefully and push the bottom of the knot gently to move it closer to your collar, making small adjustments until the shape is pleasing.
Mistake: The wide end begins to fold near the forming knot; straighten it immediately if this occurs.
Warning: Pull the thin end carefully to avoid choking or straining the neck.
Source: wikibooks:How To Tie A Tie/Tying the Knots
```

**Gemma-4-31B Q8_0** — wikibooks:How To Tie A Tie/Tying the Knots — 141 words — format ok — unjudged

```
Needed: a tie
Time: 5 minutes
1. Place the tie around the collar with the right side visible and the wide end 30 cm lower than the narrow end.
2. Place the wide end across the front of the narrow end about 12 cm from the collar.
3. Fold the wide end behind the narrow end and loop it over the front.
4. Put the wide end through the back of the large loop around the neck.
5. Push the wide end through the smaller loop around the tie and pull carefully.
6. Pull the larger end until the knot shape is what you want.
7. Pull the thin end carefully to move the knot closer to your collar.
8. Make small adjustments until you are happy with the knot.

Source: wikibooks:How To Tie A Tie/Tying the Knots, CC BY-SA 4.0
```

**Gemma-4-31B Q8_0 detail** — wikibooks:How To Tie A Tie/Tying the Knots — 162 words — format ok — unjudged

```
Needed: a tie
Time: 5 minutes
1. Place the tie around the collar with the seams and label hidden, letting the wide end hang 30 cm lower than the narrow end.
2. Place the wide end across the front of the narrow end about 12 cm from the collar.
3. Fold the wide end behind the narrow end and loop the wide end over the front.
4. Put the wide end through the back of the large loop around the neck.
5. Push the wide end through the smaller loop around the tie and pull carefully.
6. Pull the larger end until the knot has the shape you want.
7. Pull the thin end carefully to move the knot closer to the collar.
8. Make small adjustments until you are happy with the knot.

Mistake: The wide end may begin to fold near the forming knot; straighten it if this occurs.

Source: wikibooks:How To Tie A Tie/Tying the Knots, CC BY-SA 4.0
```

### how to get from narita airport to tokyo

**Qwen3.8-27B Q8_0** — wikivoyage:Narita International Airport#Ground transportation — 140 words — format ok — unjudged

```
Needed: Train ticket or Japan Rail Pass, valid ID
Time: 36 to 61 minutes
1. Identify your arrival terminal and go to the corresponding train station inside the terminal.
2. If arriving at Terminal 3, walk 500 meters or take the free shuttle bus to Terminal 2.
3. Choose between the Skyliner for the fastest ride (36 minutes) or the Narita Express for direct connections to major stations (61 minutes).
4. Purchase a ticket at the station or use a Japan Rail Pass if you have one.
5. Board the train, ensuring you are on the correct platform for your destination.
6. Ride to your desired Tokyo station, such as Tokyo, Shinagawa, Shibuya, or Shinjuku.
7. Exit the train and follow signs to your next destination or hotel.
Warning: Smoking is prohibited on all trains.
Source: wikivoyage:Narita International Airport#Ground transportation
```

**Qwen3.8-27B Q8_0 detail** — wikivoyage:Narita International Airport#Ground transportation — 246 words — format FAILED: 246 words > 240 — unjudged

```
Needed: Train ticket (Skyliner, Narita Express, or commuter fare), valid ID for ticket purchase if needed
Time: 36 to 61 minutes depending on train service
1. Identify your terminal: Check if you are at Terminal 1 or Terminal 2/3; Terminal 3 requires a 500-meter walk or free shuttle to Terminal 2 station.
2. Go to the correct station: Find the station marked "Narita Airport Terminal 1 Station" or "Narita Airport Terminal 2·3 Station" inside your terminal.
3. Choose your train: Select the Skyliner for the fastest ride (36 minutes) or the Narita Express for direct connections to major stations like Tokyo, Shinagawa, Shibuya, or Shinjuku (55–61 minutes).
4. Buy your ticket: Purchase a reserved seat ticket at the counter; the Narita Express costs ¥3,070 one-way to Tokyo Station, or ¥5,000 for a round-trip special ticket valid for 14 days.
5. Board the train: Find your reserved seat; note that smoking is prohibited on all trains.
6. Ride to your destination: Stay on the train until it reaches your target station, such as Tokyo, Shinagawa, Shibuya, or Shinjuku.
7. Exit the station: Follow signs to leave the train platform and enter the main station concourse.
Mistake: Boarding the wrong terminal station; avoid this by checking your airline's terminal assignment and walking to the matching station sign.
Warning: If heading to the Shinkansen, consider changing at Shinagawa instead of Tokyo to avoid a long underground walk, as Tokyo Station is four levels deep.
Source: wikivoyage:Narita International Airport#Ground transportation
```

**Gemma-4-31B Q8_0** — wikivoyage:Narita International Airport#Ground transportation — 98 words — format ok — unjudged

```
Needed: Train ticket or stored fare card (Suica or PASMO)
Time: 36 to 61 minutes
1. Locate the train station at Terminal 1 or Terminal 2·3.
2. Walk 500 meters or take the free shuttle bus from Terminal 3 to Terminal 2.
3. Choose a train service, such as the fast Skyliner or the Narita Express for direct connections to major stations.
4. Purchase a reserved seat ticket for express trains or use a fare card for commuter trains.
5. Board the train and travel to your destination in Tokyo.

Source: wikivoyage:Narita International Airport#Ground transportation, CC BY-SA 4.0
```

**Gemma-4-31B Q8_0 detail** — wikivoyage:Narita International Airport#Ground transportation — 129 words — format ok — unjudged

```
Needed: Train ticket or stored fare card (Suica or PASMO)
Time: 36 to 61 minutes
1. Walk to the station at Terminal 1 or Terminal 2·3, or take the free shuttle bus from Terminal 3 to Terminal 2.
2. Purchase a ticket or use a stored fare card at the station.
3. Board the Skyliner for the fastest ride (36 minutes) or the Narita Express (N'EX) for direct connections to major stations like Tokyo, Shinagawa, Shibuya, or Shinjuku.
4. Ride the train for 36 to 61 minutes depending on the service chosen.
5. Exit the train at your chosen major Tokyo train station.

Mistake: Getting off at the wrong terminal station; check the English signs for Terminal 1 or Terminal 2·3.

Source: wikivoyage:Narita International Airport#Ground transportation, CC BY-SA 4.0
```

### how to make cold brew coffee

**Qwen3.8-27B Q8_0** — UNGROUNDED — 121 words — format ok — unjudged

```
Needed: coarse ground coffee, cold water, jar or pitcher, filter or cheesecloth
Time: 12 to 18 hours
1. Add coarse ground coffee to a clean jar or pitcher.
2. Pour cold, filtered water over the grounds.
3. Stir gently to ensure all grounds are wet.
4. Cover the container and let it sit at room temperature or in the refrigerator.
5. Wait for at least 12 hours, but no more than 18 hours.
6. Strain the liquid through a fine mesh filter or cheesecloth into a clean container.
7. Discard the wet grounds.
8. Dilute the concentrate with water or milk to taste before serving.
Warning: Do not use hot water, as it will create regular coffee instead of cold brew.
```

**Qwen3.8-27B Q8_0 detail** — UNGROUNDED — 230 words — format ok — unjudged

```
Needed: coarse-ground coffee, cold water, large jar or pitcher, fine-mesh strainer, paper filter, coffee filter, mug
Time: 12 to 18 hours
1. Measure one part coffee to four parts water by weight, or use one cup of coarse grounds for every four cups of cold water.
2. Pour the cold water into a large jar or pitcher, then add the coffee grounds.
3. Stir the mixture gently for thirty seconds until all grounds are wet and no dry clumps remain on the surface.
4. Cover the container with a lid or plastic wrap to keep out dust and fridge odors.
5. Place the container in the refrigerator and let it steep for at least twelve hours, but no more than eighteen hours.
6. Remove the jar from the fridge and set up a fine-mesh strainer over a clean bowl or pitcher.
7. Line the strainer with a paper coffee filter and pour the steeped liquid through it slowly to catch fine particles.
8. Discard the wet grounds and store the clear coffee concentrate in a sealed container in the fridge for up to two weeks.

Mistake: Using fine espresso grounds, which makes the coffee gritty and hard to filter; always use coarse grounds like for a French press.
Warning: Do not use hot water, as it will start brewing immediately and ruin the smooth, low-acid flavor profile of cold brew.
```

**Gemma-4-31B Q8_0** — UNGROUNDED — 101 words — format ok — unjudged

```
Needed: coarsely ground coffee beans, water, a jar or pitcher, a strainer or cheesecloth
Time: 12 to 24 hours

1. Put the coarse coffee grounds into the jar.
2. Pour water over the grounds until they are fully covered.
3. Stir gently to ensure all grounds are wet.
4. Cover the jar with a lid or plastic wrap.
5. Let the mixture sit at room temperature or in the fridge for 12 to 24 hours.
6. Strain the liquid through a fine mesh strainer or cheesecloth into a clean container.
7. Dilute the coffee concentrate with water or milk before serving.
```

**Gemma-4-31B Q8_0 detail** — UNGROUNDED — 136 words — format ok — unjudged

```
Needed: coarsely ground coffee beans, water, a large jar or pitcher, a strainer or cheesecloth
Time: 12 to 24 hours

1. Pour 1 cup of coarsely ground coffee into a glass jar.
2. Add 4 cups of room-temperature water to the jar.
3. Stir the mixture gently with a spoon until all grounds are wet.
4. Cover the jar with a lid or plastic wrap and let it sit on the counter or in the fridge for 12 to 24 hours.
5. Pour the liquid through a fine-mesh strainer or a coffee filter into a clean pitcher to remove the grounds.
6. Dilute the resulting concentrate with equal parts water or milk in a glass before drinking.

Mistake: Using finely ground coffee, which makes the brew bitter and hard to filter; use a coarse, salt-like grind.
```

### how to factory reset an android phone

**Qwen3.8-27B Q8_0** — UNGROUNDED — 100 words — format ok — unjudged

```
Needed: nothing special
Time: 10 minutes
1. Back up your photos, contacts, and important files to the cloud or a computer.
2. Charge your phone to at least 50 percent to prevent it from turning off during the process.
3. Open the Settings app on your phone.
4. Scroll down and tap System.
5. Tap Reset.
6. Select Erase all data (factory reset).
7. Tap Reset phone and confirm your choice if prompted.
8. Wait for the phone to restart and set it up as new.
Warning: This permanently deletes all data on the device, including apps, photos, and accounts.
```

**Qwen3.8-27B Q8_0 detail** — UNGROUNDED — 233 words — format ok — unjudged

```
Needed: nothing special
Time: 10 minutes
1. Back up your data by opening the Settings app, tapping Accounts, and selecting the option to back up photos, contacts, and files to your cloud storage or computer.
2. Open the Settings app on your phone by tapping the gear icon in your app drawer or notification shade.
3. Scroll down to the bottom of the menu and tap System, which is usually the last option listed.
4. Tap Reset, then select Erase all data (factory reset) from the list of options.
5. Tap Reset phone to confirm your choice, then enter your PIN, pattern, or password when prompted to verify your identity.
6. Tap Erase everything to begin the process, which will delete all apps, accounts, and personal data from the device.
7. Wait for the phone to reboot automatically; this may take several minutes, so do not turn it off or remove the battery.
8. When the setup screen appears, the reset is complete; you can now set up the phone as new or sell it.
Mistake: Forgetting to remove your Google or manufacturer account before resetting, which can lock the phone for the next user; avoid this by signing out of accounts in Settings before starting the reset.
Warning: A factory reset permanently deletes all data on the phone; ensure you have a complete backup before proceeding, as this action cannot be undone.
```

**Gemma-4-31B Q8_0** — UNGROUNDED — 90 words — format ok — unjudged

```
Needed: Google account password, charger
Time: 10 to 20 minutes

1. Back up your photos, contacts, and files to a cloud service or computer.
2. Open the "Settings" app.
3. Tap "System" or "General Management."
4. Select "Reset" or "Reset options."
5. Tap "Factory data reset" or "Erase all data."
6. Confirm that you want to erase everything.
7. Enter your screen lock PIN or Google password if prompted.
8. Wait for the phone to restart and reach the setup screen.

Warning: This permanently deletes all data on the device.
```

**Gemma-4-31B Q8_0 detail** — UNGROUNDED — 152 words — format ok — unjudged

```
Needed: Google account password, charger
Time: 10 to 20 minutes

1. Plug the phone into a charger to ensure it does not power off during the process.
2. Open the "Settings" app from the home screen or app drawer.
3. Tap the search bar at the top and type "reset" to find the reset menu quickly.
4. Select "Factory data reset" or "Erase all data" from the search results.
5. Tap the "Erase all data" button at the bottom of the screen.
6. Enter your screen lock PIN or password when prompted to confirm your identity.
7. Tap "Erase all data" one final time to start the wipe.
8. Wait for the phone to restart and show the original welcome setup screen.

Mistake: Forgetting to back up photos and contacts first; use Google Drive or a computer to save files before starting.
Warning: This permanently deletes all personal data on the device.
```
