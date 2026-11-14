## Prompt variants (overnight 2026-10-14, buildbox)

A = the detailed prompt of 2026-10-13 (reused); B = A + a bare-versus-good example (re-potting a houseplant, not one of the 50) + 15-30 words per step; C = a JSON form the server enforces (do / how / done_when per step, "not stated" where the source does not say, sources up to 7,000 characters); D = C + a subject line by category + a second pass that rewrites vague steps. Judges: Qwen3-14B and clef-flash for pass counts; the fourth question ("does each step say how") by gpt-oss-20b at default reasoning, asked twice (dq1 / dq2). Numbers not in source: digits in a grounded answer that do not appear in the source text the model was given.

| model | arm | format ok | usable: Qwen3-14B judge | usable: clef-flash | usable: both | detailed enough dq1 / dq2 (flips) | wrong/dangerous flagged: Qwen3-14B / clef-flash | "not stated" slots | numbers not in source (answers) | example copies | words per step | tokens/answer | 20,000 topics on buildbox |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Gemma-4-12B QAT Q4_0 | A | 47/50 | 46 | 47 | 46 | 15 / 13 (2 flips) | 0 / 0 | — | 0 (0) | 0 | 14.6 | 191 | 10.9 h (4 streams) |
| Gemma-4-12B QAT Q4_0 | B | 48/50 | 45 | 47 | 44 | 15 / 16 (3 flips) | 1 / 1 | — | 1 (1) | 0 | 18.7 | 192 | 9.4 h (4 streams) |
| Gemma-4-12B QAT Q4_0 | C | 50/50 | 41 | 47 | 39 | 19 / 19 (2 flips) | 0 / 2 | 57 | 3 (2) | 0 | 33.1 | 466 | 19.8 h (4 streams) |
| Gemma-4-12B QAT Q4_0 | D | 50/50 | 40 | 45 | 38 | 17 / 17 (0 flips) | 1 / 3 | 44 | 5 (2) | 0 | 34.4 | 948 | 41.9 h (4 streams) |
| Qwen3-14B Q4_K_M | A | 49/50 | 47 | 46 | 44 | 19 / 21 (6 flips; unjudged: dq1 1, dq2 0) | 1 / 2 | — | 20 (12) | 0 | 15.6 | 215 | 8.9 h (4 streams) |
| Qwen3-14B Q4_K_M | B | 45/50 | 44 | 43 | 42 | 10 / 13 (5 flips) | 0 / 2 | — | 11 (7) | 0 | 17.0 | 212 | 10.6 h (3 streams) |
| Qwen3-14B Q4_K_M | C | 50/50 | 45 | 47 | 43 | 18 / 17 (1 flips; unjudged: dq1 1, dq2 0) | 1 / 2 | 41 | 9 (4) | 0 | 40.9 | 450 | 22.7 h (3 streams) |
| Qwen3-14B Q4_K_M | D | 49/50 | 44 | 46 | 41 | 20 / 18 (4 flips; unjudged: dq1 1, dq2 0) | 0 / 3 | 76 | 30 (6) | 0 | 42.2 | 876 | 44.8 h (3 streams) |
| gpt-oss-20b MXFP4 reasoning-low | A | 48/50 | 47 | 46 | 45 | 25 / 25 (8 flips) | 0 / 2 | — | 11 (7) | 0 | 15.3 | 258 | 18.7 h (1 stream) |
| gpt-oss-20b MXFP4 reasoning-low | B | 46/50 | 43 | 43 | 41 | 17 / 17 (4 flips) | 0 / 2 | — | 7 (5) | 0 | 13.7 | 225 | 17.4 h (1 stream) |
| gpt-oss-20b MXFP4 reasoning-low | C | 42/50 | 37 | 42 | 37 | 16 / 16 (2 flips) | 0 / 0 | 44 | 18 (4) | 0 | 46.8 | 537 | 40.0 h (1 stream) |
| gpt-oss-20b MXFP4 reasoning-low | D | 40/50 | 31 | 36 | 29 | 19 / 20 (3 flips) | 0 / 1 | 51 | 14 (1) | 1 | 46.7 | 985 | 86.4 h (1 stream) |

Format failures, per run:

- Gemma-4-12B QAT Q4_0 arm-B: how to fix a running toilet (insufficient source); how to find the area of a circle (insufficient source)
- Gemma-4-12B QAT Q4_0 arm-C: none
- Gemma-4-12B QAT Q4_0 arm-D: none
- Qwen3-14B Q4_K_M arm-B: how to make cold brew coffee (1 step(s) over 30 words); how to tie a bow tie (1 step(s) over 30 words); how to sew a button (5 step(s) over 30 words); how to knit a scarf (1 step(s) over 30 words); how to jump start a car (1 step(s) over 30 words)
- Qwen3-14B Q4_K_M arm-C: none
- Qwen3-14B Q4_K_M arm-D: how to fix a running toilet (insufficient source)
- gpt-oss-20b MXFP4 reasoning-low arm-B: how to bake bread (1 step(s) over 30 words); how to sew a button (1 step(s) over 30 words); how to get over jet lag (insufficient source); how to do long division (1 step(s) over 30 words)
- gpt-oss-20b MXFP4 reasoning-low arm-C: how to make a roux (insufficient source); how to tie a tie (insufficient source); how to fix a running toilet (insufficient source); how to remove dandelions from a lawn (insufficient source); how to get over jet lag (insufficient source); how to get from narita airport to tokyo (insufficient source); how to use the paris metro (insufficient source); how to find the area of a circle (insufficient source)
- gpt-oss-20b MXFP4 reasoning-low arm-D: how to tie a tie (insufficient source); how to fold a paper airplane (insufficient source); how to sew a button (insufficient source); how to tie a bowline knot (insufficient source); how to fix a running toilet (insufficient source); how to prune tomato plants (insufficient source); how to grow grass from seed (insufficient source); how to get over jet lag (insufficient source); how to get from narita airport to tokyo (insufficient source); how to use the paris metro (insufficient source)

Numbers in grounded answers that are not in the source text given to the model:

- **Gemma-4-12B QAT Q4_0 arm-B**: how to cook rice — 1/4
- **Gemma-4-12B QAT Q4_0 arm-C**: how to pack a suitcase — 3, 5; how to do long division — 0
- **Gemma-4-12B QAT Q4_0 arm-D**: how to pack a suitcase — 3, 5, 3, 5; how to do long division — 0
- **Qwen3-14B Q4_K_M detail**: how to make whipped cream — 10; how to cook rice — 5; how to bake bread — 10, 5; how to freeze bananas — 1/2; how to make a roux — 50, 50, 2, 3; how to crochet a chain stitch — 2, 4; how to tie a bowline knot — 1; how to fix a running toilet — 10, 15; how to sharpen a kitchen knife — 5, 10; how to grow grass from seed — 2; how to remove dandelions from a lawn — 3, 4; how to find the area of a circle — 3.1416
- **Qwen3-14B Q4_K_M arm-B**: how to crochet a chain stitch — 3, 4; how to tie a bowline knot — 1; how to fix a running toilet — 1/2, 10, 15; how to sharpen a kitchen knife — 5, 10; how to prune tomato plants — 7; how to grow grass from seed — 2; how to find the area of a circle — 3.14159
- **Qwen3-14B Q4_K_M arm-C**: how to make whipped cream — 2, 3, 2, 2; how to grow grass from seed — 2, 3; how to pack a suitcase — 3, 5; how to find the area of a circle — 3.1416
- **Qwen3-14B Q4_K_M arm-D**: how to make whipped cream — 5; how to prune tomato plants — 7; how to grow grass from seed — 2; how to pack a suitcase — 3, 5, 3, 5; how to do long division — 0, 10, 10, 10, 10, 8, 0, 0, 8, 10, 0, 20, 20, 20, 20, 20, 0, 0, 20, 20, 0, 0; how to find the area of a circle — 3.14
- **gpt-oss-20b MXFP4 reasoning-low detail**: how to freeze bananas — 1; how to fix a running toilet — 1; how to sharpen a kitchen knife — 5, 10, 2, 4; how to jump start a car — 2, 1; how to prune tomato plants — 14; how to calculate a percentage — 80; how to find the area of a circle — 3.1416
- **gpt-oss-20b MXFP4 reasoning-low arm-B**: how to sharpen a kitchen knife — 4, 5; how to jump start a car — 2; how to prune tomato plants — 14; how to grow grass from seed — 1, 1; how to find the area of a circle — 3.1416
- **gpt-oss-20b MXFP4 reasoning-low arm-C**: how to crochet a chain stitch — 180, 1; how to tie a bowline knot — 1; how to take a screenshot on a mac — 1, 1; how to do long division — 0, 10, 10, 10, 10, 8, 8, 10, 0, 8, 10, 0, 0
- **gpt-oss-20b MXFP4 reasoning-low arm-D**: how to do long division — 0, 10, 10, 10, 10, 8, 8, 10, 8, 0, 8, 10, 8, 0

### Samples — the same five topics, every arm

#### how to jump start a car

**Gemma-4-12B QAT Q4_0, arm A** — 148 words

```
Needed: jumper cables, a car with a dead battery, a car with a good battery
Time: 10 minutes
1. Clamp one cable to the positive (+) terminal of the dead battery.
2. Connect the other end of the positive cable to the positive terminal of the good battery.
3. Connect one end of the negative (-) cable to the negative terminal of the good battery.
4. Connect the other end of the negative cable to metal on the engine block on the car with the dead battery.
5. Stand back and start the car with the good battery.
6. Start the stalled car.
7. Remove the cables in reverse order.
Mistake: Let the positive cable touch anything metal other than the battery terminals.
Warning: Do not connect the negative cable to the dead battery, carburetor, fuel lines, or moving parts.

Source: wikibooks:Automobile Repair/Jump start#Step-by-Step Guide, CC BY-SA 4.0
```

**Gemma-4-12B QAT Q4_0, arm B** — 178 words

```
Needed: jumper cables, two cars
Time: 15 minutes
1. Clamp one cable to the positive (+) terminal of the dead battery by gripping the terminal with the clamp.
2. Connect the other end of the positive cable to the positive terminal of the good battery.
3. Connect one end of the negative (-) cable to the negative terminal of the good battery.
4. Connect the other end of the negative cable to metal on the engine block of the car with the dead battery.
5. Stand back and start the car with the good battery until you hear the engine run.
6. Start the stalled car by turning the ignition key or pressing the start button.
7. Remove the cables in reverse order by pulling them off the terminals one by one.
Mistake: Let the positive cable touch anything metal other than the battery terminals; keep the cable away from other metal parts.
Warning: Do not connect the negative cable to the dead battery, carburetor, fuel lines, or moving parts.

Source: wikibooks:Automobile Repair/Jump start#Step-by-Step Guide, CC BY-SA 4.0
```

**Gemma-4-12B QAT Q4_0, arm C** — 264 words

```
Needed: dead battery, good battery, positive (+) cable, negative (-) cable
Time: not stated
1. Clamp the positive cable to the dead battery. Attach one cable to the positive (+) terminal of the dead battery. Done when: The cable is secured to the terminal.
2. Connect the positive cable to the good battery. Connect the other end of the positive cable to the positive terminal of the good battery. Done when: The cable is connected to both batteries.
3. Connect the negative cable to the good battery. Connect one end of the negative (-) cable to the negative terminal of the good battery. Done when: The cable is secured to the terminal.
4. Connect the negative cable to the engine block. Connect the other end of the negative cable to metal on the engine block on the car with the dead battery. Done when: The cable is secured to the metal on the engine block.
5. Start the car with the good battery. Stand back and start the car with the good battery. Done when: The car with the good battery starts.
6. Start the stalled car. Start the stalled car. Done when: The stalled car starts.
7. Remove the cables. Remove the cables in reverse order. Done when: All cables are removed from the vehicles.
Mistake: Letting the positive cable touch anything metal other than the battery terminals; avoid this by ensuring the cable only touches the terminals.
Warning: Do not connect the negative cable to the dead battery, carburetor, fuel lines, or moving parts.
Source: wikibooks:Automobile Repair/Jump start#Step-by-Step Guide, CC BY-SA 4.0
```

**Gemma-4-12B QAT Q4_0, arm D** — 254 words

```
Needed: dead battery, good battery, positive (+) cable, negative (-) cable
Time: not stated
1. Clamp the positive cable to the dead battery. Connect one cable to the positive (+) terminal of the dead battery. Done when: The cable is attached to the terminal.
2. Connect the positive cable to the good battery. Connect the other end of the positive cable to the positive terminal of the good battery. Done when: The cable is attached to the terminal.
3. Connect the negative cable to the good battery. Connect one end of the negative (-) cable to the negative terminal of the good battery. Done when: The cable is attached to the terminal.
4. Connect the negative cable to the engine block. Connect the other end of the negative cable to metal on the engine block on the car with the dead battery. Done when: The cable is attached to the metal.
5. Start the car with the good battery. Stand back and start the car with the good battery. Done when: The car with the good battery starts.
6. Start the stalled car. not stated. Done when: The stalled car starts.
7. Remove the cables. Remove the cables in reverse order. Done when: All cables are removed.
Mistake: Letting the positive cable touch anything metal other than the battery terminals; avoid this by ensuring the cable only touches the terminal.
Warning: Don't connect the negative cable to the dead battery, carburetor, fuel lines or moving parts.
Source: wikibooks:Automobile Repair/Jump start#Step-by-Step Guide, CC BY-SA 4.0
```

**Qwen3-14B Q4_K_M, arm A** — 197 words

```
Needed: jumper cables, another car with a working battery, gloves, safety goggles  
Time: 10 minutes  
1. Clamp one end of the positive (+) cable to the positive terminal of the dead battery. Make sure the clamp does not touch any metal parts.  
2. Connect the other end of the positive cable to the positive terminal of the good battery.  
3. Connect one end of the negative (-) cable to the negative terminal of the good battery.  
4. Connect the other end of the negative cable to a metal part on the engine block of the car with the dead battery. Avoid connecting it to the dead battery, carburetor, fuel lines, or moving parts.  
5. Stand back and start the car with the good battery.  
6. Start the stalled car.  
7. Remove the cables in reverse order, starting with the negative cable from the engine block.  
Mistake: Connecting the negative cable to the dead battery or fuel system parts can cause sparks and a fire. Always attach it to a metal part on the engine block.  
Warning: Sparks can cause a fire if the negative cable touches fuel lines or the dead battery.  
Source: wikibooks:Automobile Repair/Jump start#Step-by-Step Guide
```

**Qwen3-14B Q4_K_M, arm B** — 180 words — FORMAT: 1 step(s) over 30 words

```
Needed: jumper cables, another car with working battery, gloves, safety glasses
Time: 10 minutes
1. Clamp one cable to the positive (+) terminal of the dead battery. Don't let the positive cable touch anything metal other than the battery terminals.
2. Connect the other end of the positive cable to the positive terminal of the good battery.
3. Connect one end of the negative (-) cable to the negative terminal of the good battery.
4. Connect the other end of the negative cable to metal on the engine block on the car with the dead battery. Don't connect it to the dead battery, carburetor, fuel lines or moving parts.
5. Stand back and start the car with the good battery.
6. Start the stalled car.
7. Remove the cables in reverse order.
Mistake: Connecting the negative cable to the dead battery or fuel system parts can cause sparks and danger. Avoid this by following the instructions carefully.
Warning: Sparks can cause explosions if done near fuel vapors. Work away from the fuel tank and engine.
Source: wikibooks:Automobile Repair/Jump start#Step-by-Step Guide
```

**Qwen3-14B Q4_K_M, arm C** — 385 words

```
Needed: Jumper cables, A working car with a good battery
Time: not stated
1. Clamp one cable to the positive (+) terminal of the dead battery. Use jumper cables. Connect the red clamp to the positive terminal of the dead battery. The positive terminal is marked with a '+' sign. Find it on the battery in the car. Done when: The red clamp is securely attached to the positive terminal of the dead battery.
2. Connect the other end of the positive cable to the positive terminal of the good battery. Use the same jumper cable. Connect the red clamp to the positive terminal of the good battery. The positive terminal is marked with a '+' sign. Find it on the battery in. Done when: The red clamp is securely attached to the positive terminal of the good battery.
3. Connect one end of the negative (-) cable to the negative terminal of the good battery. Use the black clamp of the jumper cable. Connect it to the negative terminal of the good battery. The negative terminal is marked with a '-' sign. Find it on the battery. Done when: The black clamp is securely attached to the negative terminal of the good battery.
4. Connect the other end of the negative cable to metal on the engine block on the car with. Use the black clamp of the jumper cable. Attach it to a metal part of the engine block, not to the dead battery, carburetor, fuel lines, or moving parts. Find the engine. Done when: The black clamp is securely attached to a metal part of the engine block.
5. Stand back and start the car with the good battery. not stated. Done when: The car with the good battery starts.
6. Start the stalled car. not stated. Done when: The stalled car starts.
7. Remove the cables in reverse order. not stated. Done when: The cables are removed in the reverse order they were connected.
Mistake: Connecting the negative cable to the dead battery or other sensitive parts can cause sparks or damage. Always connect the negative cable to a metal part of the
Warning: Connecting the cables incorrectly can cause sparks, fire, or injury. Always follow the correct order and avoid touching metal parts with the
Source: wikibooks:Automobile Repair/Jump start#Step-by-Step Guide
```

**Qwen3-14B Q4_K_M, arm D** — 388 words

```
Needed: Jumper cables, A working car with a good battery
Time: not stated
1. Clamp one cable to the positive (+) terminal of the dead battery. Use jumper cables. Connect the red clamp to the positive terminal of the dead battery. The positive terminal is usually marked with a '+' sign and is often red. You can [. Done when: The red clamp is securely attached to the positive terminal of the dead battery.
2. Connect the other end of the positive cable to the positive terminal of the good battery. Use the same jumper cable. Connect the other red clamp to the positive terminal of the good battery. The positive terminal is usually marked with a '+' sign and is often. Done when: The red clamp is securely attached to the positive terminal of the good battery.
3. Connect one end of the negative (-) cable to the negative terminal of the good battery. Use the black jumper cable. Connect the black clamp to the negative terminal of the good battery. The negative terminal is usually marked with a '-' sign and is often. Done when: The black clamp is securely attached to the negative terminal of the good battery.
4. Connect the other end of the negative cable to metal on the engine block on the car with a. Use the black jumper cable. Connect the other black clamp to a metal part of the engine block of the car with the dead battery. Avoid connecting it to the dead battery,. Done when: The black clamp is securely attached to a metal part of the engine block.
5. Stand back and start the car with the good battery. not stated. Done when: The car with the good battery starts.
6. Start the stalled car. not stated. Done when: The stalled car starts.
7. Remove the cables in reverse order. not stated. Done when: The cables are removed in the reverse order they were connected.
Mistake: Connecting the negative cable to the dead battery or other sensitive parts like the carburetor or fuel lines. To avoid this, make sure the negative cable is cl
Warning: Connecting the negative cable to the wrong part can cause sparks or damage to the car. Always connect it to a metal part of the engine block
Source: wikibooks:Automobile Repair/Jump start#Step-by-Step Guide
```

**gpt-oss-20b MXFP4 reasoning-low, arm A** — 128 words

```
Needed: 2 jump cables, 1 good battery car  
Time: 5–10 minutes  

1. Clamp one end of the positive (+) cable to the positive terminal of the dead battery.  
2. Clamp the other end of the positive cable to the positive terminal of the good battery.  
3. Clamp one end of the negative (-) cable to the negative terminal of the good battery.  
4. Clamp the other end of the negative cable to a metal part of the engine block on the car with the dead battery.  
5. Stand back and start the car with the good battery.  
6. Start the stalled car.  
7. Remove the cables in reverse order.  

Mistake: Forgetting to clamp the negative cable to the engine block can cause sparks.  
Source: wikibooks:Automobile Repair/Jump start#Step-by-Step Guide
```

**gpt-oss-20b MXFP4 reasoning-low, arm B** — 129 words

```
Needed: 2 jumper cables  
Time: 5 minutes  

1. Clamp one cable to the positive (+) terminal of the dead battery.  
2. Connect the other end of the positive cable to the positive terminal of the good battery.  
3. Connect one end of the negative (-) cable to the negative terminal of the good battery.  
4. Connect the other end of the negative cable to metal on the engine block of the car with the dead battery.  
5. Stand back and start the car with the good battery.  
6. Start the stalled car.  
7. Remove the cables in reverse order.  

Mistake: Forgetting to clamp the negative cable to the engine block instead of the dead battery; avoid by following the order above.  

Source: wikibooks:Automobile Repair/Jump start#Step-by-Step Guide, CC BY-SA 4.0
```

**gpt-oss-20b MXFP4 reasoning-low, arm C** — 361 words

```
Needed: positive cable, negative cable, good battery, dead battery, engine block metal point
Time: not stated
1. Clamp the positive cable to the positive (+) terminal of the dead battery. Use a clamp tool; attach the cable to the + terminal only; do not let the cable touch any other metal. Done when: You see the clamp securely fastened to the + terminal and no other metal contact.
2. Connect the other end of the positive cable to the positive terminal of the good battery. Use the same clamp; attach the cable to the + terminal of the good battery. Done when: The cable is firmly attached to the good battery’s + terminal.
3. Connect one end of the negative cable to the negative terminal of the good battery. Clamp the cable to the – terminal of the good battery. Done when: The negative cable is securely fastened to the – terminal.
4. Connect the other end of the negative cable to metal on the engine block of the car with a. Attach the cable to a clean, unpainted metal spot on the engine block; do not connect to the dead battery, carburetor, fuel lines or moving parts. Done when: The cable is attached to the engine block and no other metal surfaces are touched.
5. Stand back and start the car with the good battery. Press the ignition of the car with the good battery; ensure you are at a safe distance. Done when: The good battery’s car engine turns over.
6. Start the stalled car. Press the ignition of the car with the dead battery; it should start. Done when: The stalled car’s engine turns over and runs.
7. Remove the cables in reverse order. First remove the negative cable from the engine block, then from the good battery, then the positive cable from the good battery, and finally from the dead battery. Done when: All cables are disconnected and no metal contacts remain.
Mistake: Connecting the negative cable to the dead battery or to moving parts can cause sparks or damage; avoid by attaching it only to the good battery and the engine‑b
Source: wikibooks:Automobile Repair/Jump start#Step-by-Step Guide
```

**gpt-oss-20b MXFP4 reasoning-low, arm D** — 374 words

```
Needed: items, time, steps, mistake, warning, car
Time: not stated
1. Clamp one cable to the positive (+) terminal of the dead battery. Use a jumper cable; clamp the red (positive) cable to the positive terminal of the dead battery. Do not let the positive cable touch any other metal. Done when: You see the cable securely attached to the positive terminal and no other metal contact.
2. Connect the other end of the positive cable to the positive terminal of the good battery. Take the free end of the red (positive) cable and clamp it to the positive terminal of the working battery. Done when: The cable is firmly attached to the positive terminal of the good battery.
3. Connect one end of the negative (-) cable to the negative terminal of the good battery. Clamp the black (negative) cable to the negative terminal of the working battery. Done when: The cable is securely attached to the negative terminal of the good battery.
4. Connect the other end of the negative cable to metal on the engine block of the car with a. Clamp the free end of the black (negative) cable to a clean, unpainted metal surface on the engine block of the dead‑battery car. Do not attach it to the dead battery, a. Done when: The cable is attached to the engine block and not touching the dead battery or any moving parts.
5. Stand back and start the car with the good battery. Sit in the working car, turn the key to the start position, and let the engine run. Done when: The engine of the good‑battery car turns over and stays running.
6. Start the stalled car. Sit in the dead‑battery car, turn the key to the start position. The engine should turn over and start. Done when: The stalled car’s engine turns over and begins running.
7. Remove the cables in reverse order. First remove the negative cable from the engine block, then from the good battery, then remove the positive cable from the good battery, and finally from the dead battery. Done when: All cables are disconnected and no longer touching any terminals or metal surfaces.
Mistake: not stated
Source: wikibooks:Automobile Repair/Jump start#Step-by-Step Guide, CC BY-SA 4.0
```

#### how to tie a tie

**Gemma-4-12B QAT Q4_0, arm A** — 193 words

```
Needed: a tie with a narrow and wide end
Time: 5 minutes

1. Place the tie around your collar the right way visible so the seams and label are not visible.
2. Adjust the length so the wide end is 30 cm or 12 inches lower than the narrow end.
3. Place the wide end across the front of the narrow end about 12 cm or 5 inches from the collar.
4. Fold the wide end behind the narrow end and loop the same wide end over the front.
5. Put the wide end through the back of the large loop that is around your neck.
6. Push the wide end through the smaller loop around the tie and pull it through carefully.
7. Pull the larger end until the shape of the knot is what you want.
8. Pull the thin end carefully to move the knot closer to your collar.
9. Make small adjustments until you are happy with the knot.

Mistake: The wide end folding near the forming knot; avoid this by straightening it as you push it through.

Source: [wikibooks:How To Tie A Tie/Tying the Knots, CC BY-SA 4.0]
```

**Gemma-4-12B QAT Q4_0, arm B** — 159 words

```
Needed: a necktie, a shirt with a collar
Time: 2 minutes
1. Drape the tie around your collar with the wide end hanging 30 cm or 12 inches lower than the narrow end.
2. Cross the wide end over the narrow end so that it sits about 12 cm or 5 inches from the collar.
3. Wrap the wide end behind the narrow end and loop it over the front to create a small loop.
4. Pass the wide end through the back of the large neck loop and then through the smaller loop.
5. Pull the wide end carefully until the knot forms a pleasing shape and move it toward your collar.
6. Make small adjustments to the knot until you are happy with the final look.
Mistake: Ensure the wide end does not begin to fold near the forming knot by straightening it if it occurs.
Source: [wikibooks:How To Tie A Tie/Tying the Knots, CC BY-SA 4.0]
```

**Gemma-4-12B QAT Q4_0, arm C** — 341 words

```
Needed: tie, collar
Time: not stated
1. Place the tie around your collar. Place the tie around the collar the right way visible (i.e. the seams and label not visible). Done when: The seams and label are not visible.
2. Position the ends of the tie. Adjust the relative length of the narrow and wide ends so that the wide end is 30 cm or 12 inches lower than the narrow end. Done when: The wide end is 30 cm or 12 inches lower than the narrow end.
3. Cross the tie ends. Place the wide end of the tie across the front of the narrow end about 12 cm or 5 inches from the collar. Done when: The wide end is across the front of the narrow end.
4. Fold the wide end. Fold the wide end behind the narrow end and loop the same (wide) end over the front. Done when: The wide end is looped over the front.
5. Pass the wide end through the neck loop. Put the wide end through the back of the large loop (which is around the neck). Done when: The wide end is through the back of the large loop.
6. Pass the wide end through the small loop. Push through the same (wide) end through the smaller loop (around the tie) and pull it through carefully. Done when: The wide end is through the smaller loop.
7. Shape and tighten the knot. Pull the larger end until the shape of the knot is what you want, then pull the thin end carefully to move the knot closer to your collar. Done when: The knot is the desired shape and moved closer to the collar.
8. Finalize the knot. Make small adjustments until you are happy with the knot. Done when: You are happy with the knot.
Mistake: For the Four-In-Hand knot, ensure that the wide end does not begin to fold near the forming knot; if this occurs, straighten it.
Source: wikibooks:How To Tie A Tie/Tying the Knots, CC BY-SA 4.0
```

**Gemma-4-12B QAT Q4_0, arm D** — 339 words

```
Needed: necktie (with a narrow and a wide end), shirt with a collar
Time: not stated
1. Place the tie around your collar. Place the tie around the collar the right way visible (i.e. the seams and label not visible). Done when: The tie is around your neck with the label hidden.
2. Position the ends of the tie. Adjust the length so the wide end is 30 cm or 12 inches lower than the narrow end. Done when: The wide end hangs 30 cm or 12 inches below the narrow end.
3. Cross the wide end over the narrow end. Place the wide end of the tie across the front of the narrow end about 12 cm or 5 inches from the collar. Done when: The wide end is crossed over the narrow end near the collar.
4. Fold the wide end behind the narrow end. Fold the wide end behind the narrow end and loop the same (wide) end over the front. Done when: The wide end is looped over the front of the narrow end.
5. Pass the wide end through the neck loop. Put the wide end through the back of the large loop (which is around the neck). Done when: The wide end is threaded through the loop around your neck.
6. Push the wide end through the small loop. Push through the same (wide) end through the smaller loop (around the tie) and pull it through carefully. Done when: The wide end is pulled through the smaller loop to form a knot.
7. Tighten and adjust the knot. Pull the thin end carefully and push the bottom of the knot gently to move the knot closer to your collar. Done when: The knot is positioned against your collar and has a pleasing shape.
Mistake: When pushing the wide end through the smaller loop, the wide end may begin to fold near the forming knot; avoid this by straightening it as you pull.
Source: wikibooks:How To Tie A Tie/Tying the Knots, CC BY-SA 4.0
```

**Qwen3-14B Q4_K_M, arm A** — 151 words

```
Needed: a tie, a collar or shirt with a neck hole  
Time: 5 minutes  
1. Place the tie around your neck with the wide end hanging 12 inches lower than the narrow end.  
2. Cross the narrow end over the wide end, about 5 inches from the collar.  
3. Wrap the wide end across the narrow end and around the back of the large loop formed around your neck.  
4. Push the wide end through the smaller loop formed around the tie and pull it through carefully.  
5. Pull the narrow end gently to move the knot closer to your collar.  
6. Adjust the knot until it is tight and centered.  
Mistake: The wide end may fold near the knot; straighten it before pulling through.  
Warning: Do not pull too hard, as it may damage the tie or cause discomfort.  
Source: wikibooks:How To Tie A Tie/Tying the Knots, CC BY-SA 4.0
```

**Qwen3-14B Q4_K_M, arm B** — 204 words

```
Needed: a tie, a collar or shirt, nothing special
Time: 5 minutes
1. Place the tie around the collar with the seams and label visible (for Oriental knot) or not visible (for Four-In-Hand or Windsor).
2. Adjust the tie so the wide end is 30 cm (12 inches) lower than the narrow end for Oriental or Four-In-Hand, or 50 cm or lower for Windsor.
3. For Oriental: Place the narrow end across the front of the wide end about 12 cm (5 inches) from the collar.
4. Wrap the wide end across the narrow end so it is now the right way on the outside of the knot.
5. Put the wide end through the back of the large loop (which is around the neck).
6. Push the wide end through the smaller loop (around the tie) and pull it through carefully.
7. Pull the thin end carefully and push the bottom of the knot gently to move it closer to your collar.
8. Make small adjustments until you are happy with the knot.
Mistake: Forgetting to adjust the length of the tie before starting, which can lead to an improperly tied knot.
Source: wikibooks:How To Tie A Tie/Tying the Knots, CC BY-SA 4.0
```

**Qwen3-14B Q4_K_M, arm C** — 302 words

```
Needed: a tie, a collar or shirt with a neck opening
Time: 5 to 10 minutes
1. Place the tie around the collar the right way visible. not stated. Done when: The seams and label are not visible.
2. Adjust the length of the tie so the wide end is 30 cm or 12 inches lower than the narrow. not stated. Done when: The wide end is 30 cm or 12 inches below the narrow end.
3. Place the wide end of the tie across the front of the narrow end about 12 cm or 5 inches. not stated. Done when: The wide end is positioned 12 cm or 5 inches from the collar.
4. Fold the wide end behind the narrow end and loop the same (wide) end over the front. not stated. Done when: The wide end is folded behind and then looped over the narrow end.
5. Put the wide end through the back of the large loop (which is around the neck). not stated. Done when: The wide end is threaded through the large loop around the neck.
6. Push through the same (wide) end through the smaller loop (around the tie) and pull it. not stated. Done when: The wide end is pulled through the smaller loop and forms a knot.
7. Pull the thin end carefully and push the bottom of the knot gently to move it closer to. not stated. Done when: The knot is positioned near the collar.
8. Make small adjustments until you are happy with the knot. not stated. Done when: The knot is tight, centered, and looks even.
Mistake: The wide end may begin to fold near the forming knot. To avoid this, straighten it if this occurs.
Warning: null
Source: wikibooks:How To Tie A Tie/Tying the Knots, CC BY-SA 4.0
```

**Qwen3-14B Q4_K_M, arm D** — 215 words

```
Needed: a tie
Time: not stated
1. Place the tie around the collar the right way visible (i.e. the seams and label notvisible. not stated. Done when: not stated.
2. Adjust the relative length of the narrow and wide ends so that the tie is the desired leng. not stated. Done when: not stated.
3. Place the wide end of the tie across the front of the narrow end about 12 cm or 5 inches f. not stated. Done when: not stated.
4. Put the wide end through the back of the large loop (which is around the neck). not stated. Done when: not stated.
5. Push through the same (wide) end through the smaller loop (around the tie) and pull it th. not stated. Done when: not stated.
6. Pull the thin end carefully and push the bottom of the knot gently, to move the knot clo. not stated. Done when: not stated.
7. Make small adjustments until you are happy with the knot. not stated. Done when: not stated.
Mistake: The most common mistake is not ensuring the wide end is the correct length below the narrow end. To avoid this, use the guide of having the wide end 30 cm or 12
Warning: null
Source: wikibooks:How To Tie A Tie/Tying the Knots
```

**gpt-oss-20b MXFP4 reasoning-low, arm A** — 184 words

```
Needed: tie, collar, hands  
Time: 2–3 minutes  
1. Place the tie around your collar so the wide end is on the right and the narrow end on the left, with the label facing away from you.  
2. Adjust the lengths so the wide end hangs about 30 cm (12 inches) lower than the narrow end.  
3. Bring the wide end across the front of the narrow end, about 12 cm (5 inches) from the collar.  
4. Fold the wide end behind the narrow end and loop it over the front.  
5. Push the wide end through the back of the large loop that surrounds your neck.  
6. Push the same wide end through the smaller loop that is around the tie itself, then pull it through carefully.  
7. Pull the wide end until the knot looks the shape you want.  
8. Pull the narrow end gently to bring the knot closer to your collar, then adjust as needed.  
Mistake: The wide end can fold near the knot; straighten it immediately if this happens.  
Source: wikibooks:How To Tie A Tie/Tying the Knots, CC BY-SA 4.0
```

**gpt-oss-20b MXFP4 reasoning-low, arm B** — 135 words

```
Needed: nothing special  
Time: 2 minutes  
1. Place the tie around the collar the right way visible, with the wide end 30 cm lower than the narrow end.  
2. Place the wide end across the front of the narrow end about 12 cm from the collar.  
3. Fold the wide end behind the narrow end and loop it over the front.  
4. Put the wide end through the back of the large loop around the neck.  
5. Push the same wide end through the smaller loop around the tie and pull it carefully.  
6. Pull the thin end to move the knot closer to the collar, adjusting until satisfied.  
Mistake: The wide end may fold near the forming knot; straighten it if this occurs.  
Source: wikibooks:How To Tie A Tie/Tying the Knots, CC BY-SA 4.0
```

**gpt-oss-20b MXFP4 reasoning-low, arm C** — 2 words — FORMAT: insufficient source

```
INSUFFICIENT SOURCE
```

**gpt-oss-20b MXFP4 reasoning-low, arm D** — 2 words — FORMAT: insufficient source

```
INSUFFICIENT SOURCE
```

#### how to get from narita airport to tokyo

**Gemma-4-12B QAT Q4_0, arm A** — 167 words

```
Needed: nothing special
Time: 55–61 minutes
1. Locate the train station at your terminal by following the signs marked in English for Narita Airport Terminal 1 Station or Narita Airport Terminal 2·3 Station.
2. Walk 500 meters or take the free shuttle bus if you are arriving at Terminal 3 to reach the Terminal 2 station.
3. Go to the JR Travel Service Center or a regular JR counter to purchase a reserved-seat ticket or a special round trip ticket for ¥5,000.
4. Board the Narita Express (N'EX) train to travel to Tokyo Station or other major stations like Shinagawa, Shibuya, and Shinjuku.
5. Stay in your seat for the duration of the ride, as all seats on the N'EX are reserved.
6. Exit at Tokyo Station or your desired destination station.

Mistake: Getting off at the wrong terminal station; check the lists of airlines and their terminals posted inside the trains.
Warning: Smoking is prohibited on all trains.

Source: [wikivoyage:Narita International Airport#Ground transportation, CC BY-SA 4.0]
```

**Gemma-4-12B QAT Q4_0, arm B** — 158 words

```
Needed: Japan Rail Pass or JR East Pass, or ¥5,000 for a round trip ticket, or ¥3,070 for a standard class ticket
Time: 60 minutes
1. Locate the JR Travel Service Center or a regular JR counter inside the airport terminal to purchase your train ticket or reserve a seat.
2. Follow the signs for the Narita Express (N'EX) train to find the correct platform for your departure to Tokyo Station.
3. Board the Narita Express train and sit in a reserved seat to enjoy a direct one-seat connection to major stations.
4. Watch for the train's destination signs to ensure you exit at Tokyo Station or your specific stop like Shinagawa, Shibuya, or Shinjuku.
Mistake: Forgetting that Terminal 3 is not served directly by train; walk 500 meters or take the free shuttle bus from Terminal 2.
Warning: Smoking is strictly prohibited on all trains departing from Narita Airport.

Source: [wikivoyage:Narita International Airport#Ground transportation, CC BY-SA 4.0]
```

**Gemma-4-12B QAT Q4_0, arm C** — 205 words

```
Needed: Passport, Money (Japanese Yen), Optional: Japan Rail Pass or JR East Pass
Time: 36 to 82 minutes
1. Locate the correct train station based on your arrival terminal. Look for signs in English for '1 Narita Airport Terminal 1 Station' or '2 Narita Airport Terminal 2·3 Station'. Done when: You see the station entrance.
2. Purchase a ticket or use a fare card. Use a Suica or PASMO card, or buy a ticket at a manned counter or ticket machine. For the Narita Express, a round trip for foreigners costs ¥5,000. Done when: You have your ticket or a charged card.
3. Board the train to Tokyo. Choose between the Skyliner (fastest, 36 minutes), Narita Express (direct to major stations, 55–60 minutes), or a commuter train (budget, ~82 minutes). Done when: You are seated on the train.
4. Exit at your destination station. Listen for announcements or watch for signs for Tokyo Station, Keisei Ueno, Nippori, or other major hubs. Done when: You see the station platform and exit signs.
Mistake: Missing your stop because trains split; if heading south to Shinagawa, consider changing at Shinagawa instead of Tokyo as it is closer to the Shinkansen.
Source: wikivoyage:Narita International Airport#Ground transportation, CC BY-SA 4.0
```

**Gemma-4-12B QAT Q4_0, arm D** — 225 words

```
Needed: Passport, Suica or PASMO card (optional), Japan Rail Pass or JR East Pass (optional), Cash (Yen)
Time: 36 to 82 minutes
1. Locate the correct train station based on your arrival terminal. Look for signs in English for '1 Narita Airport Terminal 1 Station' or '2 Narita Airport Terminal 2·3 Station' at the airport. Done when: You see the clearly marked station entrance.
2. Purchase your train ticket or reserve a seat. Go to the JR Travel Service Center, a regular JR counter, or a manned Keisei/Skyliner limited express ticket counter to buy a ticket or exchange a voucher. Done when: You hold a physical ticket or a reserved seat voucher.
3. Board your chosen train to Tokyo. Enter the station and board the Skyliner, Narita Express, Keisei Sky Access Line, or a commuter train. Done when: You are seated on the train and moving toward Tokyo.
4. Exit the train at your destination station. Listen for announcements or watch for signs for Tokyo Station, Keisei Ueno, Nippori, Shinagawa, Shibuya, or Shinjuku. Done when: You feel the train come to a stop at your desired destination.
Mistake: Getting off at the wrong terminal station. Ensure you check the signs for Terminal 1 or Terminal 2·3, as Terminal 3 requires a 500-meter walk or a shuttle bus.
Source: wikivoyage:Narita International Airport#Ground transportation, CC BY-SA 4.0
```

**Qwen3-14B Q4_K_M, arm A** — 127 words

```
Needed: train ticket, Suica or PASMO card, luggage
Time: 1 hour
1. Go to the train station at Narita Airport Terminal 1 or Terminal 2·3.
2. Buy a ticket for the Skyliner or Narita Express (N'EX) at the ticket counter or vending machine.
3. Take the Skyliner or N'EX train to Tokyo Station, Shinagawa, Shibuya, or Shinjuku.
4. If using a Suica or PASMO card, tap it on the reader at the gate to enter the train.
5. Arrive at your destination station and exit.
Mistake: Getting off at the wrong terminal station; check the station name in English on the train.
Warning: Be careful when changing trains at Tokyo Station due to its large size and multiple levels.
Source: wikivoyage:Narita International Airport#Ground transportation, CC BY-SA 4.0
```

**Qwen3-14B Q4_K_M, arm B** — 155 words

```
Needed: train ticket, map, Suica/PASMO card
Time: 1 hour
1. Find the correct train station for your terminal: Terminal 1 uses Narita Airport Terminal 1 Station, and Terminal 2·3 uses Narita Airport Terminal 2·3 Station.
2. Purchase a ticket for the JR Narita Express (N'EX) or a limited express train like the Skyliner at the ticket counter or vending machine.
3. Board the train at the station marked for your terminal, ensuring you are on the correct platform.
4. Sit in a reserved seat if you have one, or find an available seat on non-reserved trains.
5. Arrive at your destination station in Tokyo, such as Tokyo Station, Shinjuku, or Shibuya.
6. If using a Suica or PASMO card, tap it on the reader at the station entrance and exit.
Mistake: Getting off at the wrong station; double-check the station name on the train and the destination on your ticket.
Source: wikivoyage:Narita International Airport#Ground transportation
```

**Qwen3-14B Q4_K_M, arm C** — 194 words

```
Needed: Train ticket or Suica/PASMO card, Luggage
Time: 36 to 82 minutes depending on the train
1. Find the correct train station for your terminal. Look for the station signs in English: Narita Airport Terminal 1 Station or Narita Airport Terminal 2·3 Station. Terminal 3 is a short walk or shuttle from Terminal 2. Done when: You see the station name in English and the correct terminal number.
2. Purchase a train ticket or use a Suica/PASMO card. Buy a ticket at the ticket counter or vending machine, or use a Suica/PASMO card at the gate. For the Skyliner, you may need to purchase a voucher in advance. Done when: You have a ticket or your Suica/PASMO card is accepted at the gate.
3. Board the train to Tokyo. Find the correct platform for your train (Skyliner, Narita Express, or Keisei Sky Access Line). Check the train schedule and destination signs. Done when: You are seated on the train and it begins to move.
Mistake: Getting off at the wrong station. To avoid this, check the train's destination and ensure it matches your intended stop in Tokyo.
Warning: null
Source: wikivoyage:Narita International Airport#Ground transportation
```

**Qwen3-14B Q4_K_M, arm D** — 203 words

```
Needed: Train ticket or Suica/PASMO card, Luggage
Time: 36 to 82 minutes depending on the method
1. Choose a train line from Narita Airport to Tokyo. Look at the train schedule or information boards at the airport. Options include JR Narita Express, Keisei Skyliner, or Keisei Main Line. Choose based on your destination. Done when: You see the train line and its schedule on the information board or in the app.
2. Purchase a ticket or use a Suica/PASMO card. Go to the ticket counter or vending machine. For JR Narita Express, you can buy a ticket or use a Japan Rail Pass. For Keisei, buy a Skyliner ticket or use a Suica card. Done when: You have a ticket in your hand or your Suica/PASMO card is charged.
3. Board the correct train at the correct terminal station. Go to the correct terminal station (Terminal 1 or Terminal 2·3). Check the train's destination and board the correct one. Done when: You are on the train heading toward your destination in Tokyo.
Mistake: Getting off at the wrong station. To avoid this, check the train's destination and confirm it matches your intended stop before getting off.
Warning: null
Source: wikivoyage:Narita International Airport#Ground transportation
```

**gpt-oss-20b MXFP4 reasoning-low, arm A** — 159 words

```
Needed: Narita Airport Terminal 1 or 2 station, JR ticket or Suica/PASMO, JR Travel Service Center or ticket machine  
Time: about 1 hour 15 minutes total  

1. At the airport terminal, locate the train station sign for “Narita Airport Terminal 1 Station” or “Terminal 2·3 Station.”  
2. Buy a ticket for the Narita Express (N'EX) to Tokyo Station at the JR ticket counter or machine; if you have a Japan Rail Pass, show it and get a free seat reservation.  
3. Board the N'EX train; it departs roughly every 30 minutes between 07:37 and 21:44.  
4. Sit in the reserved seat; the ride takes about 55–60 minutes to Tokyo Station.  
5. Arrive at Tokyo Station, then transfer to your next train or subway line.  

Mistake: Assuming the N'EX stops at every Tokyo station – it only stops at major stations (Tokyo, Shinagawa, Shibuya, Shinjuku). Use a map or ask staff for the correct stop.  

Source: wikivoyage:Narita International Airport#Ground transportation
```

**gpt-oss-20b MXFP4 reasoning-low, arm B** — 148 words

```
Needed: nothing special  
Time: 1 hour 15 minutes  

1. From the airport terminal, locate the station sign for “Narita Airport Terminal 1 Station” or “Terminal 2·3 Station” in English.  
2. Walk to the platform for the Narita Express (N'EX) train; the platform is marked in English and Japanese.  
3. Purchase a ticket or use a Suica/PASMO stored fare card at the ticket machine or JR counter.  
4. Board the N'EX train; all seats are reserved, but you can sit anywhere if you have a ticket.  
5. Ride the N'EX for about 55–60 minutes to Tokyo Station, watching the train’s arrival announcements.  
6. At Tokyo Station, follow the signs to the Shinkansen or other major stations (Shinagawa, Shibuya, Shinjuku) if you need a direct connection.  

Mistake: Assuming the train will stop at every station; the N'EX only stops at major stations.  

Source: wikivoyage:Narita International Airport#Ground transportation, CC BY-SA 4.0
```

**gpt-oss-20b MXFP4 reasoning-low, arm C** — 2 words — FORMAT: insufficient source

```
INSUFFICIENT SOURCE
```

**gpt-oss-20b MXFP4 reasoning-low, arm D** — 2 words — FORMAT: insufficient source

```
INSUFFICIENT SOURCE
```

#### how to make cold brew coffee

**Gemma-4-12B QAT Q4_0, arm A** — 159 words

```
Needed: coarse ground coffee beans, filtered water, a large glass jar, a strainer or coffee filter
Time: 12 to 24 hours
1. Measure out 1 cup of coarsely ground coffee for every 4 cups of water.
2. Pour the coffee grounds into the large glass jar.
3. Pour the filtered water over the grounds until the jar is full.
4. Stir the mixture gently with a long spoon until all grounds are wet.
5. Cover the jar with a lid and place it in the refrigerator.
6. Let the coffee sit undisturbed for at least 12 hours.
7. Strain the liquid through a fine mesh strainer or coffee filter into a clean pitcher.
8. Pour the finished coffee into glasses over ice.
Mistake: Using finely ground coffee, which makes the brew muddy; use coarse grounds that look like sea salt.
Warning: Do not leave the coffee on the counter for too long, as it can spoil at room temperature.
```

**Gemma-4-12B QAT Q4_0, arm B** — 146 words

```
Needed: coarse ground coffee, cold water, a large glass jar, a coffee filter or mesh strainer
Time: 12 to 24 hours
1. Measure out one cup of coarse coffee grounds and pour them into the bottom of the large glass jar.
2. Pour four cups of cold water over the grounds until the jar is nearly full, ensuring all grounds are soaked.
3. Cover the jar with a lid and place it in the refrigerator for at least 12 hours until the liquid turns dark.
4. Place a filter over a second container and pour the coffee mixture through it to separate the grounds from the liquid.
5. Pour the finished coffee into a glass and serve it over ice or with a splash of milk.
Mistake: Using fine grounds will make the coffee muddy and bitter; use a coarse grind that looks like sea salt.
```

**Gemma-4-12B QAT Q4_0, arm C** — 223 words

```
Needed: Coarse ground coffee beans, Cold filtered water, Large glass jar or French press, Fine mesh strainer or coffee filter
Time: 12 to 24 hours
1. Measure the coffee and water. Use a measuring cup to combine 1 cup of coarse ground coffee with 4 cups of cold filtered water in a large glass jar. Done when: The jar is filled with the coffee and water mixture.
2. Mix the ingredients. Use a long spoon to stir the coffee grounds into the water until all the grounds are submerged. Done when: The coffee grounds are evenly distributed throughout the water.
3. Steep the coffee. Cover the jar with a lid or cloth and place it in the refrigerator for 12 to 24 hours. Done when: The coffee has sat in the refrigerator for the full duration.
4. Strain the coffee. Pour the mixture through a fine mesh strainer or coffee filter into a clean carafe. Done when: The liquid coffee is in the carafe and the grounds remain in the strainer.
5. Serve the coffee. Pour the finished cold brew into a glass over ice. Done when: The coffee is in your glass ready to drink.
Mistake: Using coffee grounds that are too fine (like espresso grounds) will make the coffee muddy and bitter; use a coarse grind that looks like sea salt.
```

**Gemma-4-12B QAT Q4_0, arm D** — 208 words

```
Needed: Coarse ground coffee beans (1 cup), Cold filtered water (4 cups), Large glass jar or pitcher, Fine mesh strainer or cheesecloth, Ice cubes
Time: 12 to 24 hours
1. Combine the coffee grounds and water in the jar. Pour 1 cup of coarse grounds into the jar, then pour 4 cups of cold water over them, stirring briefly with a spoon. Done when: The coffee grounds are fully submerged under the water.
2. Steep the coffee mixture. Cover the jar with a lid or cloth and place it in the refrigerator for 12 to 24 hours. Done when: The water has turned into a dark, opaque brown liquid.
3. Strain the coffee into a clean container. Place the mesh strainer over a second jar and pour the mixture through it, letting the grounds stay in the strainer. Done when: The liquid in the second jar is smooth and free of coffee grounds.
4. Serve the cold brew. Pour the coffee into a glass over 1 to 2 cups of ice. Done when: The coffee is chilled and ready to drink.
Mistake: Using coffee grounds that are too fine (like espresso grounds) will make the coffee muddy and bitter. Use a coarse grind that looks like sea salt.
```

**Qwen3-14B Q4_K_M, arm A** — 171 words

```
Needed: coffee grounds, water, a container with lid, a way to strain coffee  
Time: 12 to 24 hours  

1. Place coffee grounds in a container with a lid, using about 1 cup of grounds for every 4 cups of water.  
2. Pour cold water over the coffee grounds until the container is full.  
3. Stir the mixture gently with a spoon to make sure all grounds are wet.  
4. Cover the container with a lid and leave it in a cool, dark place for 12 to 24 hours.  
5. After the time is up, place a coffee filter in a strainer over a clean container.  
6. Pour the coffee mixture into the strainer and let it drain completely.  
7. Discard the used coffee grounds and store the cold brew in the refrigerator.  

Mistake: Using hot water instead of cold water can make the coffee bitter. Always use cold or room temperature water.  
Warning: Do not leave the coffee mixture at room temperature for more than 24 hours to avoid bacterial growth.
```

**Qwen3-14B Q4_K_M, arm B** — 195 words — FORMAT: 1 step(s) over 30 words

```
Needed: coffee beans, coarse grind, water, large container, coffee filter or paper towels, mug or carafe  
Time: 12 to 24 hours  

1. Grind coffee beans to a coarse consistency using a coffee grinder, ensuring the grounds are not too fine to avoid over-extraction.  
2. Place the coffee grounds in a large container, such as a mason jar or pitcher, and add cold water, making sure the grounds are fully submerged.  
3. Cover the container with a lid or plastic wrap and let it sit at room temperature for 12 to 24 hours, stirring occasionally to ensure even extraction.  
4. After steeping, place a coffee filter or layer of paper towels over the mouth of the container and pour the coffee through it into a clean mug or carafe to separate the liquid from the grounds.  
5. Discard the used coffee grounds and enjoy the cold brew coffee immediately or store it in the refrigerator for up to a week.  

Mistake: Using fine coffee grounds can lead to a bitter taste; always use a coarse grind for cold brew.  
Warning: Do not use hot water when preparing the coffee, as it can cause over-extraction and bitterness.
```

**Qwen3-14B Q4_K_M, arm C** — 253 words

```
Needed: coarse ground coffee beans, filtered water, large container with lid, plastic or glass jar, coffee filter or cheesecloth, funnel, pitcher or bottle
Time: 12 to 24 hours
1. Measure the coffee grounds. Use 1 cup of coarse ground coffee beans. Find the measuring cup in your kitchen. Done when: The coffee grounds are level in the measuring cup.
2. Add the coffee grounds to the container. Place the coffee grounds in a large container with a lid. Use a container that can hold at least 4 cups of liquid. Done when: The coffee grounds are evenly spread at the bottom of the container.
3. Pour filtered water over the coffee grounds. Add 4 cups of filtered water. Pour slowly over the coffee grounds until they are fully covered. Done when: The water covers the coffee grounds and the container is about 3/4 full.
4. Cover the container and refrigerate. Put the lid on the container and place it in the refrigerator. Let it sit for 12 to 24 hours. Done when: The container is cold and the coffee is steeping.
5. Strain the coffee. Use a coffee filter or cheesecloth in a funnel to strain the coffee into a pitcher or bottle. Place the funnel over the pitcher or bottle. Done when: The liquid flows through the filter and into the pitcher or bottle, leaving the coffee grounds behind.
Mistake: Using fine ground coffee instead of coarse ground coffee. To avoid this, always use a coarse grind for cold brew.
Warning: None
```

**Qwen3-14B Q4_K_M, arm D** — 267 words

```
Needed: coarse ground coffee beans, filtered water, large container with lid, plastic or glass funnel, pitcher or jar for storing, coffee filter (optional)
Time: 12 to 24 hours
1. Measure the coffee grounds. Use 1 cup of coarse ground coffee beans. Find a measuring cup in your kitchen. Done when: You have the correct amount of coffee in the measuring cup.
2. Add the coffee grounds to the container. Place the coffee grounds into a large container with a lid. Use a container that can hold at least 4 cups of liquid. Done when: The coffee grounds are evenly spread at the bottom of the container.
3. Pour filtered water over the coffee grounds. Add 4 cups of filtered water. Use a pitcher or measuring cup to pour the water slowly over the coffee. Done when: The water covers the coffee grounds completely.
4. Cover the container and refrigerate. Put the lid on the container and place it in the refrigerator. The temperature should be between 35°F and 40°F (2°C and 4°C). Done when: The container is in the refrigerator and the coffee is steeping.
5. Strain the coffee. After 12 to 24 hours, use a plastic or glass funnel and coffee filter to strain the coffee into a pitcher or jar. Done when: The liquid flows through the filter and into the pitcher, leaving the grounds behind.
Mistake: Using fine ground coffee instead of coarse grounds. To avoid this, always use a coarse grind for cold brew.
Warning: Do not use hot water when making cold brew, as it can cause the coffee to become bitter.
```

**gpt-oss-20b MXFP4 reasoning-low, arm A** — 170 words

```
Needed: coffee grounds, cold water, large jar or pitcher, fine mesh sieve or cheesecloth, spoon, timer  
Time: about 12–24 hours  

1. Measure 1 cup of coarsely ground coffee and pour it into the jar.  
2. Add 4 cups of cold, filtered water. Stir gently with a spoon until all grounds are wet.  
3. Seal the jar and let it sit at room temperature for 12–24 hours, stirring once after 6 hours.  
4. After steeping, line a fine mesh sieve with cheesecloth and place it over a bowl.  
5. Pour the coffee mixture through the sieve, pressing gently with the back of a spoon to extract all liquid.  
6. Discard the grounds and transfer the brewed coffee to a clean container.  
7. Store the cold brew in the refrigerator; it keeps fresh for up to a week.  

Mistake: Skipping the coarse grind causes a gritty brew; use a coarse grind to avoid sediment.  

Warning: If using a metal filter, avoid metal-to-metal contact with the coffee grounds to prevent a metallic taste.
```

**gpt-oss-20b MXFP4 reasoning-low, arm B** — 123 words

```
Needed: coffee beans, coarse grinder, large jar or pitcher, fine mesh sieve or cheesecloth, cold water, refrigerator  
Time: 12–24 hours  

1. Measure 1 cup of coarsely ground coffee into the jar.  
2. Pour 4 cups of cold filtered water over the grounds, stir gently until all grounds are wet.  
3. Seal the jar, place it in the refrigerator, and let steep for 12–24 hours.  
4. After steeping, strain the mixture through a fine mesh sieve lined with cheesecloth into a clean container.  
5. Discard the grounds, refrigerate the brewed coffee, and serve over ice or dilute with water or milk.  

Mistake: Using a fine grind; it clogs the filter and makes the brew bitter.  

Warning: Handle the hot water carefully to avoid burns.
```

**gpt-oss-20b MXFP4 reasoning-low, arm C** — 323 words

```
Needed: coffee beans (medium‑fine grind), filtered water, large jar or pitcher, mesh strainer or cheesecloth, container for the finished brew
Time: about 12–24 hours
1. Measure the coffee grounds. Use a kitchen scale or measuring cup: 1 cup (≈100 g) of medium‑fine ground coffee for every 4 cups (≈950 ml) of filtered water. Pour the grounds into the jar. Done when: You see a uniform layer of grounds sitting at the bottom of the jar, not clumped or uneven.
2. Add the water. Pour 4 cups (≈950 ml) of cold, filtered water over the grounds. Use a measuring cup to ensure the volume is correct. Gently stir once with a spoon to wet all the grounds. Done when: All the grounds are fully saturated and the mixture looks evenly mixed, with no dry pockets.
3. Cover and steep. Place a lid or plastic wrap on the jar, then put it in the refrigerator or a cool room. Let it steep for 12–24 hours; 12 hours for a milder brew, 18–24 hours for a richer. Done when: After the steeping time, you can taste a small sip of the liquid: it should have a smooth, mild coffee flavor.
4. Filter the brew. Place a mesh strainer or a piece of cheesecloth over a second container. Slowly pour the cold brew through the filter, allowing the liquid to pass while the grounds stay. Done when: You see a clear, dark liquid in the container with no visible grounds.
5. Store the finished cold brew. Transfer the filtered coffee to a clean bottle or jar. Seal it and keep it in the refrigerator. Use within 5–7 days for best flavor. Done when: The bottle is sealed, the liquid is cold, and you can see the clear coffee inside.
Mistake: Using too fine a grind or too much coffee can make the brew bitter and cloudy. Keep the grind medium‑fine and stick to the 1:4 coffee‑to‑water ratio.
```

**gpt-oss-20b MXFP4 reasoning-low, arm D** — 305 words

```
Needed: Coffee beans, Filtered water, Large jar or pitcher, Fine mesh sieve or cheesecloth, Measuring spoon or scale, Timer, Refrigerator
Time: 4–12 hours (depending on steeping time)
1. Measure the coffee. Use a kitchen scale or a 2‑tsp spoon. For a standard 1‑qt (946 ml) jar, weigh 40–50 g of coarsely ground coffee. Done when: You see the weight on the scale or the spoonful on the counter.
2. Add the coffee to the jar. Pour the measured coffee into the clean jar or pitcher. Done when: The jar contains a uniform bed of coffee grounds.
3. Add water. Pour cold, filtered water into the jar until the coffee grounds are fully submerged. For a 1‑qt jar, add 1 qt (946 ml) of water. Use a long‑spoon or stir stick to gently,. Done when: All grounds are wet and the mixture looks evenly mixed.
4. Cover and steep. Place a lid or plastic wrap on the jar. Put the jar in the refrigerator and let it steep for 12 hours (or 4–6 hours for a milder brew). Done when: You hear the refrigerator hum and the jar sits undisturbed.
5. Filter the brew. Place a fine mesh sieve or a piece of cheesecloth over a second jar or pitcher. Slowly pour the cold brew through the filter, allowing all liquid to pass. Done when: You hear a gentle pour and see the liquid clear out of the filter.
6. Serve or store. Pour the filtered cold brew into a glass. Add ice, milk, or sweetener if desired. Store any leftover brew in the refrigerator for up to 1 week. Done when: The glass contains a clear, dark‑colored liquid that tastes smooth.
Mistake: Using too fine a grind or too much coffee can make the brew bitter and cloudy. Avoid by grinding coarsely and measuring accurately.
```

#### how to factory reset an android phone

**Gemma-4-12B QAT Q4_0, arm A** — 109 words

```
Needed: nothing special
Time: 10 minutes
1. Open the Settings app on your phone.
2. Tap on the System or General Management menu.
3. Select the Reset option from the list.
4. Tap on Factory Data Reset or Erase All Data.
5. Tap the button to confirm the reset.
6. Enter your PIN, pattern, or password if prompted.
7. Tap Confirm or Delete All Data to start the process.
Mistake: Forgetting to back up your photos and files before starting, as this process deletes everything.
Warning: Ensure your phone has at least 50% battery or is plugged into a charger to prevent it from turning off during the reset.
```

**Gemma-4-12B QAT Q4_0, arm B** — 128 words

```
Needed: your phone, your Google account password
Time: 10 minutes
1. Open the Settings app by tapping the gear icon on your home screen or app drawer.
2. Tap the System or General Management icon to find the device's core settings menu.
3. Tap the Reset option and select the Factory Data Reset or Erase All Data choice.
4. Tap the Erase All Data button at the bottom of the screen to confirm your intent.
5. Enter your screen lock PIN or pattern when the prompt appears to verify your identity.
6. Tap Confirm and wait for the phone to restart, which shows the initial setup screen.
Mistake: Forgetting to back up your photos and files before starting, which can be avoided by using a cloud service first.
```

**Gemma-4-12B QAT Q4_0, arm C** — 235 words

```
Needed: Android phone, Google account password, Power button, Volume buttons
Time: 10 to 15 minutes
1. Back up your personal data. Go to Settings > System > Backup and restore, and select 'Back up to Google Drive' to save your photos and contacts. Done when: You see a confirmation message saying 'Backup complete'.
2. Remove your Google account from the device. Go to Settings > Passwords & accounts, tap your Google account, and select 'Remove account' to prevent Factory Reset Protection from locking the phone. Done when: The account name disappears from the list of accounts.
3. Open the factory reset menu. Go to Settings > System > Reset options, and tap 'Erase all data (factory reset).'. Done when: A screen appears showing a list of items that will be deleted.
4. Confirm the reset. Tap the 'Erase all data' button at the bottom of the screen and enter your device's PIN or pattern if prompted. Done when: The screen goes black and shows a 'Deleting' or 'Resetting' progress bar.
5. Restart the phone. Wait for the device to automatically reboot, which takes about 2 to 5 minutes. Done when: The phone displays the 'Welcome' or 'Hello' setup screen as if it were brand new.
Mistake: Forgetting to remove your Google account first. To avoid this, always sign out of your account in the settings menu before starting the reset, or you may be 'Fp
```

**Gemma-4-12B QAT Q4_0, arm D** — 216 words

```
Needed: Android phone, Google account password, Charger
Time: 15 to 30 minutes
1. Back up your personal data. Go to Settings > System > Backup (or Google > Backup) and tap 'Back up now' to save your photos and contacts to the cloud. Done when: You see a message saying 'Backup complete'.
2. Remove your Google account. Go to Settings > Passwords & accounts (or Users & accounts), tap your Google account, and select 'Remove account'. Done when: The account is no longer listed in your account menu.
3. Start the factory reset. Go to Settings > System > Reset options (or General Management > Reset) and tap 'Erase all data (factory reset). Done when: A confirmation screen appears showing the list of items to be deleted.
4. Confirm the deletion. Tap the 'Erase all data' or 'Reset' button at the bottom of the screen and enter your device PIN or pattern if prompted. Done when: The screen goes black and the phone restarts with a 'Welcome' or 'Hello' setup screen.
Mistake: Forgetting to back up your data before starting. To avoid this, ensure your photos and contacts are synced to a cloud service or moved to a computer before youh
Warning: A factory reset will permanently delete all photos, messages, and apps from your device.
```

**Qwen3-14B Q4_K_M, arm A** — 101 words

```
Needed: Android phone, access to Google account, internet connection  
Time: 5 minutes  

1. Turn on your Android phone and tap the **Settings** app.  
2. Scroll down and tap **System**, then select **Reset options**.  
3. Tap **Erase all data (factory reset)**.  
4. Confirm the action by tapping **Reset phone**.  
5. Wait for the phone to restart and follow the setup steps.  

Mistake: Forgetting to back up important data before resetting. Always back up your data using Google Drive or a computer before performing a factory reset.  
Warning: A factory reset will delete all data on the phone, including apps, photos, and settings.
```

**Qwen3-14B Q4_K_M, arm B** — 140 words

```
Needed: Android phone, computer (optional), charging cable
Time: 10 minutes
1. Turn on the phone and go to the Settings app, then tap System > Advanced > Reset options.
2. Tap Erase all data (factory reset) and confirm the action by tapping Reset phone.
3. Wait for the phone to restart and follow the setup steps as if it were new for the first time.
4. If the phone is unresponsive, press and hold the Power and Volume Down buttons until the recovery menu appears.
5. Use the Volume buttons to highlight "Wipe data/factory reset" and press the Power button to select it.
6. Choose "Reboot system now" to restart the phone and complete the reset.
Mistake: Forgetting to back up important data before resetting, which can be avoided by using Google Drive or a computer to save files first.
```

**Qwen3-14B Q4_K_M, arm C** — 253 words

```
Needed: Android phone, USB cable, Computer (optional)
Time: 10 to 20 minutes
1. Turn off the Android phone. Press and hold the power button until the power menu appears, then tap 'Power off'. Done when: The screen goes dark and the phone stops responding to touch.
2. Press and hold the power and volume down buttons together. Use your fingers to press and hold both buttons for at least 10 seconds. Done when: The phone screen lights up and shows the recovery menu or a logo.
3. Use the volume buttons to select 'Wipe data/factory reset'. Press the volume down button to scroll down to the option, then press the power button to select it. Done when: The screen confirms the selection with a message or a checkmark.
4. Select 'Yes' to confirm the factory reset. Press the volume down button to highlight 'Yes', then press the power button to confirm. Done when: The screen shows a progress bar or message indicating the reset is in progress.
5. Wait for the reset to complete. Do nothing while the phone automatically reboots and sets itself up. Done when: The phone restarts and shows the initial setup screen, like choosing a language or connecting to Wi-Fi.
Mistake: Not holding the power and volume buttons long enough to enter recovery mode. To avoid this, press and hold both buttons for at least 10 seconds until the screen
Warning: Factory reset will delete all data on the phone. Make sure to back up important information before proceeding.
```

**Qwen3-14B Q4_K_M, arm D** — 234 words

```
Needed: Android phone
Time: 10 to 15 minutes
1. Turn off the phone. Press and hold the power button until the power menu appears, then tap 'Power off'. Done when: The screen goes dark and the phone stops responding.
2. Press and hold the power and volume down buttons together. Use both hands to press and hold the power button and the volume down button at the same time. Done when: The phone screen lights up and shows the recovery menu.
3. Select 'Wipe data/factory reset' using the volume buttons. Use the volume down button to scroll to 'Wipe data/factory reset', then press the power button to select it. Done when: The screen shows a confirmation message asking if you want to proceed.
4. Confirm the factory reset. Use the volume down button to scroll to 'Yes' and press the power button to confirm. Done when: The phone begins the reset process and shows a progress bar or message.
5. Wait for the reset to complete. Do not touch the phone until it restarts automatically. Done when: The phone restarts and shows the initial setup screen.
Mistake: Not holding the buttons long enough to enter recovery mode. To avoid this, press and hold both buttons for at least 10 seconds until the recovery menu appears.
Warning: Factory reset will erase all data on the phone. Make sure to back up important information before proceeding.
```

**gpt-oss-20b MXFP4 reasoning-low, arm A** — 99 words

```
Needed: backup data, charger, phone with battery >20%  
Time: 15–30 minutes  

1. Open Settings, scroll to “System” or “General management.”  
2. Tap “Reset” or “Reset options.”  
3. Select “Erase all data (factory reset).”  
4. Read the warning screen; tap “Reset phone.”  
5. If a PIN, pattern, or password is set, enter it when prompted.  
6. Confirm by tapping “Erase everything.”  
7. Wait while the phone clears data and restarts.  

Mistake: Skipping a backup – you’ll lose photos, contacts, and apps.  
Warning: If the battery is low, the phone may shut down during reset, causing data loss. Keep it charged.
```

**gpt-oss-20b MXFP4 reasoning-low, arm B** — 118 words

```
Needed: nothing special  
Time: 5–10 minutes  

1. Unlock the phone and swipe down from the top to open the notification shade.  
2. Tap the gear icon to open Settings.  
3. Scroll to System, tap it, then select Reset options.  
4. Choose Erase all data (factory reset).  
5. Confirm by tapping Reset phone and then Delete all.  
6. Enter your PIN, pattern, or password if prompted.  
7. Wait for the phone to reboot and complete the reset.  

Mistake: Forgetting to back up important data before resetting; avoid by backing up contacts, photos, and apps first.  
Warning: If the phone is locked with a Google account, you must remove the account or have the credentials to unlock it after reset.
```

**gpt-oss-20b MXFP4 reasoning-low, arm C** — 242 words

```
Needed: Android phone (any model), Charged battery or power source, Internet connection (optional for re‑setup)
Time: 5–10 minutes
1. Open the Settings app. Tap the Settings icon on the home screen or app drawer. It looks like a gear. Done when: The Settings screen appears with categories such as System, Apps, etc.
2. Navigate to System settings. Scroll down until you see 'System' (or 'General Management' on some phones) and tap it. Done when: The System screen opens, showing options like Advanced, Language & input, Reset options.
3. Open Reset options. Tap 'Reset options' (or 'Backup & reset'). Done when: A list of reset choices appears, including 'Erase all data (factory reset)'.
4. Select Erase all data (factory reset). Tap the option, then tap 'Reset phone' or 'Erase everything'. Done when: A confirmation dialog shows the data that will be deleted and a final 'Erase phone' button.
5. Confirm the reset. Enter your PIN, pattern, or password if prompted, then tap 'Erase phone' (or 'Erase everything'). Done when: The phone begins to wipe data; you see a progress bar and the screen goes dark.
6. Wait for the phone to reboot. Let the device finish wiping and restart automatically. Done when: The phone boots to the initial setup screen (welcome screen or Android logo).
Mistake: Skipping the backup step before resetting. Without a backup, all personal data (contacts, photos, apps) will be permanently lost. To avoid this, back up to a PC
```

**gpt-oss-20b MXFP4 reasoning-low, arm D** — 267 words

```
Needed: Android phone (any model running Android 6.0 or newer), Internet connection (optional, for re‑setup), Backup of important data (optional)
Time: 5–10 minutes
1. Open the Settings app. Tap the Settings icon on the home screen or in the app drawer. It looks like a gear. Done when: The Settings screen appears, showing categories such as System, Apps, and Privacy.
2. Navigate to the System section. Scroll down the Settings list until you see ‘System’ (or ‘About phone’ on some devices). Tap it. Done when: You see options like Advanced, Reset options, or Backup & reset.
3. Select Reset options. In the System menu tap ‘Reset options’ (or ‘Backup & reset’). Done when: A screen appears with choices such as ‘Erase all data (factory reset)’.
4. Choose Erase all data (factory reset). Tap ‘Erase all data (factory reset)’. Done when: A warning screen shows that all personal data, apps, and settings will be deleted.
5. Confirm the reset. Tap the button that says ‘Erase everything’ or ‘Reset phone’. If your device has a PIN, pattern, or password, enter it when prompted. Done when: A progress bar appears and the phone begins rebooting. The screen will go black and then show the initial boot.
6. Wait for the phone to finish resetting. Let the device reboot and complete the setup wizard. This usually takes 2–5 minutes. Done when: The phone displays the welcome screen for a new user, with no apps or data from before.
Mistake: Skipping the backup step can lead to permanent loss of photos, contacts, and app data. Always back up important information before resetting.
```

