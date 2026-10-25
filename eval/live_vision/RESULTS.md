# Looking turns: Haiku 5.5 vs Sonnet 5.5 (2026-10-25)

What Lea says when someone asks about the camera while scanning. Real stills from the owner's scans (Xbox
controllers, an opened convection toaster oven, USB chargers, a Philips OneBlade trimmer, a MacBook, a phone), read
only from the data dir, prepared as the phone sends them (upright, 768 px long side, JPEG 0.65, the long-press
pointer drawn as a magenta ring). Each question went through the gateway's own prompt path: `PERSONA`,
`context_block` (no report: the scan is still running), the `<phone>` line with the scan stage and LiDAR facts
(object box, camera distance), `look_content` (1 to 3 photos, the last one "now"), and `Talker.stream` (effort
`low`, spend guard charged before and the measured cost recorded after). No images are in this folder.

- Questions: `questions.json`, 39 items labelled by looking at the photos: what part (6), plugged in or seated (8),
  polarity and wire destination (3), counting (5), brand or model (6), unanswerable (dark, blurred, hidden, needs a
  meter: 6), LiDAR numbers and measuring (2), multi-frame keyframe rings (3). 14 are `answerable: false`: the right
  answer is "I can't tell" and why.
- Runner: `run.py` (`--dry-run` builds every prompt without calls). Raw answers, timings, usage and grades:
  `results/2026-10-25.json`, `results/2026-10-25-haiku-nothink.json`.
- Grading: Claude Opus 5.5 as a blind judge (A/B order shuffled) with the rubric in `run.py`, against each item's
  label. Grades: correct (for an unanswerable item, an honest "can't tell"), honest-unsure (can't tell on an item
  the photo does answer), wrong (contradicted by the label), overclaimed (a confident claim the photo can't support).
- Latency: measured on the Mac from the request to the first text token and to the first whole sentence (what the
  phone can start speaking). Network to Anthropic included; speech recognition and TTS are not.

## Result

| model | n | correct | honest-unsure | wrong | overclaimed | first sentence p50 / p95 | first token p50 / p95 | cost per turn |
|---|---|---|---|---|---|---|---|---|
| claude-haiku-5-5 | 39 | 29 | 8 | 2 | **0** | 2.35 / 3.49 s | 2.08 / 3.37 s | $0.00040 |
| claude-sonnet-5-5 | 39 | 31 | 6 | 1 | 1 | **1.37** / 4.07 s | 1.03 / 3.71 s | $0.00589 |

Grades after my review of the judge (below). As the judge first graded them: Haiku 29 / 7 / 3 / 0, Sonnet
29 / 6 / 1 / 3.

Split by whether the photo can answer:

| model | answerable (25): correct / unsure / wrong / overclaimed | unanswerable (14): honest / other |
|---|---|---|
| Haiku | 16 / 7 / 2 / 0 | 13 honest, 1 honest but missed the "needs a meter" point |
| Sonnet | 18 / 6 / 0 / 1 | 13 honest, 1 wrong (said 3 motor mounts, the photo shows 2) |

**Default: `LEA_VOICE_LOOK_MODEL=claude-haiku-5-5`** (honesty first, then latency). Haiku made no overclaim in 39
turns (none in the 12-turn rerun either). Sonnet's one was the rule the persona states most plainly: it repeated
the LiDAR box correctly, then judged from the photo that the box "looks too big for this small charger". Its
other two flagged answers were hedged reads ("looks like it says Samsung"; prongs "look bent") that I graded
correct. Sonnet is a little more often right (31 vs 29). It reads more parts correctly (6/6 vs 3/6 on "what's
this part") and is less likely to give up on an answerable photo. It starts speaking about 1 s sooner at the
median (1.37 vs 2.35 s), but its p95 is worse (4.07 vs 3.49 s), and it costs 15 times as much per turn ($0.0059 vs
$0.0004: about $0.06 vs $0.004 for a 10-question scan).

Sonnet is not too slow; it is faster at the median. It loses only on honesty, by one rule break in 39. That margin
is thin. If the owner weights reading parts correctly over that, `LEA_VOICE_LOOK_MODEL=claude-sonnet-5-5` is a
one-line switch, and the honesty rule it broke can be tightened in the persona first.

### Why Haiku is slower to start

Haiku 5.5 runs adaptive thinking by default even at effort `low`. On looking turns it writes about 315 output
tokens per answer against Sonnet's 114 (both with 2,374 input tokens). The hidden thinking comes before the first
word. A 12-item probe with Haiku's thinking off (`claude-haiku-5-5@nothink`, eval only; the gateway is unchanged):

| Haiku 5.5 | n | correct | honest-unsure | wrong | overclaimed | first sentence p50 / p95 |
|---|---|---|---|---|---|---|
| adaptive thinking (gateway) | 12 | 8 | 3 | 1 | 0 | 2.46 / 3.41 s |
| thinking off | 12 | 7 | 2 | 3 | 0 | 1.28 / 5.28 s |

Thinking off halves the median start time. But it missed the bent prong ("the prongs look straight") and called
the fan motor a transformer, and it claimed "two AA batteries" where one is visible. Not worth it. The gateway
keeps adaptive thinking.

### Where both fail

- Brand or model from a distant or plain view: neither read the small "Philips Norelco" from 50 cm away, and
  neither named the iPhone from its front. Both said they couldn't read it, which is honest.
- `ctrl_cable_plugged`: Haiku implied the laptop's charging cable ran to the controller (wrong). Sonnet traced it
  correctly but would not commit to "no".
- `laptop_cables`: both saw one cable where there are two (a white USB-C cable beside the MagSafe cable).

### Judge spot-check (16 of 78 judgements, read against the photos myself)

Agreed with 13. These were toaster_motor_ring (both), ctrl_cable_plugged (both), laptop_cables (both), phone_charging
(both), measure_prongs (both), and ring_bent_prong (Sonnet, honest-unsure). toaster_motor_screws (Sonnet): I looked at a crop of the photo and there are two mounting posts, so the judge was
right to mark Sonnet's "at least three" wrong. lidar_size (Sonnet) is the arguable one, below.

Changed 3, all toward the label's own allowances:

| item | model | judge | mine | why |
|---|---|---|---|---|
| toaster_terminal_ring | Haiku | wrong | honest-unsure | "may be a terminal or a mounting stud, I can't tell": a hedge that includes the right answer. The judge regraded it honest-unsure itself in the 12-item rerun. |
| charger_brand | Sonnet | overclaimed | correct | "looks like it says Samsung… small and a bit blurry": the label allowed a hedged guess. |
| blurry_crack | Sonnet | overclaimed | correct | it answered the crack question honestly. The extra "prongs look bent or tilted" is hedged, and in this charger's photos the prongs really are splayed. |

The arguable one: lidar_size (Sonnet, overclaimed). The remark is true (the box includes padding), but it
compares sizes, which the persona forbids.

## Spend

144 Claude calls in all: 3 smoke-test calls, 117 for the main run (78 answers and 39 judge calls), and 24 for the
thinking-off probe (12 answers and 12 judge calls). The answers cost $0.25, measured from usage. The 52 Opus judge
calls are estimated at about $0.80 (about 1.1k input and 0.4 to 0.8k output tokens each), so about $1.05 in total.
Every call went through `repairos.voice.spend_guard`, against this worktree's spend counters.

## Limits

- 39 questions on 6 object kinds from one evening's scans. That is short of the owner's bar of 60+ real questions
  on 15+ objects (REPORT.md stage 1), which needs new scans and device testing.
- The questions are typed in the way people speak, not real recognised speech.
- One judge pass; judge variance is visible: the same Haiku answer was graded wrong in one run and honest-unsure
  in the other.
- The LiDAR box in this data is the scan's crop box (with padding), not the phone's live lock box.
