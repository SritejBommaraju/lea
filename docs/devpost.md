# LEA: Devpost write-up (draft)

**Tagline:** Scan anything with your iPhone, then ask Lea; she answers and shows you on a 3D model of your own object.

## Inspiration
You open a broken device and stare at a board full of parts with no idea which one matters. Repair guides show
someone else's device, not yours. We wanted an assistant that looks at your actual object and points.

## What it does
Scan → See → Ask Lea. Walk around the object with your iPhone, flip it to get the bottom, and in seconds you have a 3D
model of your object and a report on what looks wrong. Ask "where's A2?" and Lea answers out loud while the camera
flies to the pin, circles it, and shows the real photo of it.

## How we built it
- **iPhone app:** Swift, SwiftUI, ARKit and LiDAR capture, Metal live overlays, a SceneKit 3D stage.
- **Gateway and pipeline:** Python and FastAPI on one Runpod GPU pod. The pipeline registers flipped scans, builds a
  textured mesh (or, for thin boards, a photo slab with plane-sweep stereo relief), inspects close-ups with an OWLv2
  detector and Claude vision, and ray-casts findings onto the model.
- **Lea:** Claude answers with markers that drive the 3D camera, a per-scan part index from the photos, and web search
  for general questions. Cartesia handles speech in and out.
- **Live mirror:** SpacetimeDB streams scans and models to a big-screen viewer.

## Challenges we ran into
- Circuit boards are thinner than LiDAR's noise. We built the model from the photos and measured part heights by
  stereo, with a quality gate so bad matches never become fake towers.
- A flipped board shares almost no geometry with its top. We register it from its outline, through-holes and connector
  edges.
- Mapping a spoken question to the exact spot on the model and its best photo.
- Keeping voice fast and the camera moves in step with the sentence being spoken.

## Accomplishments that we're proud of
The first time Lea turned the model to the B button, lit it up and faded in the real close-up while answering, it felt
like the object was explaining itself. On the board scans, Lea read 22 of the header pins and circled them on the right
sockets, from the photos alone.

## What we learned
Honesty beats confidence when the evidence is thin, and confidence beats hedging when it isn't. Every visual has to
come from the user's own scan or it stops feeling real.

## What's next
Registering flipped boards from more cues, live part pointing while scanning, and repair steps linked to each finding.
